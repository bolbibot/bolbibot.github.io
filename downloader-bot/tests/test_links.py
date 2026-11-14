import pytest

from bot.config import DEFAULT_HOSTS
from bot.links import LinkError, check_url, extract_url


@pytest.mark.parametrize("url,site", [
    ("https://www.youtube.com/watch?v=x", "youtube"),
    ("https://youtu.be/x", "youtube"),
    ("https://m.vk.com/video1_2", "vk"),
    ("https://vkvideo.ru/video-1_2", "vk"),
    ("https://rutube.ru/video/abc/", "rutube"),
    ("http://RUTUBE.RU/video/abc/", "rutube"),
    ("https://vm.tiktok.com/abc/", "tiktok"),
    ("https://dzen.ru/video/watch/1", "dzen"),
    ("https://ok.ru/video/1", "ok"),
    ("https://clips.twitch.tv/x", "twitch"),
    ("https://vimeo.com/1", "vimeo"),
    ("https://www.dailymotion.com/video/x", "dailymotion"),
    ("https://rutube.ru:443/video/abc/", "rutube"),
])
def test_allowed(url, site):
    assert check_url(url, DEFAULT_HOSTS) == site


@pytest.mark.parametrize("url", [
    "https://youtube.com.evil.ru/x",
    "https://evilyoutube.com/x",
    "http://127.0.0.1/x",
    "http://[::1]/",
    "http://localhost/x",
    "http://169.254.169.254/latest/meta-data",
    "ftp://rutube.ru/x",
    "file:///etc/passwd",
    "https://user@rutube.ru@evil.ru/",
    "https://rutube.ru:8080/x",
    "https://rutube.ru\\@evil.ru/",
    "rutube.ru/video/abc",
    "https:///x",
    "",
])
def test_refused(url):
    with pytest.raises(LinkError):
        check_url(url, DEFAULT_HOSTS)


def test_error_codes():
    with pytest.raises(LinkError) as e:
        check_url("ftp://rutube.ru/x", DEFAULT_HOSTS)
    assert e.value.code == "bad_scheme"
    with pytest.raises(LinkError) as e:
        check_url("https://example.com/x", DEFAULT_HOSTS)
    assert e.value.code == "not_allowed"


def test_extract_url():
    assert extract_url("смотри https://rutube.ru/video/abc/ вот") == "https://rutube.ru/video/abc/"
    assert extract_url("(https://youtu.be/x).") == "https://youtu.be/x"
    assert extract_url("просто текст") is None
    assert extract_url("") is None
