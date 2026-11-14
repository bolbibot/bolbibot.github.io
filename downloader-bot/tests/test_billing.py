import asyncio
from datetime import datetime, timedelta, timezone

from bot import billing


def utc(y, m, d, h=12, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


NOW = utc(2026, 11, 14)


def test_moscow_today_boundary():
    assert billing.moscow_today(utc(2026, 11, 14, 20, 59)) == "2026-11-14"
    assert billing.moscow_today(utc(2026, 11, 14, 21, 0)) == "2026-11-15"


async def test_new_user_has_one_free(db, settings):
    await billing.ensure_user(db, 1)
    await billing.ensure_user(db, 1)
    q = await billing.get_quota(db, settings, 1, NOW)
    assert (q.free_left, q.credits, q.unlimited_until) == (1, 0, None)
    assert q.source == "free"


async def test_free_is_spent_and_returns_next_day(db, settings):
    await billing.ensure_user(db, 1)
    assert await billing.charge(db, settings, 1, utc(2026, 11, 14, 20, 0)) == "free"
    assert await billing.charge(db, settings, 1, utc(2026, 11, 14, 20, 30)) is None
    q = await billing.get_quota(db, settings, 1, utc(2026, 11, 14, 20, 59))
    assert q.source is None
    assert await billing.charge(db, settings, 1, utc(2026, 11, 14, 21, 1)) == "free"


async def test_free_goes_before_credits(db, settings):
    await billing.ensure_user(db, 1)
    await db.execute("UPDATE users SET credits=2 WHERE id=1")
    assert await billing.charge(db, settings, 1, NOW) == "free"
    assert await billing.charge(db, settings, 1, NOW) == "credits"
    q = await billing.get_quota(db, settings, 1, NOW)
    assert (q.free_left, q.credits) == (0, 1)


async def test_unlimited_does_not_touch_the_rest(db, settings):
    await billing.ensure_user(db, 1)
    until = (NOW + timedelta(days=1)).isoformat()
    await db.execute("UPDATE users SET credits=2, unlimited_until=? WHERE id=1", (until,))
    for _ in range(5):
        assert await billing.charge(db, settings, 1, NOW) == "unlimited"
    q = await billing.get_quota(db, settings, 1, NOW)
    assert (q.free_left, q.credits) == (1, 2)
    # истёкший безлимит не действует
    assert await billing.charge(db, settings, 1, NOW + timedelta(days=2)) == "free"


async def test_credit_order_once(db, settings):
    await billing.ensure_user(db, 1)
    oid = await billing.create_order(db, 1, settings.tariffs["p10"], "stars")
    assert await billing.credit_order(db, settings, oid, 25, "ch1", NOW) == "credited"
    assert await billing.credit_order(db, settings, oid, 25, "ch1", NOW) == "already"
    q = await billing.get_quota(db, settings, 1, NOW)
    assert q.credits == 10
    row = await (await db.execute("SELECT * FROM orders WHERE id=?", (oid,))).fetchone()
    assert row["status"] == "paid" and row["tg_charge_id"] == "ch1" and row["paid_at"]


async def test_credit_order_rub_amount(db, settings):
    await billing.ensure_user(db, 1)
    oid = await billing.create_order(db, 1, settings.tariffs["p50"], "robokassa")
    row = await (await db.execute("SELECT amount FROM orders WHERE id=?", (oid,))).fetchone()
    assert row["amount"] == 199
    assert await billing.credit_order(db, settings, oid, 198, None, NOW) == "mismatch"
    row = await (await db.execute("SELECT status FROM orders WHERE id=?", (oid,))).fetchone()
    assert row["status"] == "new"
    assert (await billing.get_quota(db, settings, 1, NOW)).credits == 0
    assert await billing.credit_order(db, settings, 999, 199, None, NOW) == "unknown"


async def test_unlimited_stacks(db, settings):
    await billing.ensure_user(db, 1)
    for _ in range(2):
        oid = await billing.create_order(db, 1, settings.tariffs["unlim30"], "stars")
        assert await billing.credit_order(db, settings, oid, 150, None, NOW) == "credited"
    q = await billing.get_quota(db, settings, 1, NOW)
    assert q.unlimited_until == NOW + timedelta(days=60)


async def test_concurrent_credit_only_once(db, settings):
    await billing.ensure_user(db, 1)
    oid = await billing.create_order(db, 1, settings.tariffs["p10"], "robokassa")
    res = await asyncio.gather(*[billing.credit_order(db, settings, oid, 49, None, NOW) for _ in range(5)])
    assert sorted(res) == ["already"] * 4 + ["credited"]
    assert (await billing.get_quota(db, settings, 1, NOW)).credits == 10


async def test_refund(db, settings):
    await billing.ensure_user(db, 1)
    oid = await billing.create_order(db, 1, settings.tariffs["p10"], "stars")
    assert await billing.refund_order(db, settings, oid) is False  # не оплачен
    await billing.credit_order(db, settings, oid, 25, "c", NOW)
    await billing.charge(db, settings, 1, NOW)  # free
    for _ in range(3):
        await billing.charge(db, settings, 1, NOW)  # 3 из купленных
    assert await billing.refund_order(db, settings, oid) is True
    assert (await billing.get_quota(db, settings, 1, NOW)).credits == 0  # не уходит в минус
    assert await billing.refund_order(db, settings, oid) is False

    oid = await billing.create_order(db, 1, settings.tariffs["unlim30"], "stars")
    await billing.credit_order(db, settings, oid, 150, "c", NOW)
    assert await billing.refund_order(db, settings, oid) is True
    assert (await billing.get_quota(db, settings, 1, NOW)).unlimited_until is None


async def test_concurrent_charge_last_unit(db, settings):
    await billing.ensure_user(db, 1)
    res = await asyncio.gather(*[billing.charge(db, settings, 1, NOW) for _ in range(4)])
    assert sorted(r or "" for r in res) == ["", "", "", "free"]


async def test_record_download(db):
    await billing.record_download(db, 1, "rutube", "abc", "720", 100, "free")
    row = await (await db.execute("SELECT * FROM downloads")).fetchone()
    assert (row["site"], row["video_id"], row["paid_with"]) == ("rutube", "abc", "free")
