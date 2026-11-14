"""Главный сценарий: проверить право на скачивание, скачать, отправить, списать."""
from __future__ import annotations

import logging
import re
import secrets
import shutil
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from . import billing, downloader
from .config import Settings
from .downloader import DownloadError, VideoInfo
from .jobs import Busy, Jobs

log = logging.getLogger(__name__)

PENDING_TTL = 60 * 60
PENDING_MAX = 1000
PENDING_PER_USER = 5
JOB_DIR_RE = re.compile(r"[0-9a-f]{32}")
# Ответы Telegram, означающие, что сохранённый file_id больше не годится
STALE_FILE_ID = ("wrong file identifier", "wrong remote file", "file reference", "wrong file_id",
                 "invalid file", "file_id_invalid", "file is temporarily unavailable")


def clean_tmp(tmp_dir: str) -> None:
    """Удаляет остатки прошлых загрузок — только свои папки, остальное не трогает."""
    root = Path(tmp_dir)
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink() and JOB_DIR_RE.fullmatch(child.name):
            shutil.rmtree(child, ignore_errors=True)


class RateLimiter:
    """Не больше limit событий за window секунд на человека."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._hits: dict[int, deque] = {}

    def allow(self, user_id: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if len(self._hits) > 1000:
            for key in [k for k, q in self._hits.items() if now - q[-1] >= self.window]:
                del self._hits[key]
        hits = self._hits.setdefault(user_id, deque())
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


@dataclass
class PendingLink:
    url: str
    info: VideoInfo
    user_id: int
    created: float


@dataclass
class Ctx:
    settings: Settings
    db: object
    jobs: Jobs
    pending: dict[str, PendingLink] = field(default_factory=dict)
    probe_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(10, 600))


class Sender(Protocol):
    async def __call__(self, source: Path | str, kind: str, info: VideoInfo) -> str:
        """Отправляет файл (путь) или уже загруженный file_id; возвращает file_id."""


def put_pending(ctx: Ctx, link: PendingLink) -> str:
    now = time.time()
    for key in [k for k, v in ctx.pending.items() if now - v.created > PENDING_TTL]:
        del ctx.pending[key]
    own = [k for k, v in ctx.pending.items() if v.user_id == link.user_id]
    for key in own[:max(0, len(own) - PENDING_PER_USER + 1)]:  # свои старые кнопки
        del ctx.pending[key]
    while len(ctx.pending) >= PENDING_MAX:
        del ctx.pending[next(iter(ctx.pending))]  # самый старый
    token = secrets.token_urlsafe(6)
    ctx.pending[token] = link
    return token


def get_pending(ctx: Ctx, token: str) -> PendingLink | None:
    link = ctx.pending.get(token)
    if link is None or time.time() - link.created > PENDING_TTL:
        ctx.pending.pop(token, None)
        return None
    return link


async def user_blocked(ctx: Ctx, user_id: int) -> bool:
    cur = await ctx.db.execute("SELECT blocked FROM users WHERE id=?", (user_id,))
    row = await cur.fetchone()
    return bool(row and row["blocked"])


async def is_blocked(ctx: Ctx, user_id: int, site: str, video_id: str) -> bool:
    if await user_blocked(ctx, user_id):
        return True
    cur = await ctx.db.execute(
        "SELECT 1 FROM blocked_videos WHERE site=? AND video_id=?", (site, video_id))
    return await cur.fetchone() is not None


async def process_download(ctx: Ctx, user_id: int, pending: PendingLink, quality: str,
                           send: Sender, status: Callable[[str], Awaitable[None]]) -> str:
    """Возвращает: sent | no_quota | busy | blocked | error:<код>."""
    s, db, info = ctx.settings, ctx.db, pending.info
    if pending.user_id != user_id or quality not in {o.quality for o in info.options}:
        return "error:unknown"
    await billing.ensure_user(db, user_id)
    if await is_blocked(ctx, user_id, info.site, info.video_id):
        return "blocked"
    if (await billing.get_quota(db, s, user_id)).source is None:
        return "no_quota"

    kind = "audio" if quality == "audio" else "video"
    workdir = Path(s.tmp_dir) / uuid.uuid4().hex
    try:
        async with ctx.jobs.slot(user_id):
            # право могло быть потрачено, пока ждали очередь
            if (await billing.get_quota(db, s, user_id)).source is None:
                return "no_quota"
            size = None
            file_id = None
            cur = await db.execute(
                "SELECT file_id FROM file_cache WHERE site=? AND video_id=? AND quality=?",
                (info.site, info.video_id, quality))
            cached = await cur.fetchone()
            if cached:
                try:
                    file_id = await send(cached["file_id"], kind, info)
                except Exception as e:
                    if not any(n in str(e).lower() for n in STALE_FILE_ID):
                        # лимит запросов, бот заблокирован человеком и т.п. — кэш ни при чём
                        log.warning("не удалось отправить готовый файл: %s", e)
                        return "error:send"
                    log.warning("кэш file_id устарел для %s/%s", info.site, info.video_id)
                    await billing.write(
                        db, "DELETE FROM file_cache WHERE site=? AND video_id=? AND quality=?",
                        (info.site, info.video_id, quality))
            if file_id is None:
                await status("downloading")
                chosen = next(o for o in info.options if o.quality == quality)
                path = await downloader.download(s, pending.url, info.site, quality, workdir,
                                                 selector=chosen.selector)
                size = path.stat().st_size
                await status("sending")
                try:
                    file_id = await send(path, kind, info)
                except Exception:
                    log.exception("не удалось отправить файл %s/%s", info.site, info.video_id)
                    return "error:send"
            paid_with = await billing.charge(db, s, user_id) or "none"
            await billing.record_download(db, user_id, info.site, info.video_id, quality,
                                          size, paid_with)
            if file_id:
                await billing.write(
                    db, "INSERT OR REPLACE INTO file_cache (site, video_id, quality, file_id, kind)"
                    " VALUES (?,?,?,?,?)", (info.site, info.video_id, quality, file_id, kind))
            return "sent"
    except Busy:
        return "busy"
    except DownloadError as e:
        log.info("скачивание не удалось: %s %s", e.code, e.detail[:300])
        return f"error:{e.code}"
    except Exception:
        log.exception("неожиданная ошибка при скачивании")
        return "error:unknown"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
