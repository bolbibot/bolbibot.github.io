"""Учёт скачиваний: бесплатный лимит, пакеты, безлимит, заказы."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Settings, Tariff

MOSCOW = ZoneInfo("Europe/Moscow")


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def moscow_today(now: datetime | None = None) -> str:
    return _now(now).astimezone(MOSCOW).date().isoformat()


@asynccontextmanager
async def _tx(db):
    """Одна пишущая транзакция за раз на соединение."""
    lock = getattr(db, "_tx_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        db._tx_lock = lock
    async with lock:
        await db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            await db.execute("ROLLBACK")
            raise
        else:
            await db.execute("COMMIT")


@dataclass
class Quota:
    unlimited_until: datetime | None
    free_left: int
    credits: int

    @property
    def source(self) -> str | None:
        if self.unlimited_until is not None:
            return "unlimited"
        if self.free_left > 0:
            return "free"
        if self.credits > 0:
            return "credits"
        return None


async def ensure_user(db, user_id: int) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users (id, created_at) VALUES (?, ?)",
        (user_id, _now(None).isoformat()),
    )


def _quota_from_row(row, s: Settings, now: datetime) -> Quota:
    until = datetime.fromisoformat(row["unlimited_until"]) if row["unlimited_until"] else None
    if until is not None and until <= now:
        until = None
    used = row["free_used"] if row["free_date"] == moscow_today(now) else 0
    return Quota(until, max(0, s.free_per_day - used), row["credits"])


async def _user_row(db, user_id: int):
    await ensure_user(db, user_id)
    cur = await db.execute("SELECT * FROM users WHERE id=?", (user_id,))
    return await cur.fetchone()


async def get_quota(db, s: Settings, user_id: int, now: datetime | None = None) -> Quota:
    return _quota_from_row(await _user_row(db, user_id), s, _now(now))


async def charge(db, s: Settings, user_id: int, now: datetime | None = None) -> str | None:
    """Списывает одно скачивание. Возвращает, чем оплачено, или None."""
    now = _now(now)
    async with _tx(db):
        row = await _user_row(db, user_id)
        quota = _quota_from_row(row, s, now)
        source = quota.source
        if source == "free":
            used = s.free_per_day - quota.free_left + 1
            await db.execute(
                "UPDATE users SET free_used=?, free_date=? WHERE id=?",
                (used, moscow_today(now), user_id),
            )
        elif source == "credits":
            await db.execute("UPDATE users SET credits=credits-1 WHERE id=?", (user_id,))
        return source


async def create_order(db, user_id: int, tariff: Tariff, method: str) -> int:
    if method not in ("stars", "robokassa"):
        raise ValueError(method)
    amount = tariff.stars if method == "stars" else tariff.rub
    await ensure_user(db, user_id)
    cur = await db.execute(
        "INSERT INTO orders (user_id, tariff, method, amount, created_at) VALUES (?,?,?,?,?)",
        (user_id, tariff.key, method, amount, _now(None).isoformat()),
    )
    return cur.lastrowid


async def get_order(db, order_id: int):
    cur = await db.execute("SELECT * FROM orders WHERE id=?", (order_id,))
    return await cur.fetchone()


async def credit_order(db, s: Settings, order_id: int, paid_amount: int,
                       tg_charge_id: str | None = None, now: datetime | None = None) -> str:
    """Зачисляет оплату ровно один раз: credited / already / mismatch / unknown."""
    now = _now(now)
    async with _tx(db):
        order = await get_order(db, order_id)
        if order is None:
            return "unknown"
        if order["status"] != "new":
            return "already"
        tariff = s.tariffs.get(order["tariff"])
        if tariff is None or int(paid_amount) != order["amount"]:
            return "mismatch"
        cur = await db.execute(
            "UPDATE orders SET status='paid', paid_at=?, tg_charge_id=? WHERE id=? AND status='new'",
            (now.isoformat(), tg_charge_id, order_id),
        )
        if cur.rowcount != 1:
            return "already"
        user = await _user_row(db, order["user_id"])
        if tariff.downloads:
            await db.execute("UPDATE users SET credits=credits+? WHERE id=?",
                             (tariff.downloads, order["user_id"]))
        if tariff.days:
            current = datetime.fromisoformat(user["unlimited_until"]) if user["unlimited_until"] else now
            until = max(now, current) + timedelta(days=tariff.days)
            await db.execute("UPDATE users SET unlimited_until=? WHERE id=?",
                             (until.isoformat(), order["user_id"]))
        return "credited"


async def refund_order(db, s: Settings, order_id: int) -> bool:
    """Отмечает заказ возвращённым и снимает начисленное. False, если заказ не оплачен."""
    async with _tx(db):
        cur = await db.execute(
            "UPDATE orders SET status='refunded' WHERE id=? AND status='paid'", (order_id,))
        if cur.rowcount != 1:
            return False
        order = await get_order(db, order_id)
        tariff = s.tariffs.get(order["tariff"])
        user = await _user_row(db, order["user_id"])
        if tariff and tariff.downloads:
            await db.execute("UPDATE users SET credits=MAX(0, credits-?) WHERE id=?",
                             (tariff.downloads, order["user_id"]))
        if tariff and tariff.days and user["unlimited_until"]:
            until = datetime.fromisoformat(user["unlimited_until"]) - timedelta(days=tariff.days)
            value = until.isoformat() if until > _now(None) else None
            await db.execute("UPDATE users SET unlimited_until=? WHERE id=?",
                             (value, order["user_id"]))
        return True


async def record_download(db, user_id: int, site: str, video_id: str, quality: str,
                          size: int | None, paid_with: str) -> None:
    await db.execute(
        "INSERT INTO downloads (user_id, site, video_id, quality, size, paid_with, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (user_id, site, video_id, quality, size, paid_with, _now(None).isoformat()),
    )
