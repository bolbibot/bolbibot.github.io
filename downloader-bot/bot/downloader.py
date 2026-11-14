"""Обёртка над yt-dlp: что можно скачать, в каком качестве, и само скачивание."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import yt_dlp
import yt_dlp.utils

from .config import Settings

log = logging.getLogger(__name__)
TARGETS = (360, 720, 1080)
AUDIO_SUFFIXES = (".m4a", ".mp3", ".opus", ".ogg", ".aac", ".weba", ".wav", ".flac")
SELECTOR_RE = re.compile(r"[\w.+/\-]+")       # идентификаторы форматов yt-dlp, ничего лишнего
# Извлекатели только разрешённых сайтов. Остальные (их больше тысячи, и часть принимает
# любой адрес) не загружаются вовсе — сайт не сможет перенаправить бота куда попало.
SITE_EXTRACTORS = {
    "youtube": ["youtube.*"],
    "vk": ["vk.*"],
    "rutube": ["rutube.*"],
    "tiktok": ["tiktok.*", r"vm\.tiktok"],
    "dzen": [r"dzen\.ru.*"],
    "ok": ["odnoklassniki"],
    "twitch": ["twitch.*"],
    "vimeo": ["vimeo.*"],
    "dailymotion": ["dailymotion.*"],
}
PART_RE = re.compile(r"\.f[\w-]+\.\w+$")  # несклеенная половина: abc.f140.m4a
SKIP_SUFFIXES = (".part", ".ytdl", ".tmp", ".jpg", ".webp", ".png", ".json")


class DownloadError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class Option:
    quality: str          # "360" | "720" | "1080" | "best" | "audio"
    label: str
    size: int | None
    selector: str | None = None   # какой именно формат скачивать (из списка yt-dlp)


@dataclass
class VideoInfo:
    site: str
    video_id: str
    title: str
    duration: int | None
    options: list[Option]


def _size(fmt: dict, duration) -> int | None:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        return int(size)
    if fmt.get("tbr") and duration:
        return int(fmt["tbr"] * 1000 / 8 * duration)
    return None


def _has_video(fmt: dict) -> bool:
    return fmt.get("vcodec") not in (None, "none") and bool(fmt.get("height"))


def _has_audio(fmt: dict) -> bool:
    return fmt.get("acodec") not in (None, "none")


def _level(fmt: dict) -> int:
    """«Качество» видео: короткая сторона кадра. Вертикальные 1080×1920 — это 1080p."""
    width, height = fmt.get("width"), fmt.get("height")
    return min(width, height) if width and height else height


def _is_h264(fmt: dict) -> bool:
    return str(fmt.get("vcodec") or "").startswith(("avc", "h264"))


def _selector(fmt: dict) -> str | None:
    fid = str(fmt.get("format_id") or "")
    if not fid or not SELECTOR_RE.fullmatch(fid):
        return None
    if _has_audio(fmt):
        return f"{fid}/{fid}+ba"
    return f"{fid}+ba[ext=m4a]/{fid}+ba/{fid}"


def build_options(info: dict, max_filesize: int) -> list[Option]:
    """Кнопки качества для ролика. Чистая функция, сеть не трогает."""
    formats = info.get("formats") or []
    duration = info.get("duration")
    videos = [f for f in formats if _has_video(f)]
    audios = [f for f in formats if _has_audio(f) and not _has_video(f)]
    best_audio = max(audios, key=lambda f: _size(f, duration) or 0, default=None)
    audio_size = _size(best_audio, duration) if best_audio else None

    def video_size(fmt: dict) -> int | None:
        size = _size(fmt, duration)
        if size is None:
            return None
        if not _has_audio(fmt) and audio_size:
            size += audio_size
        return size

    def best_at(level: int) -> dict:
        same = [f for f in videos if _level(f) == level]
        # h264 Telegram проигрывает прямо в чате; среди равных — файл побольше
        return max(same, key=lambda f: (_is_h264(f), _size(f, duration) or 0))

    def option(quality: str, label: str, fmt: dict) -> Option:
        return Option(quality, label, video_size(fmt), _selector(fmt))

    options: list[Option] = []
    seen: set[int] = set()
    for target in TARGETS:
        levels = [_level(f) for f in videos if _level(f) <= target]
        if not levels or max(levels) in seen:
            continue
        level = max(levels)
        seen.add(level)
        options.append(option(str(target), f"{level}p", best_at(level)))
    if videos and not options:  # всё крупнее 1080p — берём самое скромное
        options.append(option("best", "Видео", best_at(min(_level(f) for f in videos))))

    if audios or any(_has_audio(f) for f in videos):
        size = audio_size
        if not audios:  # звук только внутри видеофайла: качать придётся его целиком
            sizes = [_size(f, duration) for f in videos if _has_audio(f)]
            size = min((x for x in sizes if x), default=None)
        elif size is None and duration:
            size = int(192_000 / 8 * duration)
        options.append(Option("audio", "MP3", size))

    return [o for o in options if o.size is None or o.size <= max_filesize]


def format_selector(quality: str) -> str:
    if quality == "audio":
        return "ba/b"
    if quality == "best":
        return "wv*[vcodec^=avc1]+ba[ext=m4a]/w[ext=mp4]/wv*+ba/w"
    if quality not in {str(t) for t in TARGETS}:
        raise ValueError(f"unknown quality: {quality!r}")
    h = f"[height<={quality}]"
    return (f"bv*{h}[vcodec^=avc1]+ba[ext=m4a]/b{h}[ext=mp4]/"
            f"bv*{h}+ba/b{h}")


_ERRORS = (
    ("private", ("private video", "is private", "members-only", "members only", "login required",
                 "only available to", "доступ ограничен")),
    ("blocked_by_site", ("sign in to confirm", "not a bot", "http error 429", "too many requests",
                         "http error 403", "captcha")),
    ("unsupported", ("unsupported url", "no suitable extractor", "is not a valid url")),
    ("unavailable", ("video unavailable", "http error 404", "http error 410", "video not found",
                     "has been removed", "video does not exist", "no video formats",
                     "not available in your country", "not available from your location",
                     "geo restricted", "geo-restricted", "georestricted",
                     "this video is not available", "content is not available")),
)


def map_error(message: str) -> str:
    text = (message or "").lower()
    for code, needles in _ERRORS:
        if any(n in text for n in needles):
            return code
    return "unknown"


class _Quiet:
    """yt-dlp пишет ошибки в stderr даже с quiet=True; ошибки мы получаем исключениями."""

    def debug(self, msg):
        pass

    error = debug

    def warning(self, msg):
        log.warning("yt-dlp: %s", str(msg)[:500])


def make_guard(max_bytes: int, deadline: float):
    """Обрывает загрузку, когда скачано слишком много или прошло слишком много времени.

    Нужен потому, что max_filesize у yt-dlp не действует на потоковые (HLS/DASH) видео.
    """
    sizes: dict[str, int] = {}

    def guard(d: dict) -> None:
        if d.get("status") not in ("downloading", "finished"):
            return
        sizes[str(d.get("filename"))] = int(d.get("downloaded_bytes") or 0)
        if sum(sizes.values()) > max_bytes:
            guard.tripped = "too_big"
        elif time.monotonic() > deadline:
            guard.tripped = "timeout"
        if guard.tripped:
            raise DownloadError(guard.tripped)

    guard.tripped = None
    return guard


def allowed_extractors(s: Settings) -> list[str]:
    """Извлекатели только тех сайтов, что разрешены в настройках."""
    from .links import SITE_ALIASES
    names: list[str] = []
    for host in s.allowed_hosts:
        site = SITE_ALIASES.get(host, host.split(".")[-2] if "." in host else host)
        family = SITE_EXTRACTORS.get(site)
        if family is None:  # сайт добавлен вручную: его извлекатель нам неизвестен
            return ["default", "-generic"]
        names += [n for n in family if n not in names]
    return names


def ydl_opts(s: Settings, site: str) -> dict:
    opts: dict = {
        "logger": _Quiet(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "playlistend": 1,
        "extract_flat": "in_playlist",
        "socket_timeout": 30,
        "retries": 3,
        "allowed_extractors": allowed_extractors(s),
        "cachedir": False,
        "color": "never",
    }
    proxy = s.proxies.get(site) or s.proxy_default
    if proxy:
        opts["proxy"] = proxy
    if site == "youtube" and s.youtube_cookies:
        opts["cookiefile"] = s.youtube_cookies
    return opts


def _extract(opts: dict, url: str, download: bool) -> dict:
    # yt-dlp перезаписывает файл cookies при закрытии. Параллельные загрузки делили бы
    # один файл и могли бы прочитать его недописанным — у каждой своя копия.
    private_cookies = None
    source = opts.get("cookiefile")
    if source:
        opts = dict(opts)
        if os.path.isfile(source):
            fd, private_cookies = tempfile.mkstemp(prefix="cookies-", suffix=".txt")
            os.close(fd)
            shutil.copyfile(source, private_cookies)
            opts["cookiefile"] = private_cookies
        else:
            log.warning("файл cookies не найден: %s", source)
            del opts["cookiefile"]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=download) or {}
    except yt_dlp.utils.DownloadError as e:
        raise DownloadError(map_error(str(e)), str(e)) from e
    except DownloadError:
        raise
    except Exception as e:  # yt-dlp иногда бросает что попало
        raise DownloadError("unknown", repr(e)) from e
    finally:
        if private_cookies:
            try:
                os.unlink(private_cookies)
            except OSError:
                pass


async def probe(s: Settings, url: str, site: str) -> VideoInfo:
    info = await asyncio.to_thread(_extract, ydl_opts(s, site), url, False)
    if info.get("_type") in ("playlist", "multi_video") or "entries" in info:
        raise DownloadError("playlist")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise DownloadError("live")
    duration = info.get("duration")
    if duration and duration > s.max_duration:
        raise DownloadError("too_long")
    all_options = build_options(info, 10**18)
    if not all_options:
        raise DownloadError("unavailable")
    options = [o for o in all_options if o.size is None or o.size <= s.max_filesize]
    if not options:
        raise DownloadError("too_big")
    raw_id = str(info.get("id") or "").strip()
    if not raw_id:  # без идентификатора нельзя ни кэшировать, ни блокировать ролик
        raise DownloadError("unavailable")
    return VideoInfo(
        site=site,
        video_id=f"{str(info.get('extractor_key') or site).lower()}:{raw_id}",
        title=str(info.get("title") or "Видео")[:200],
        duration=int(duration) if duration else None,
        options=options,
    )


async def download(s: Settings, url: str, site: str, quality: str, workdir: Path,
                   selector: str | None = None) -> Path:
    """Скачивает в workdir и возвращает путь к готовому файлу.

    selector — конкретный формат, выбранный при показе кнопок; общий подбор по
    качеству остаётся запасным вариантом.
    """
    fmt = format_selector(quality)
    if selector:
        if not SELECTOR_RE.fullmatch(selector):
            raise ValueError(f"bad format selector: {selector!r}")
        fmt = f"{selector}/{fmt}"
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    opts = ydl_opts(s, site)
    opts.update({
        "format": fmt,
        "outtmpl": str(workdir / "%(id).80s.%(ext)s"),
        "max_filesize": s.max_filesize,
        "restrictfilenames": True,
        "overwrites": True,
        "concurrent_fragment_downloads": 4,
    })
    guard = make_guard(s.max_filesize, time.monotonic() + s.download_timeout)
    opts["progress_hooks"] = [guard]
    if quality == "audio":
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                   "preferredquality": "192"}]
    else:
        opts["merge_output_format"] = "mp4"
    try:
        info = await asyncio.to_thread(_extract, opts, url, True)
    except DownloadError:
        if guard.tripped:  # yt-dlp мог обернуть наше исключение в своё
            raise DownloadError(guard.tripped) from None
        raise

    files = []
    for item in info.get("requested_downloads") or []:
        path = item.get("filepath")
        if path and Path(path).is_file():
            files.append(Path(path))
    if not files:
        files = [p for p in workdir.iterdir() if p.is_file()
                 and not p.name.endswith(SKIP_SUFFIXES) and not PART_RE.search(p.name)]
    if quality == "audio":
        files = [p for p in files if p.suffix.lower() == ".mp3"]
    else:
        files = [p for p in files if p.suffix.lower() not in AUDIO_SUFFIXES]
    if not files:  # yt-dlp молча пропускает часть больше max_filesize
        raise DownloadError("too_big")
    result = max(files, key=lambda p: p.stat().st_size)
    if result.stat().st_size > s.max_filesize:
        raise DownloadError("too_big")
    return result

async def video_size(path: Path) -> tuple[int, int] | None:
    """Ширина и высота видео — чтобы Telegram показал его без искажений."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 30)
        width, height = out.decode().strip().splitlines()[0].split("x")[:2]
        return int(width), int(height)
    except Exception:
        return None
