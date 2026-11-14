"""Проверка присланных ссылок: только разрешённые сайты, никаких внутренних адресов."""
from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import urlsplit

SITE_ALIASES = {"youtu.be": "youtube", "vkvideo.ru": "vk"}
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def normalize_url(url: str, site: str) -> str:
    """Приводит ссылку к виду, который понимает yt-dlp."""
    if site == "rutube":
        return re.sub(r"(?i)^(https?://(?:www\.)?rutube\.ru)/shorts/", r"\1/video/", url)
    if site == "ok":
        return re.sub(r"(?i)^(https?://)m\.(ok\.ru/)", r"\1\2", url)
    return url


class LinkError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def extract_url(text: str) -> str | None:
    match = _URL_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(".,;:!?)]}»")


def check_url(url: str, allowed_hosts: Iterable[str]) -> str:
    """Возвращает ключ сайта (youtube, vk, rutube…) или бросает LinkError."""
    url = (url or "").strip()
    if "\\" in url or any(ch.isspace() for ch in url):
        raise LinkError("not_url")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise LinkError("not_url") from None
    if not parts.scheme:
        raise LinkError("not_url")
    if parts.scheme.lower() not in ("http", "https"):
        raise LinkError("bad_scheme")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise LinkError("not_allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise LinkError("not_url")
    if port not in (None, 80, 443):
        raise LinkError("not_allowed")
    if not _HOST_RE.match(host):  # отсекает IP-адреса, localhost и мусор
        raise LinkError("not_allowed")
    for allowed in allowed_hosts:
        allowed = allowed.lower()
        if host == allowed or host.endswith("." + allowed):
            return SITE_ALIASES.get(allowed, allowed.split(".")[-2])
    raise LinkError("not_allowed")
