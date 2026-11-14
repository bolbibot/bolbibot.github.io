import pytest

from bot import downloader
from bot.config import load_settings
from bot.downloader import DownloadError, build_options, format_selector, map_error, ydl_opts


def fmt(height, size, vcodec="avc1", acodec="none", **kw):
    return {"height": height, "filesize": size, "vcodec": vcodec, "acodec": acodec, **kw}


AUDIO = {"height": None, "filesize": 3_000_000, "vcodec": "none", "acodec": "mp4a"}
INFO = {"id": "abc", "title": "T", "duration": 100,
        "formats": [fmt(144, 1_000_000), fmt(360, 10_000_000), fmt(720, 50_000_000), AUDIO]}


def test_build_options_basic():
    opts = build_options(INFO, 2_000_000_000)
    assert [o.quality for o in opts] == ["360", "720", "audio"]
    assert [o.label for o in opts][:2] == ["360p", "720p"]
    assert opts[1].size == 53_000_000  # видео + звук
    assert opts[2].size == 3_000_000


def test_build_options_real_heights_and_dedupe():
    info = {"duration": 10, "formats": [fmt(480, 5_000_000, acodec="mp4a")]}
    opts = build_options(info, 2_000_000_000)
    assert [(o.quality, o.label) for o in opts] == [("720", "480p"), ("audio", "MP3")]


def test_build_options_drops_too_big():
    opts = build_options(INFO, 20_000_000)
    assert [o.quality for o in opts] == ["360", "audio"]


def test_build_options_size_estimate_from_bitrate():
    info = {"duration": 80, "formats": [
        {"height": 720, "vcodec": "avc1", "acodec": "mp4a", "tbr": 1000}]}
    opts = build_options(info, 2_000_000_000)
    assert opts[0].size == 10_000_000  # 1000 кбит/с * 80 с / 8


def test_build_options_fallback_and_no_formats():
    tall = {"duration": 10, "formats": [fmt(1920, 9_000_000, acodec="mp4a")]}
    opts = build_options(tall, 2_000_000_000)
    assert [(o.quality, o.label) for o in opts][0] == ("best", "Видео")
    assert build_options({"formats": []}, 1) == []
    only_audio = {"formats": [AUDIO]}
    assert [o.quality for o in build_options(only_audio, 2_000_000_000)] == ["audio"]


def test_format_selector():
    assert "height<=720" in format_selector("720")
    assert "height<=1080" in format_selector("1080")
    assert "height" not in format_selector("audio")
    with pytest.raises(ValueError):
        format_selector("../x")


@pytest.mark.parametrize("msg,code", [
    ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "private"),
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "blocked_by_site"),
    ("ERROR: [youtube] x: Video unavailable", "unavailable"),
    ("ERROR: HTTP Error 404: Not Found", "unavailable"),
    ("ERROR: HTTP Error 429: Too Many Requests", "blocked_by_site"),
    ("ERROR: Unsupported URL: https://x", "unsupported"),
    ("something odd", "unknown"),
])
def test_map_error(msg, code):
    assert map_error(msg) == code


def test_ydl_opts():
    s = load_settings({"BOT_TOKEN": "t", "PROXY_DEFAULT": "http://d", "PROXY_YOUTUBE": "http://y",
                       "YOUTUBE_COOKIES": "/c.txt"})
    yt = ydl_opts(s, "youtube")
    assert yt["noplaylist"] is True
    assert yt["allowed_extractors"] == ["default", "-generic"]
    assert yt["proxy"] == "http://y" and yt["cookiefile"] == "/c.txt"
    ru = ydl_opts(s, "rutube")
    assert ru["proxy"] == "http://d" and "cookiefile" not in ru
    assert "proxy" not in ydl_opts(load_settings({"BOT_TOKEN": "t"}), "vk")


class FakeYDL:
    info: dict = {}
    error: Exception | None = None

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        if FakeYDL.error:
            raise FakeYDL.error
        return FakeYDL.info


@pytest.fixture
def fake(monkeypatch):
    FakeYDL.error = None
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


async def probe_code(settings, info=None, error=None):
    FakeYDL.info, FakeYDL.error = info or {}, error
    with pytest.raises(DownloadError) as e:
        await downloader.probe(settings, "https://rutube.ru/video/x/", "rutube")
    return e.value.code


async def test_probe_ok(settings, fake):
    FakeYDL.info = INFO
    info = await downloader.probe(settings, "https://rutube.ru/video/x/", "rutube")
    assert (info.site, info.video_id, info.title, info.duration) == ("rutube", "abc", "T", 100)
    assert [o.quality for o in info.options] == ["360", "720", "audio"]


async def test_probe_refusals(settings, fake):
    assert await probe_code(settings, {**INFO, "is_live": True}) == "live"
    assert await probe_code(settings, {**INFO, "live_status": "is_upcoming"}) == "live"
    assert await probe_code(settings, {**INFO, "duration": 20000}) == "too_long"
    assert await probe_code(settings, {"_type": "playlist", "entries": []}) == "playlist"
    assert await probe_code(settings, {**INFO, "formats": []}) == "unavailable"
    big = {**INFO, "formats": [fmt(720, 3_000_000_000, acodec="mp4a")]}
    assert await probe_code(settings, big) == "too_big"
    assert await probe_code(settings, None, downloader.yt_dlp.utils.DownloadError("Private video")) == "private"
    assert await probe_code(settings, None, RuntimeError("boom")) == "unknown"


async def test_download_checks_result(settings, fake, tmp_path, monkeypatch):
    def run(self, url, download=False):
        (tmp_path / "abc.mp4").write_bytes(b"x" * 10)
        (tmp_path / "abc.f1.mp4.part").write_bytes(b"x" * 99)
        return INFO
    monkeypatch.setattr(FakeYDL, "extract_info", run)
    path = await downloader.download(settings, "https://rutube.ru/video/x/", "rutube", "720", tmp_path)
    assert path.name == "abc.mp4"

    settings.max_filesize = 5
    with pytest.raises(DownloadError) as e:
        await downloader.download(settings, "https://rutube.ru/video/x/", "rutube", "720", tmp_path)
    assert e.value.code == "too_big"

    monkeypatch.setattr(FakeYDL, "extract_info", lambda self, url, download=False: INFO)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(DownloadError) as e:
        await downloader.download(settings, "https://rutube.ru/video/x/", "rutube", "720", empty)
    assert e.value.code == "too_big"
