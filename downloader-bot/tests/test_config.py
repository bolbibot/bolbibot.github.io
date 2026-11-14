import pytest

from bot.config import load_settings


def test_defaults():
    s = load_settings({"BOT_TOKEN": "t"})
    assert s.free_per_day == 1
    assert s.tariffs["p10"].stars == 25 and s.tariffs["p10"].rub == 49
    assert s.tariffs["p10"].downloads == 10
    assert s.tariffs["p50"].stars == 100 and s.tariffs["p50"].rub == 199
    assert s.tariffs["unlim30"].stars == 150 and s.tariffs["unlim30"].rub == 299
    assert s.tariffs["unlim30"].days == 30
    assert s.robokassa_enabled is False
    assert s.max_duration == 10800 and s.max_filesize == 2_000_000_000
    assert s.parallel == 3
    assert "rutube.ru" in s.allowed_hosts and "youtu.be" in s.allowed_hosts
    assert s.required_channel is None and s.bot_api_url is None


def test_env_overrides():
    s = load_settings({
        "BOT_TOKEN": "t", "PROXY_YOUTUBE": "http://p:1", "PROXY_DEFAULT": "http://d:1",
        "ADMIN_IDS": "5, 7", "FREE_PER_DAY": "2", "ROBOKASSA_ENABLED": "1", "ROBOKASSA_LOGIN": "l",
        "ROBOKASSA_PASS1": "a", "ROBOKASSA_PASS2": "b",
        "PRICE_P10_STARS": "30", "ALLOWED_HOSTS": "a.com,b.com", "REQUIRED_CHANNEL": "@ch",
    })
    assert s.proxies["youtube"] == "http://p:1"
    assert s.proxy_default == "http://d:1"
    assert s.admin_ids == {5, 7}
    assert s.free_per_day == 2
    assert s.robokassa_enabled is True
    assert s.tariffs["p10"].stars == 30
    assert s.allowed_hosts == ("a.com", "b.com")
    assert s.required_channel == "@ch"


def test_token_required():
    with pytest.raises(ValueError):
        load_settings({})


def test_robokassa_needs_credentials():
    with pytest.raises(ValueError):
        load_settings({"BOT_TOKEN": "t", "ROBOKASSA_ENABLED": "1"})
