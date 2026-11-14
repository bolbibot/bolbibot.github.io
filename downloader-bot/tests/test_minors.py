"""Тесты на доработки после проверки кода (бывшие «отложенные мелочи»)."""
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yt_dlp

from bot import billing, core, downloader, links, main, texts
from bot.config import load_settings
from bot.core import process_download
from bot.downloader import DownloadError, build_options, map_error, ydl_opts
from bot.web import make_app
from tests.test_bot_flow import CHAT, NOW, USER, app  # noqa: F401
from tests.test_downloader import INFO as RAW_INFO, FakeYDL, fake  # noqa: F401
from tests.test_handlers import Sender, ctx, dl, link, noop  # noqa: F401

from aiogram.types import Message, SuccessfulPayment

URL = "https://rutube.ru/video/x/"


# 1. Лишний платёж звёздами возвращается автоматически

def paid_message(n, charge, amount=25, payload="order:1"):
    pay = SuccessfulPayment(currency="XTR", total_amount=amount, invoice_payload=payload,
                            telegram_payment_charge_id=charge, provider_payment_charge_id="")
    return Message(message_id=n, date=NOW, chat=CHAT, from_user=USER, successful_payment=pay)


async def test_second_charge_for_same_order_is_refunded(app):
    app.settings.admin_ids = {9}
    await billing.create_order(app.db, 1, app.settings.tariffs["p10"], "stars")
    await app.raw(message=paid_message(50, "ch-1"))
    await app.raw(message=paid_message(51, "ch-1"))      # то же сообщение повторно — не возврат
    assert app.session.of("RefundStarPayment") == []
    await app.raw(message=paid_message(52, "ch-2"))      # настоящий второй платёж
    refunds = app.session.of("RefundStarPayment")
    assert [(r.user_id, r.telegram_payment_charge_id) for r in refunds] == [(1, "ch-2")]
    assert (await billing.get_quota(app.db, app.settings, 1)).credits == 10
    row = await (await app.db.execute("SELECT * FROM stray_payments")).fetchone()
    assert (row["charge_id"], row["amount"], row["refunded"]) == ("ch-2", 25, 1)
    sent = app.session.of("SendMessage")
    assert sent[-2].text == texts.PAY_REFUNDED
    assert sent[-1].chat_id == 9 and "ch-2" in sent[-1].text   # администратор в курсе


async def test_uncredited_payment_is_kept_when_refund_fails(app):
    app.session.fail.add("RefundStarPayment")
    await app.raw(message=paid_message(50, "ch-9", payload="order:404"))
    row = await (await app.db.execute("SELECT * FROM stray_payments")).fetchone()
    assert (row["charge_id"], row["refunded"], row["user_id"]) == ("ch-9", 0, 1)
    assert texts.PAY_FAILED in [m.text for m in app.session.of("SendMessage")]


# 2. Тестовый режим Robokassa

def sig(out_sum, inv_id):
    return hashlib.md5(f"{out_sum}:{inv_id}:pass2".encode()).hexdigest()


async def test_test_notification_rejected_in_production(aiohttp_client, db, settings):
    async def notify(*a):
        pass
    client = await aiohttp_client(make_app(settings, db, notify))
    oid = await billing.create_order(db, 1, settings.tariffs["p10"], "robokassa")
    data = {"OutSum": "49.00", "InvId": str(oid), "SignatureValue": sig("49.00", oid), "IsTest": "1"}
    assert (await client.post("/robokassa/result", data=data)).status == 400
    assert (await billing.get_order(db, oid))["status"] == "new"
    settings.robokassa_test = True
    assert (await client.post("/robokassa/result", data=data)).status == 200


def test_startup_warnings(settings):
    assert main.startup_warnings(settings) == []
    settings.robokassa_enabled = settings.robokassa_test = True
    settings.admin_ids = set()
    warnings = main.startup_warnings(settings)
    assert any("ROBOKASSA_TEST" in w for w in warnings)
    assert any("ADMIN_IDS" in w for w in warnings)


# 3. Свой экземпляр cookies на каждую загрузку

async def test_cookie_file_is_copied_per_job(fake, tmp_path, monkeypatch):
    original = tmp_path / "yt.txt"
    original.write_text("# Netscape HTTP Cookie File\n")
    s = load_settings({"BOT_TOKEN": "t", "YOUTUBE_COOKIES": str(original)})
    seen = {}

    def run(self, url, download=False):
        used = Path(self.opts["cookiefile"])
        seen["path"], seen["text"] = used, used.read_text()
        used.write_text("испорчено")                 # yt-dlp перезаписывает файл при закрытии
        return RAW_INFO
    monkeypatch.setattr(FakeYDL, "extract_info", run)
    await downloader.probe(s, "https://youtu.be/x", "youtube")
    assert seen["path"] != original and seen["text"].startswith("# Netscape")
    assert not seen["path"].exists()
    assert original.read_text().startswith("# Netscape")


async def test_missing_cookie_file_is_ignored(fake, tmp_path):
    s = load_settings({"BOT_TOKEN": "t", "YOUTUBE_COOKIES": str(tmp_path / "nope.txt")})
    FakeYDL.info = RAW_INFO
    assert (await downloader.probe(s, "https://youtu.be/x", "youtube")).title == "T"


# 4. Кэш сбрасывается только при негодном file_id

async def test_cache_survives_unrelated_send_error(ctx, dl):
    await ctx.db.execute("INSERT INTO file_cache VALUES ('rutube','abc','720','good','video')")

    class Flood(Sender):
        async def __call__(self, source, kind, info):
            raise RuntimeError("Telegram server says - Flood control exceeded. Retry in 30 seconds")

    assert await process_download(ctx, 1, link(), "720", Flood(), noop) == "error:send"
    assert dl["calls"] == 0
    assert (await (await ctx.db.execute("SELECT file_id FROM file_cache")).fetchone())["file_id"] == "good"
    assert (await billing.get_quota(ctx.db, ctx.settings, 1)).free_left == 1


# 5. Ключ кэша включает раздел сайта; пустой идентификатор не принимается

async def test_video_id_has_extractor_and_is_required(settings, fake):
    FakeYDL.info = {**RAW_INFO, "extractor_key": "RutubeEmbed"}
    assert (await downloader.probe(settings, URL, "rutube")).video_id == "rutubeembed:abc"
    FakeYDL.info = {**RAW_INFO, "id": ""}
    with pytest.raises(DownloadError) as e:
        await downloader.probe(settings, URL, "rutube")
    assert e.value.code == "unavailable"


# 6. Вертикальное видео и выбор конкретного формата

def vfmt(fid, w, h, size, vcodec="avc1", acodec="none", **kw):
    return {"format_id": fid, "width": w, "height": h, "filesize": size, "vcodec": vcodec,
            "acodec": acodec, **kw}


AUDIO = {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "filesize": 1_000_000, "ext": "m4a"}


def test_vertical_video_uses_short_side():
    info = {"duration": 30, "formats": [
        vfmt("a", 576, 1024, 3_000_000), vfmt("b", 720, 1280, 6_000_000),
        vfmt("c", 1080, 1920, 12_000_000), AUDIO]}
    opts = build_options(info, 2_000_000_000)
    assert [(o.quality, o.label) for o in opts] == [("720", "720p"), ("1080", "1080p"), ("audio", "MP3")]
    assert opts[1].selector.startswith("c+") and opts[1].size == 13_000_000
    only_tall = {"duration": 30, "formats": [vfmt("c", 1080, 1920, 12_000_000, acodec="mp4a")]}
    opts = build_options(only_tall, 2_000_000_000)
    assert (opts[0].quality, opts[0].label) == ("1080", "1080p") and opts[0].selector.startswith("c/")


def test_h264_is_preferred_at_the_same_level():
    info = {"duration": 30, "formats": [
        vfmt("vp9", 1280, 720, 9_000_000, vcodec="vp09"), vfmt("avc", 1280, 720, 7_000_000), AUDIO]}
    opt = build_options(info, 2_000_000_000)[0]
    assert opt.selector.startswith("avc+") and opt.size == 8_000_000


async def test_download_uses_option_selector(settings, fake, tmp_path, monkeypatch):
    seen = {}

    def run(self, url, download=False):
        seen["format"] = self.opts["format"]
        (tmp_path / "abc.mp4").write_bytes(b"x")
        return RAW_INFO
    monkeypatch.setattr(FakeYDL, "extract_info", run)
    await downloader.download(settings, URL, "rutube", "720", tmp_path, selector="avc+140/avc")
    assert seen["format"].startswith("avc+140/avc/") and "height<=720" in seen["format"]
    with pytest.raises(ValueError):
        await downloader.download(settings, URL, "rutube", "720", tmp_path, selector="x;rm -rf")


async def test_process_download_passes_selector(ctx, monkeypatch):
    seen = {}

    async def fake_download(s, url, site, quality, workdir, selector=None):
        seen["selector"] = selector
        Path(workdir).mkdir(parents=True)
        (Path(workdir) / "a.mp4").write_bytes(b"x")
        return Path(workdir) / "a.mp4"
    monkeypatch.setattr(downloader, "download", fake_download)
    pending = link()
    pending.info.options[1].selector = "avc+140"
    try:
        assert await process_download(ctx, 1, pending, "720", Sender(), noop) == "sent"
    finally:
        pending.info.options[1].selector = None
    assert seen["selector"] == "avc+140"


# 7. «Сегодня» в статистике — по московскому времени

def test_moscow_day_start():
    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    assert billing.moscow_day_start_utc(now) == "2026-11-13T21:00:00+00:00"
    late = datetime(2026, 11, 14, 22, tzinfo=timezone.utc)
    assert billing.moscow_day_start_utc(late) == "2026-11-14T21:00:00+00:00"


# 8. Точное распознавание ошибок

@pytest.mark.parametrize("msg,code", [
    ("ERROR: ffmpeg not found. Please install", "unknown"),
    ("ERROR: Postprocessing: ffprobe and ffmpeg not found", "unknown"),
    ("ERROR: unable to open for writing: geometry.mp4", "unknown"),
    ("ERROR: [vk] This video is not available in your country", "unavailable"),
    ("ERROR: [x] The uploader has not made this video available in your country (geo restricted)", "unavailable"),
    ("ERROR: [rutube] abc: HTTP Error 404: Not Found", "unavailable"),
    ("ERROR: [vimeo] 1: Video not found", "unavailable"),
])
def test_map_error_is_precise(msg, code):
    assert map_error(msg) == code


# 9. Очистка временной папки не трогает чужое

def test_clean_tmp_only_removes_job_dirs(tmp_path):
    job = tmp_path / ("a" * 32)
    job.mkdir()
    (job / "x.mp4").write_bytes(b"x")
    (tmp_path / "important").mkdir()
    (tmp_path / "notes.txt").write_text("keep")
    core.clean_tmp(str(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["important", "notes.txt"]
    core.clean_tmp(str(tmp_path / "missing"))
    assert (tmp_path / "missing").is_dir()


def test_bot_api_image_is_pinnable():
    compose = Path(__file__).parent.parent.joinpath("docker-compose.yml").read_text()
    assert "aiogram/telegram-bot-api:${BOT_API_TAG:-latest}" in compose
    assert "BOT_API_TAG" in Path(__file__).parent.parent.joinpath(".env.example").read_text()


# 10. Только извлекатели разрешённых сайтов

def test_only_allowlisted_extractors_are_loaded(settings):
    opts = ydl_opts(settings, "rutube")
    assert "default" not in opts["allowed_extractors"]
    with yt_dlp.YoutubeDL(opts) as ydl:
        names = set(ydl._ies)
    assert {"Youtube", "VK", "Rutube", "TikTok", "TikTokVM", "ZenYandex", "Odnoklassniki",
            "TwitchClips", "Vimeo", "Dailymotion"} <= names
    assert not names & {"Generic", "Mediasite", "ZenPorn"}
    assert len(names) < 80


def test_custom_site_falls_back_to_default_extractors():
    s = load_settings({"BOT_TOKEN": "t", "ALLOWED_HOSTS": "rutube.ru,coub.com"})
    assert ydl_opts(s, "coub")["allowed_extractors"] == ["default", "-generic"]


# Ссылки, которые yt-dlp не понимает в исходном виде

@pytest.mark.parametrize("url,site,expected", [
    ("https://rutube.ru/shorts/c6cc4d620b1d4338901770a44b3e82f4/", "rutube",
     "https://rutube.ru/video/c6cc4d620b1d4338901770a44b3e82f4/"),
    ("https://m.ok.ru/video/20079905452", "ok", "https://ok.ru/video/20079905452"),
    ("https://rutube.ru/video/abc/?t=5", "rutube", "https://rutube.ru/video/abc/?t=5"),
    ("https://vimeo.com/1", "vimeo", "https://vimeo.com/1"),
])
def test_normalize_url(url, site, expected):
    assert links.normalize_url(url, site) == expected
    ies = yt_dlp.extractor.gen_extractor_classes()
    assert any(ie.suitable(expected) and ie.ie_key() != "Generic" for ie in ies) or "abc" in url
