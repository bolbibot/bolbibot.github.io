"""SQLite: подключение и схема."""
from __future__ import annotations

import os

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    credits INTEGER NOT NULL DEFAULT 0,
    unlimited_until TEXT,
    free_used INTEGER NOT NULL DEFAULT 0,
    free_date TEXT,
    accepted_terms INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    tariff TEXT NOT NULL,
    method TEXT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL,
    paid_at TEXT,
    tg_charge_id TEXT
);
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    site TEXT NOT NULL,
    video_id TEXT NOT NULL,
    quality TEXT NOT NULL,
    size INTEGER,
    paid_with TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS file_cache (
    site TEXT NOT NULL,
    video_id TEXT NOT NULL,
    quality TEXT NOT NULL,
    file_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY (site, video_id, quality)
);
CREATE TABLE IF NOT EXISTS stray_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    order_id INTEGER,
    charge_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    refunded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocked_videos (
    site TEXT NOT NULL,
    video_id TEXT NOT NULL,
    PRIMARY KEY (site, video_id)
);
"""


async def open_db(path: str) -> aiosqlite.Connection:
    if path != ":memory:":
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
    # isolation_level=None: транзакции открываем сами (BEGIN IMMEDIATE)
    db = await aiosqlite.connect(path, isolation_level=None)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.executescript(SCHEMA)
    return db
