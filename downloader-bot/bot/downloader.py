"""Обёртка над yt-dlp: что можно скачать, в каком качестве, и само скачивание."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import yt_dlp
import yt_dlp.utils

from .config import Settings

TARGETS = (360, 720, 1080)
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

    def best_at(height: int) -> dict:
        same = [f for f in videos if f["height"] == height]
        return max(same, key=lambda f: (_size(f, duration) or 0))

    options: list[Option] = []
    seen: set[int] = set()
    for target in TARGETS:
        heights = [f["height"] for f in videos if f["height"] <= target]
        if not heights or max(heights) in seen:
            continue
        height = max(heights)
        seen.add(height)
        options.append(Option(str(target), f"{height}p", video_size(best_at(height))))
    if videos and not options:  # например, вертикальное видео 1080×1920
        smallest = min(videos, key=lambda f: f["height"])
        options.append(Option("best", "Видео", video_size(smallest)))

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
    ("unavailable", ("video unavailable", "http error 404", "not found", "has been removed",
                     "does not exist", "no video formats", "not available", "geo")),
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

    warning = error = debug


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
        "allowed_extractors": ["default", "-generic"],  # без «скачай с любого адреса»
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
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=download) or {}
    except yt_dlp.utils.DownloadError as e:
        raise DownloadError(map_error(str(e)), str(e)) from e
    except DownloadError:
        raise
    except Exception as e:  # yt-dlp иногда бросает что попало
        raise DownloadError("unknown", repr(e)) from e


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
    return VideoInfo(
        site=site,
        video_id=str(info.get("id") or ""),
        title=str(info.get("title") or "Видео")[:200],
        duration=int(duration) if duration else None,
        options=options,
    )


async def download(s: Settings, url: str, site: str, quality: str, workdir: Path) -> Path:
    """Скачивает в workdir и возвращает путь к готовому файлу."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    opts = ydl_opts(s, site)
    opts.update({
        "format": format_selector(quality),
        "outtmpl": str(workdir / "%(id).80s.%(ext)s"),
        "max_filesize": s.max_filesize,
        "restrictfilenames": True,
        "overwrites": True,
        "concurrent_fragment_downloads": 4,
    })
    if quality == "audio":
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                   "preferredquality": "192"}]
    else:
        opts["merge_output_format"] = "mp4"
    await asyncio.to_thread(_extract, opts, url, True)

    files = [p for p in workdir.iterdir() if p.is_file() and not p.name.endswith(SKIP_SUFFIXES)]
    if not files:  # yt-dlp молча пропускает файл больше max_filesize
        raise DownloadError("too_big")
    result = max(files, key=lambda p: p.stat().st_size)
    if result.stat().st_size > s.max_filesize:
        raise DownloadError("too_big")
    return result
