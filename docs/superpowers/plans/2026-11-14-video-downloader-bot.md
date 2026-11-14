# Video Downloader Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegram bot that downloads a video by link and sells downloads for Telegram Stars and (optionally) rubles via Robokassa.

**Architecture:** One asyncio Python process: aiogram bot, in-memory download queue, aiohttp listener for Robokassa. SQLite for users, orders, downloads and file_id cache. Pure-logic modules (billing, links, robokassa signature, format choice) are separated from Telegram handlers so they are unit-tested without a network.

**Tech Stack:** Python 3.12, aiogram 3, yt-dlp, ffmpeg, aiosqlite, aiohttp, pytest + pytest-asyncio, Docker Compose, `aiogram/telegram-bot-api` image, Caddy.

**Spec:** `docs/superpowers/specs/2026-11-14-video-downloader-bot-design.md`

## Global Constraints

- All code lives in `downloader-bot/`; nothing outside it changes except `docs/`.
- No secrets in git: only `.env.example`; `.env`, `data/`, `tmp/` are git-ignored.
- All user-facing text is Russian and lives only in `bot/texts.py`.
- Prices and limits come from config, defaults: free 1/day; packs `p10` 25⭐/49 ₽ (10), `p50` 100⭐/199 ₽ (50), `unlim30` 150⭐/299 ₽ (30 days).
- Free limit resets at midnight Europe/Moscow.
- Charge order: active unlimited → free today → purchased. Charge only after the file was sent.
- Limits: duration ≤ 3 h, file ≤ 2 GB, 1 job per user, 3 parallel downloads.
- Allowed sites by default: youtube.com, youtu.be, vk.com, vkvideo.ru, rutube.ru, tiktok.com, dzen.ru, ok.ru, twitch.tv, vimeo.com, dailymotion.com (subdomains included).
- `ROBOKASSA_ENABLED` defaults to off; with it off no HTTP listener starts and no card button is shown.
- Temp files are deleted after send and on any error.

## Review Focus

1. Robokassa (or Telegram) delivers the same payment notification twice → credited once.
2. Download fails or the bot cannot send the file → nothing is charged, temp files removed, user gets a readable message.
3. User taps a quality button twice / sends a second link while one job runs → second request refused, no double charge.
4. Link to a host outside the allowlist, a look-alike host (`youtube.com.evil.ru`), non-http scheme, or an IP address → refused before yt-dlp is called.
5. Robokassa notification with a valid signature but an amount different from the order → rejected, not credited.

---

## File Structure

```
downloader-bot/
  bot/__init__.py
  bot/config.py        Settings (env), Tariff table
  bot/db.py            connection + schema
  bot/billing.py       quota, charging, order creation, idempotent crediting, refund
  bot/links.py         URL validation, site key
  bot/downloader.py    yt-dlp wrapper: probe, format choice, download, error mapping
  bot/jobs.py          per-user lock + global semaphore queue
  bot/robokassa.py     signature, payment URL, result verification
  bot/web.py           aiohttp app: POST /robokassa/result
  bot/texts.py         all messages
  bot/keyboards.py     inline keyboards
  bot/handlers.py      aiogram routers (start, link, quality, pay, admin)
  bot/main.py          wiring and startup
  tests/               one test file per logic module
  requirements.txt, requirements-dev.txt, pytest.ini
  Dockerfile, docker-compose.yml, Caddyfile, .env.example, .gitignore, README.md
```

---

### Task 1: Scaffold, config, database

**Files:** Create `requirements*.txt`, `pytest.ini` (`asyncio_mode=auto`), `.gitignore`, `bot/__init__.py`, `bot/config.py`, `bot/db.py`; Test `tests/test_config.py`, `tests/test_db.py`.

**Interfaces — Produces:**
- `@dataclass(frozen=True) Tariff(key: str, title: str, stars: int, rub: int, downloads: int, days: int)`
- `@dataclass Settings` with fields: `bot_token, bot_api_url|None, db_path, tmp_dir, admin_ids: set[int], free_per_day=1, max_duration=10800, max_filesize=2_000_000_000, parallel=3, allowed_hosts: tuple[str,...], required_channel|None, bolbibot_url, proxy_default|None, proxies: dict[str,str], youtube_cookies|None, robokassa_enabled=False, robokassa_login, robokassa_pass1, robokassa_pass2, robokassa_test=False, robokassa_algo="md5", robokassa_receipt=False, web_port=8080, tariffs: dict[str,Tariff]`
- `load_settings(env: Mapping[str,str]) -> Settings` (`PROXY_<SITE>` vars fill `proxies` keyed by lowercase site key; `ADMIN_IDS` comma-separated; missing `BOT_TOKEN` raises `ValueError`).
- `async open_db(path: str) -> aiosqlite.Connection` creating tables `users(id PK, created_at, credits INT DEFAULT 0, unlimited_until TEXT NULL, free_used INT DEFAULT 0, free_date TEXT NULL, accepted_terms INT DEFAULT 0, blocked INT DEFAULT 0)`, `orders(id INTEGER PK AUTOINCREMENT, user_id, tariff, method, amount INT, status TEXT DEFAULT 'new', created_at, paid_at NULL, tg_charge_id NULL)`, `downloads(id PK, user_id, site, video_id, quality, size, paid_with, created_at)`, `file_cache(site, video_id, quality, file_id, kind, PRIMARY KEY(site, video_id, quality))`, `blocked_videos(site, video_id, PRIMARY KEY(site, video_id))`. Row factory `aiosqlite.Row`, WAL mode.

- [ ] Step 1: tests — defaults match Global Constraints (`free_per_day == 1`, `tariffs["p10"].stars == 25`, `tariffs["unlim30"].rub == 299`, `robokassa_enabled is False`); `PROXY_YOUTUBE=x` → `proxies["youtube"] == "x"`; no token → `ValueError`; `open_db(":memory:")` twice-safe and all five tables exist.
- [ ] Step 2: run `pytest -q` in `downloader-bot/` → fails (modules missing).
- [ ] Step 3: implement.
- [ ] Step 4: `pytest -q` → pass.
- [ ] Step 5: commit.

### Task 2: Billing

**Files:** Create `bot/billing.py`; Test `tests/test_billing.py`.

**Interfaces — Consumes:** `Settings`, `Tariff`, db connection. **Produces:**
- `moscow_today(now: datetime|None=None) -> str` (ISO date in Europe/Moscow)
- `async ensure_user(db, user_id: int) -> None`
- `@dataclass Quota(unlimited_until: datetime|None, free_left: int, credits: int)` with property `source -> Literal["unlimited","free","credits"]|None`
- `async get_quota(db, s: Settings, user_id, now=None) -> Quota`
- `async charge(db, s, user_id, now=None) -> str|None` — atomically consumes one unit in the constraint order, returns the source or `None` when nothing is available.
- `async create_order(db, user_id, tariff: Tariff, method: Literal["stars","robokassa"]) -> int` (amount = stars or rub)
- `async credit_order(db, s, order_id: int, paid_amount: int, tg_charge_id: str|None=None, now=None) -> Literal["credited","already","mismatch","unknown"]` — one transaction: `UPDATE orders SET status='paid' … WHERE id=? AND status='new'`; credit only when rowcount is 1. Packs add `downloads` to `credits`; `unlim30` sets `unlimited_until = max(now, current) + days`.
- `async refund_order(db, s, order_id) -> bool` — paid → 'refunded', reverses what was credited (credits floor at 0; unlimited shortened by `days`).
- `async record_download(db, user_id, site, video_id, quality, size, paid_with) -> None`

- [ ] Step 1: tests — new user: `free_left == 1`; after one `charge` returns `"free"` then `None`; next Moscow day free is back (pass `now` either side of 21:00 UTC); with credits and free both present, `"free"` goes first; with unlimited active, `charge` returns `"unlimited"` and leaves free/credits untouched; `credit_order` on `p10` adds 10; second call returns `"already"` and credits stay 10; wrong amount → `"mismatch"`, status stays `new`; unknown id → `"unknown"`; `unlim30` twice → 60 days ahead; refund of `p10` removes 10; two concurrent `charge` calls (`asyncio.gather`) with exactly one unit left → exactly one succeeds.
- [ ] Step 2: run → fail. Step 3: implement. Step 4: run → pass. Step 5: commit.

### Task 3: Link validation

**Files:** Create `bot/links.py`; Test `tests/test_links.py`.

**Produces:** `class LinkError(Exception)` with `.code in {"not_url","bad_scheme","not_allowed"}`; `extract_url(text: str) -> str|None` (first http(s) URL in a message); `check_url(url: str, allowed_hosts: Iterable[str]) -> str` returning the site key (`youtu.be`→`youtube`, `vkvideo.ru`→`vk`, otherwise second-level label: `rutube`, `tiktok`, `dzen`, `ok`, `twitch`, `vimeo`, `dailymotion`).

- [ ] Step 1: tests — `https://www.youtube.com/watch?v=x` → `"youtube"`; `https://m.vk.com/video1_2` → `"vk"`; `https://youtube.com.evil.ru/x`, `https://evilyoutube.com/x`, `http://127.0.0.1/x`, `http://[::1]/`, `ftp://rutube.ru/x`, `file:///etc/passwd`, `https://user@rutube.ru@evil.ru/` → `LinkError`; URL with port other than 80/443 → `LinkError`; `extract_url("смотри https://rutube.ru/video/abc/ вот")` returns the URL; plain text → `None`.
- [ ] Steps 2–5 as above.

### Task 4: Downloader

**Files:** Create `bot/downloader.py`; Test `tests/test_downloader.py`.

**Interfaces — Consumes:** `Settings`. **Produces:**
- `class DownloadError(Exception)` with `.code in {"unavailable","private","too_long","too_big","live","playlist","blocked_by_site","unknown"}`
- `@dataclass Option(quality: str, label: str, size: int|None)` — `quality in {"360","720","1080","audio"}`
- `@dataclass VideoInfo(site: str, video_id: str, title: str, duration: int|None, options: list[Option])`
- `build_options(info: dict, max_filesize: int) -> list[Option]` — pure; offers a height only if a format with `height >=` that value exists (one fallback option at the best available height if none of 360/720/1080 match), estimates size from `filesize`/`filesize_approx`/`tbr*duration`, drops options over the limit, always appends `audio` when an audio stream exists.
- `format_selector(quality: str) -> str` — `"bv*[height<=N][ext=mp4]+ba[ext=m4a]/b[height<=N][ext=mp4]/bv*[height<=N]+ba/b[height<=N]"`; audio → `"ba/b"`.
- `map_error(message: str) -> str` — yt-dlp message → code (`Private video`→private, `Sign in to confirm`→blocked_by_site, `Video unavailable`/`404`→unavailable, else unknown).
- `ydl_opts(s: Settings, site: str) -> dict` — `noplaylist`, `quiet`, `socket_timeout=30`, `allow_unplayable_formats=False`, `allowed_extractors` = default set **without** `generic`, proxy from `s.proxies.get(site) or s.proxy_default`, `cookiefile` only for youtube when set.
- `async probe(s, url, site) -> VideoInfo` — `extract_info(download=False)` in a thread; raises `live`, `playlist`, `too_long`.
- `async download(s, url, site, quality, workdir: Path) -> Path` — in a thread; `merge_output_format="mp4"`, audio via `FFmpegExtractAudio` mp3 192; `max_filesize` enforced; verifies resulting size ≤ limit.

- [ ] Step 1: tests (no network) — `build_options` on a fixture dict with heights 144/360/720 yields `360, 720, audio` and not `1080`; an option above `max_filesize` is dropped; `format_selector("720")` contains `height<=720`; `map_error` table above; `ydl_opts(s,"youtube")` has `noplaylist is True`, no `"generic"` in `allowed_extractors`, uses the youtube proxy over default, includes `cookiefile` only for youtube; `probe` with monkeypatched `YoutubeDL` returning `is_live=True` → `DownloadError("live")`, `duration=20000` → `too_long`, `_type="playlist"` → `playlist`.
- [ ] Steps 2–5 as above.

### Task 5: Job queue

**Files:** Create `bot/jobs.py`; Test `tests/test_jobs.py`.

**Produces:** `class Busy(Exception)`; `class Jobs(parallel: int)` with `waiting -> int` and `@asynccontextmanager slot(user_id: int)` — raises `Busy` immediately if the user already holds or awaits a slot, otherwise waits on the global semaphore; always releases.

- [ ] Step 1: tests — second `slot` for the same user raises `Busy`; after exit the user can enter again, also after an exception inside; with `parallel=1` a second user waits (`waiting == 1`) and proceeds after the first leaves.
- [ ] Steps 2–5 as above.

### Task 6: Robokassa

**Files:** Create `bot/robokassa.py`, `bot/web.py`; Test `tests/test_robokassa.py`, `tests/test_web.py`.

**Interfaces — Consumes:** `Settings`, `credit_order`. **Produces:**
- `sign(parts: Sequence[str], algo: str) -> str` (hex of `":".join(parts)`; algo md5/sha256/sha512)
- `payment_url(s, order_id: int, amount_rub: int, description: str) -> str` — `https://auth.robokassa.ru/Merchant/Index.aspx` with `MerchantLogin, OutSum ("49.00"), InvId, Description, SignatureValue = sign([login, OutSum, InvId, (Receipt,) pass1])`, `IsTest=1` in test mode, `Receipt` (URL-encoded JSON, one item, `tax:"none"`, `payment_method:"full_payment"`, `payment_object:"service"`) only when `robokassa_receipt`.
- `verify_result(s, out_sum: str, inv_id: str, signature: str) -> bool` — `sign([out_sum, inv_id, pass2])`, case-insensitive, `hmac.compare_digest`.
- `make_app(s, db, notify: Callable[[int, int], Awaitable[None]]) -> aiohttp.web.Application` — `POST` and `GET /robokassa/result`: bad signature → 403; `credit_order` with `int(round(float(OutSum)))` → `"credited"`: call `notify(user_id, order_id)`, respond `OK{InvId}`; `"already"` → `OK{InvId}` without notify; `"mismatch"`/`"unknown"` → 400. `GET /healthz` → `ok`.

- [ ] Step 1: tests — `sign(["a","b"],"md5") == hashlib.md5(b"a:b").hexdigest()`; `payment_url` round-trips: parse query, recompute signature; `IsTest` present only in test mode; `verify_result` accepts upper-case signature, rejects one built with pass1; web (aiohttp test client, in-memory db): valid notification credits and returns `OK<id>` and calls notify once; same notification again → `OK<id>`, credits unchanged, notify still once; amount `48.00` for a 49 ₽ order → 400 and order stays `new`; bad signature → 403.
- [ ] Steps 2–5 as above.

### Task 7: Texts, keyboards, handlers, wiring

**Files:** Create `bot/texts.py`, `bot/keyboards.py`, `bot/handlers.py`, `bot/main.py`; Test `tests/test_handlers.py`.

**Interfaces — Consumes:** everything above. **Produces:**
- `texts.py`: constants and small formatters (`welcome`, `terms`, `choose_quality(title, duration)`, `limit_reached`, `after_download(quota)`, `error(code)` covering every `LinkError`/`DownloadError` code, `paid(tariff)`, `busy`, `queued(n)`, `need_channel`).
- `keyboards.py`: `quality_kb(token, options)`, `tariffs_kb(s)` (card buttons only when `robokassa_enabled`), `promo_kb(s)`.
- `handlers.py`: `build_router(ctx) -> Router`, where `ctx` holds `settings, db, jobs, pending: dict[str, PendingLink]` (short token → url/site/info, capped at 1000 entries, 1 h TTL).
- Core function, tested without Telegram: `async process_download(ctx, user_id, pending, quality, send: Sender, status: Callable[[str], Awaitable]) -> str` returning an outcome code. Order inside: blocked user/video check → quota check (`None` → `"no_quota"`) → `jobs.slot` (`Busy` → `"busy"`) → cached `file_id` send or `download` + send → `charge` → `record_download` → cache `file_id` → cleanup in `finally`. `Sender` is a protocol `async (path|file_id, kind, title) -> file_id` so tests pass a fake.
- Handlers: `/start` (ensure user, welcome + promo), `/help`, `/terms`, `/balance`, text with link (channel check via `get_chat_member` when `required_channel`; validate → probe → quality keyboard), `q:<token>:<quality>` callback, `buy:<method>:<tariff>` callback (stars: `send_invoice` currency `XTR`, payload `order:<id>`; robokassa: URL button), `pre_checkout_query` (ok only if order exists and is `new`), `successful_payment` (`credit_order` with `total_amount` and `telegram_payment_charge_id`), admin-only `/stats`, `/refund <order_id>` (stars: `refund_star_payment` then `refund_order`), `/block <user_id|url>`.
- `main.py`: load settings, open db, `Bot` with `TelegramAPIServer.from_base(bot_api_url, is_local=True)` when set, start polling; when `robokassa_enabled` also run `web.make_app` on `web_port`.

- [ ] Step 1: tests for `process_download` with fake sender and monkeypatched `download` — success: outcome `"sent"`, one unit charged, `downloads` row, cache row, workdir empty; `download` raises `DownloadError("unavailable")` → outcome `"error:unavailable"`, nothing charged, workdir removed; sender raises → nothing charged, workdir removed; second call for a cached video does not call `download` and charges one unit; no quota → `"no_quota"` and `download` not called; two concurrent calls for one user → one `"busy"`, one unit charged; blocked video → `"blocked"`. Also: `tariffs_kb` has no `robokassa` callback when disabled; `texts.error` returns non-empty text for every code.
- [ ] Step 2: run → fail. Step 3: implement. Step 4: run whole suite → pass; `python -c "import bot.main"` succeeds.
- [ ] Step 5: commit.

### Task 8: Deployment and README

**Files:** Create `Dockerfile` (python:3.12-slim + ffmpeg, non-root user), `docker-compose.yml` (services `bot`, `bot-api` = `aiogram/telegram-bot-api` sharing a volume with `bot` so local-mode file paths resolve, `caddy` under compose profile `robokassa`), `Caddyfile` (reverse proxy `{$DOMAIN}` → `bot:8080`, only `/robokassa/*`), `.env.example` (every variable with a Russian comment), `README.md` (Russian: BotFather, api_id/api_hash, VPS, запуск, включение Robokassa with Result URL `https://<домен>/robokassa/result` and method POST, proxies/cookies, admin commands, **список приёмки**: free download, limit screen, Stars purchase and `/refund`, file > 50 МБ, Robokassa test payment, repeated notification).

- [ ] Step 1: `docker compose config -q` passes with `.env.example` copied to `.env` (skip with a note if Docker is unavailable).
- [ ] Step 2: with network available, run a real `probe` + 360p `download` against one public Rutube or Vimeo URL and record the result in the final report; if the sandbox blocks it, say so.
- [ ] Step 3: full `pytest -q` → pass. Commit.
