"""Настройки бота: всё читается из переменных окружения."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

DEFAULT_HOSTS = (
    "youtube.com", "youtu.be", "vk.com", "vkvideo.ru", "rutube.ru", "tiktok.com",
    "dzen.ru", "ok.ru", "twitch.tv", "vimeo.com", "dailymotion.com",
)


@dataclass(frozen=True)
class Tariff:
    key: str
    title: str
    stars: int
    rub: int
    downloads: int = 0   # сколько скачиваний добавляет пакет
    days: int = 0        # на сколько дней даёт безлимит


@dataclass
class Settings:
    bot_token: str
    bot_api_url: str | None = None
    db_path: str = "data/bot.db"
    tmp_dir: str = "tmp"
    admin_ids: set[int] = field(default_factory=set)
    free_per_day: int = 1
    max_duration: int = 3 * 60 * 60
    max_filesize: int = 2_000_000_000
    parallel: int = 3
    allowed_hosts: tuple[str, ...] = DEFAULT_HOSTS
    required_channel: str | None = None
    bolbibot_url: str = "https://bolbibot.github.io/"
    proxy_default: str | None = None
    proxies: dict[str, str] = field(default_factory=dict)
    youtube_cookies: str | None = None
    robokassa_enabled: bool = False
    robokassa_login: str = ""
    robokassa_pass1: str = ""
    robokassa_pass2: str = ""
    robokassa_test: bool = False
    robokassa_algo: str = "md5"
    robokassa_receipt: bool = False
    web_port: int = 8080
    tariffs: dict[str, Tariff] = field(default_factory=dict)


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _opt(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def load_settings(env: Mapping[str, str]) -> Settings:
    token = (env.get("BOT_TOKEN") or "").strip()
    if not token:
        raise ValueError("BOT_TOKEN не задан")

    def num(name: str, default: int) -> int:
        raw = (env.get(name) or "").strip()
        return int(raw) if raw else default

    tariffs = {
        "p10": Tariff("p10", "10 скачиваний", num("PRICE_P10_STARS", 25),
                      num("PRICE_P10_RUB", 49), downloads=10),
        "p50": Tariff("p50", "50 скачиваний", num("PRICE_P50_STARS", 100),
                      num("PRICE_P50_RUB", 199), downloads=50),
        "unlim30": Tariff("unlim30", "Безлимит на 30 дней", num("PRICE_UNLIM30_STARS", 150),
                          num("PRICE_UNLIM30_RUB", 299), days=30),
    }
    proxies = {
        key[len("PROXY_"):].lower(): value.strip()
        for key, value in env.items()
        if key.startswith("PROXY_") and key != "PROXY_DEFAULT" and value.strip()
    }
    hosts = tuple(h.strip().lower() for h in (env.get("ALLOWED_HOSTS") or "").split(",") if h.strip())
    algo = (env.get("ROBOKASSA_ALGO") or "md5").strip().lower()
    if algo not in {"md5", "sha256", "sha512"}:
        raise ValueError("ROBOKASSA_ALGO: допустимы md5, sha256, sha512")

    s = Settings(
        bot_token=token,
        bot_api_url=_opt(env.get("BOT_API_URL")),
        db_path=_opt(env.get("DB_PATH")) or "data/bot.db",
        tmp_dir=_opt(env.get("TMP_DIR")) or "tmp",
        admin_ids={int(x) for x in (env.get("ADMIN_IDS") or "").replace(" ", "").split(",") if x},
        free_per_day=num("FREE_PER_DAY", 1),
        max_duration=num("MAX_DURATION", 3 * 60 * 60),
        max_filesize=num("MAX_FILESIZE", 2_000_000_000),
        parallel=num("PARALLEL", 3),
        allowed_hosts=hosts or DEFAULT_HOSTS,
        required_channel=_opt(env.get("REQUIRED_CHANNEL")),
        bolbibot_url=_opt(env.get("BOLBIBOT_URL")) or "https://bolbibot.github.io/",
        proxy_default=_opt(env.get("PROXY_DEFAULT")),
        proxies=proxies,
        youtube_cookies=_opt(env.get("YOUTUBE_COOKIES")),
        robokassa_enabled=_flag(env.get("ROBOKASSA_ENABLED")),
        robokassa_login=(env.get("ROBOKASSA_LOGIN") or "").strip(),
        robokassa_pass1=(env.get("ROBOKASSA_PASS1") or "").strip(),
        robokassa_pass2=(env.get("ROBOKASSA_PASS2") or "").strip(),
        robokassa_test=_flag(env.get("ROBOKASSA_TEST")),
        robokassa_algo=algo,
        robokassa_receipt=_flag(env.get("ROBOKASSA_RECEIPT")),
        web_port=num("WEB_PORT", 8080),
        tariffs=tariffs,
    )
    if s.robokassa_enabled and not (s.robokassa_login and s.robokassa_pass1 and s.robokassa_pass2):
        raise ValueError("ROBOKASSA_ENABLED=1, но не заданы ROBOKASSA_LOGIN/PASS1/PASS2")
    return s
