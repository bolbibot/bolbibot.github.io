"""Тесты на находки итоговой проверки кода."""
import asyncio
import time
from pathlib import Path

import pytest

from bot import billing, core, downloader, texts
from bot.core import Ctx, PendingLink, RateLimiter
from bot.downloader import DownloadError, Option, VideoInfo
from bot.jobs import Jobs
from tests.test_downloader import INFO as RAW_INFO, FakeYDL, fake  # noqa: F401

URL = "https://rutube.ru/video/x/"


# 1. Остаток от неудачной склейки не должен уходить пользователю как «видео»

async def test_leftover_audio_part_is_not_the_result(settings, fake, tmp_path, monkeypatch):
    def run(self, url, download=False):
        (tmp_path / "abc.f140.m4a").write_bytes(b"x" * 50)   # видео-часть не скачалась
        return RAW_INFO
    monkeypatch.setattr(FakeYDL, "extract_info", run)
    with pytest.raises(DownloadError) as e:
        await downloader.download(settings, URL, "rutube", "720", tmp_path)
    assert e.value.code == "too_big"


async def test_result_comes_from_requested_downloads(settings, fake, tmp_path, monkeypatch):
    def run(self, url, download=False):
        (tmp_path / "abc.f140.m4a").write_bytes(b"x" * 500)
        (tmp_path / "abc.mp4").write_bytes(b"x" * 10)
        return {**RAW_INFO, "requested_downloads": [{"filepath": str(tmp_path / "abc.mp4")}]}
    monkeypatch.setattr(FakeYDL, "extract_info", run)
    path = await downloader.download(settings, URL, "rutube", "720", tmp_path)
    assert path.name == "abc.mp4"


async def test_audio_must_be_mp3(settings, fake, tmp_path, monkeypatch):
    def run(self, url, download=False):
        (tmp_path / "abc.m4a").write_bytes(b"x" * 10)       # конвертация не состоялась
        return RAW_INFO
    monkeypatch.setattr(FakeYDL, "extract_info", run)
    with pytest.raises(DownloadError):
        await downloader.download(settings, URL, "rutube", "audio", tmp_path)


# 2. Ограничение размера и времени для потоковых (HLS/DASH) загрузок

def test_guard_limits_bytes_across_parts():
    guard = downloader.make_guard(max_bytes=100, deadline=time.monotonic() + 60)
    guard({"status": "downloading", "filename": "a", "downloaded_bytes": 60})
    guard({"status": "downloading", "filename": "a", "downloaded_bytes": 70})   # тот же файл
    with pytest.raises(DownloadError) as e:
        guard({"status": "downloading", "filename": "b", "downloaded_bytes": 40})
    assert e.value.code == "too_big"


def test_guard_deadline():
    guard = downloader.make_guard(max_bytes=100, deadline=time.monotonic() - 1)
    with pytest.raises(DownloadError) as e:
        guard({"status": "downloading", "filename": "a", "downloaded_bytes": 1})
    assert e.value.code == "timeout"
    assert len(texts.error("timeout")) > 10 and texts.error("timeout") != texts.error("unknown")


async def test_download_installs_guard(settings, fake, tmp_path, monkeypatch):
    settings.max_filesize = 100

    def run(self, url, download=False):
        for hook in self.opts["progress_hooks"]:
            hook({"status": "downloading", "filename": "a", "downloaded_bytes": 101})
        return RAW_INFO
    monkeypatch.setattr(FakeYDL, "extract_info", run)
    with pytest.raises(DownloadError) as e:
        await downloader.download(settings, URL, "rutube", "720", tmp_path)
    assert e.value.code == "too_big"


def test_download_timeout_setting(settings):
    assert settings.download_timeout == 1800


# 3. Транзакции: сбой не должен «заклинить» базу, записи не должны теряться

async def test_failed_commit_does_not_wedge(db, settings, monkeypatch):
    await billing.ensure_user(db, 1)
    real = db.execute
    state = {"fail": True}

    async def flaky(sql, *args):
        if sql == "COMMIT" and state["fail"]:
            state["fail"] = False
            raise RuntimeError("disk I/O error")
        return await real(sql, *args)

    monkeypatch.setattr(db, "execute", flaky)
    with pytest.raises(RuntimeError):
        await billing.charge(db, settings, 1)
    assert db.in_transaction is False
    assert await billing.charge(db, settings, 1) == "free"   # списание откатилось и работает


async def test_writes_wait_for_open_transaction(db, settings):
    await billing.ensure_user(db, 1)
    started, release = asyncio.Event(), asyncio.Event()

    async def failing_tx():
        with pytest.raises(RuntimeError):
            async with billing.transaction(db):
                started.set()
                await release.wait()
                raise RuntimeError("rollback")

    task = asyncio.create_task(failing_tx())
    await started.wait()
    writes = asyncio.gather(
        billing.record_download(db, 1, "rutube", "abc", "720", 1, "free"),
        billing.write(db, "INSERT INTO blocked_videos VALUES (?,?)", ("rutube", "zzz")),
        billing.create_order(db, 1, settings.tariffs["p10"], "stars"),
        billing.ensure_user(db, 77),
    )
    await asyncio.sleep(0.02)
    release.set()
    await task
    await writes
    count = lambda t: db.execute(f"SELECT COUNT(*) c FROM {t}")  # noqa: E731
    assert (await (await count("downloads")).fetchone())["c"] == 1
    assert (await (await count("blocked_videos")).fetchone())["c"] == 1
    assert (await (await count("orders")).fetchone())["c"] == 1
    assert (await (await db.execute("SELECT 1 FROM users WHERE id=77")).fetchone())


# 6. Ограничение частоты запросов и «корзины» кнопок на человека

def test_rate_limiter():
    rl = RateLimiter(limit=3, window=600)
    assert [rl.allow(1, now=t) for t in (0, 1, 2, 3)] == [True, True, True, False]
    assert rl.allow(2, now=3) is True            # другой человек не страдает
    assert rl.allow(1, now=601) is True          # окно сдвинулось
    assert rl.allow(1, now=1203) is True
    for uid in range(5000):
        rl.allow(uid, now=5000)
    rl.allow(1, now=99999)
    assert len(rl._hits) < 100                   # старые записи вычищаются


def test_pending_cap_per_user(db, settings):
    ctx = Ctx(settings=settings, db=db, jobs=Jobs(1))
    info = VideoInfo("rutube", "a", "t", 1, [Option("720", "720p", 1)])
    other = core.put_pending(ctx, PendingLink("u", info, 2, time.time()))
    tokens = [core.put_pending(ctx, PendingLink("u", info, 1, time.time())) for _ in range(20)]
    alive = [t for t in tokens if core.get_pending(ctx, t)]
    assert len(alive) == core.PENDING_PER_USER and alive == tokens[-core.PENDING_PER_USER:]
    assert core.get_pending(ctx, other) is not None   # чужие кнопки не вытесняются
