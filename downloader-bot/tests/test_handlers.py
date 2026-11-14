import asyncio
import time
from pathlib import Path

import pytest

from bot import billing, core, downloader, keyboards, texts
from bot.core import Ctx, PendingLink, process_download
from bot.downloader import DownloadError, Option, VideoInfo
from bot.jobs import Jobs
from bot.links import LinkError

INFO = VideoInfo("rutube", "abc", "Ролик <b>", 100,
                 [Option("360", "360p", 10), Option("720", "720p", 50), Option("audio", "MP3", 3)])


class Sender:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def __call__(self, source, kind, info):
        self.calls.append((source, kind))
        if self.fail:
            raise RuntimeError("telegram said no")
        return f"file-{len(self.calls)}"


@pytest.fixture
def ctx(db, settings):
    return Ctx(settings=settings, db=db, jobs=Jobs(3))


@pytest.fixture
def dl(monkeypatch):
    state = {"calls": 0, "error": None, "gate": None}

    async def fake(s, url, site, quality, workdir, selector=None):
        state["calls"] += 1
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        if state["gate"]:
            await state["gate"].wait()
        if state["error"]:
            (workdir / "junk.part").write_bytes(b"x")
            raise state["error"]
        path = workdir / "abc.mp4"
        path.write_bytes(b"video")
        return path

    monkeypatch.setattr(downloader, "download", fake)
    return state


async def noop(text):
    pass


def link(user_id=1):
    return PendingLink("https://rutube.ru/video/abc/", INFO, user_id, time.time())


def tmp_is_empty(ctx):
    root = Path(ctx.settings.tmp_dir)
    return not root.exists() or not any(root.iterdir())


async def credits_and_free(ctx, user_id=1):
    q = await billing.get_quota(ctx.db, ctx.settings, user_id)
    return q.credits, q.free_left


async def test_success(ctx, dl):
    send = Sender()
    assert await process_download(ctx, 1, link(), "720", send, noop) == "sent"
    assert send.calls[0][1] == "video" and isinstance(send.calls[0][0], Path)
    assert await credits_and_free(ctx) == (0, 0)
    row = await (await ctx.db.execute("SELECT * FROM downloads")).fetchone()
    assert (row["video_id"], row["quality"], row["paid_with"], row["size"]) == ("abc", "720", "free", 5)
    cache = await (await ctx.db.execute("SELECT * FROM file_cache")).fetchone()
    assert (cache["file_id"], cache["kind"]) == ("file-1", "video")
    assert tmp_is_empty(ctx)


async def test_audio_kind(ctx, dl):
    send = Sender()
    assert await process_download(ctx, 1, link(), "audio", send, noop) == "sent"
    assert send.calls[0][1] == "audio"


async def test_download_error_charges_nothing(ctx, dl):
    dl["error"] = DownloadError("unavailable")
    send = Sender()
    assert await process_download(ctx, 1, link(), "720", send, noop) == "error:unavailable"
    assert send.calls == []
    assert await credits_and_free(ctx) == (0, 1)
    assert tmp_is_empty(ctx)
    assert await (await ctx.db.execute("SELECT COUNT(*) c FROM downloads")).fetchone()
    assert (await (await ctx.db.execute("SELECT COUNT(*) c FROM downloads")).fetchone())["c"] == 0


async def test_send_error_charges_nothing(ctx, dl):
    assert await process_download(ctx, 1, link(), "720", Sender(fail=True), noop) == "error:send"
    assert await credits_and_free(ctx) == (0, 1)
    assert tmp_is_empty(ctx)
    # после сбоя пользователь не остаётся «занятым»
    assert await process_download(ctx, 1, link(), "720", Sender(), noop) == "sent"


async def test_unexpected_error(ctx, dl):
    dl["error"] = RuntimeError("disk full")
    assert await process_download(ctx, 1, link(), "720", Sender(), noop) == "error:unknown"
    assert await credits_and_free(ctx) == (0, 1)
    assert tmp_is_empty(ctx)


async def test_cached_video_skips_download_but_charges(ctx, dl):
    await ctx.db.execute("UPDATE users SET credits=5 WHERE id=1") if False else None
    await billing.ensure_user(ctx.db, 2)
    assert await process_download(ctx, 1, link(), "720", Sender(), noop) == "sent"
    send = Sender()
    assert await process_download(ctx, 2, link(2), "720", send, noop) == "sent"
    assert dl["calls"] == 1
    assert send.calls == [("file-1", "video")]
    assert await credits_and_free(ctx, 2) == (0, 0)
    # другое качество кэшем не покрыто
    await ctx.db.execute("UPDATE users SET credits=1 WHERE id=2")
    assert await process_download(ctx, 2, link(2), "360", Sender(), noop) == "sent"
    assert dl["calls"] == 2


async def test_stale_cache_falls_back_to_download(ctx, dl):
    await ctx.db.execute("INSERT INTO file_cache VALUES ('rutube','abc','720','dead','video')")

    class Flaky(Sender):
        async def __call__(self, source, kind, info):
            if source == "dead":
                self.calls.append((source, kind))
                raise RuntimeError("wrong file identifier")
            return await super().__call__(source, kind, info)

    send = Flaky()
    assert await process_download(ctx, 1, link(), "720", send, noop) == "sent"
    assert dl["calls"] == 1
    cache = await (await ctx.db.execute("SELECT file_id FROM file_cache")).fetchone()
    assert cache["file_id"] != "dead"


async def test_no_quota(ctx, dl):
    assert await process_download(ctx, 1, link(), "720", Sender(), noop) == "sent"
    assert await process_download(ctx, 1, link(), "360", Sender(), noop) == "no_quota"
    assert dl["calls"] == 1


async def test_double_tap_is_busy_and_charges_once(ctx, dl):
    await billing.ensure_user(ctx.db, 1)
    await ctx.db.execute("UPDATE users SET credits=5 WHERE id=1")
    dl["gate"] = asyncio.Event()
    first = asyncio.create_task(process_download(ctx, 1, link(), "720", Sender(), noop))
    await asyncio.sleep(0.01)
    assert await process_download(ctx, 1, link(), "720", Sender(), noop) == "busy"
    dl["gate"].set()
    assert await first == "sent"
    assert await credits_and_free(ctx) == (5, 0)


async def test_blocked(ctx, dl):
    await ctx.db.execute("INSERT INTO blocked_videos VALUES ('rutube','abc')")
    assert await process_download(ctx, 1, link(), "720", Sender(), noop) == "blocked"
    await ctx.db.execute("DELETE FROM blocked_videos")
    await billing.ensure_user(ctx.db, 1)
    await ctx.db.execute("UPDATE users SET blocked=1 WHERE id=1")
    assert await process_download(ctx, 1, link(), "720", Sender(), noop) == "blocked"
    assert dl["calls"] == 0


async def test_unknown_quality_and_foreign_link(ctx, dl):
    assert await process_download(ctx, 1, link(), "9999", Sender(), noop) == "error:unknown"
    assert await process_download(ctx, 2, link(1), "720", Sender(), noop) == "error:unknown"
    assert dl["calls"] == 0


def test_pending_store(ctx):
    token = core.put_pending(ctx, link())
    assert core.get_pending(ctx, token).info is INFO
    assert core.get_pending(ctx, "nope") is None
    old = PendingLink("u", INFO, 1, time.time() - 4000)
    stale = core.put_pending(ctx, old)
    assert core.get_pending(ctx, stale) is None
    for _ in range(1100):
        core.put_pending(ctx, link())
    assert len(ctx.pending) <= 1000


def test_keyboards(settings):
    def callbacks(kb):
        return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]

    assert not any("rk" in c for c in callbacks(keyboards.tariffs_kb(settings)))
    assert len(callbacks(keyboards.tariffs_kb(settings))) == 3
    settings.robokassa_enabled = True
    cbs = callbacks(keyboards.tariffs_kb(settings))
    assert "buy:rk:p10" in cbs and "buy:stars:unlim30" in cbs and len(cbs) == 6
    q = callbacks(keyboards.quality_kb("tok", INFO.options))
    assert q == ["q:tok:360", "q:tok:720", "q:tok:audio"]
    assert all(len(c.encode()) <= 64 for c in q)
    urls = [b.url for row in keyboards.promo_kb(settings).inline_keyboard for b in row]
    assert urls == [settings.bolbibot_url]


def test_texts_cover_every_code(settings):
    for code in ["not_url", "bad_scheme", "not_allowed", "unavailable", "private", "too_long",
                 "too_big", "live", "playlist", "blocked_by_site", "unsupported", "unknown", "send"]:
        assert len(texts.error(code)) > 10
    assert texts.error("whatever") == texts.error("unknown")
    assert "&lt;b&gt;" in texts.choose_quality(INFO)  # заголовок экранируется
    assert "1:40" in texts.choose_quality(INFO)
    assert LinkError("not_url").code == "not_url"
    for t in settings.tariffs.values():
        assert t.title in texts.paid(t)


# --- оплата звёздами и мелочи обработчиков ---

from bot import handlers  # noqa: E402


async def test_order_payable(ctx):
    s, db = ctx.settings, ctx.db
    oid = await billing.create_order(db, 1, s.tariffs["p10"], "stars")
    order = await billing.get_order(db, oid)
    assert handlers.order_payable(order, 1, 25, "XTR")
    assert not handlers.order_payable(order, 2, 25, "XTR")      # чужой заказ
    assert not handlers.order_payable(order, 1, 1, "XTR")       # не та сумма
    assert not handlers.order_payable(order, 1, 25, "RUB")
    assert not handlers.order_payable(None, 1, 25, "XTR")
    rk = await billing.get_order(db, await billing.create_order(db, 1, s.tariffs["p10"], "robokassa"))
    assert not handlers.order_payable(rk, 1, 49, "XTR")
    await billing.credit_order(db, s, oid, 25)
    assert not handlers.order_payable(await billing.get_order(db, oid), 1, 25, "XTR")


def test_parse_payload():
    assert handlers.parse_payload("order:12") == 12
    for bad in ["order:", "order:x", "12", "", "order:-1", None]:
        assert handlers.parse_payload(bad) is None


def test_filename():
    assert handlers.filename("Мой ролик: тест/1?", ".mp4") == "Мой ролик тест1.mp4"
    assert handlers.filename("///", ".mp3") == "video.mp3"
    assert len(handlers.filename("я" * 300, ".mp4")) <= 84


def test_router_builds(ctx):
    router = handlers.build_router(ctx)
    assert router.message.handlers and router.callback_query.handlers
    assert router.pre_checkout_query.handlers


async def test_video_size(tmp_path):
    import subprocess
    path = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    assert await downloader.video_size(path) == (320, 240)
    assert await downloader.video_size(tmp_path / "missing.mp4") is None


def test_jobs_introspection():
    jobs = Jobs(1)
    assert not jobs.has(1) and not jobs.full
