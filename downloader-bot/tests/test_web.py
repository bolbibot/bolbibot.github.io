import hashlib

import pytest

from bot import billing
from bot.web import make_app


def sig(out_sum, inv_id, password="pass2"):
    return hashlib.md5(f"{out_sum}:{inv_id}:{password}".encode()).hexdigest()


@pytest.fixture
async def env(aiohttp_client, db, settings):
    notified = []

    async def notify(user_id, order_id):
        notified.append((user_id, order_id))

    client = await aiohttp_client(make_app(settings, db, notify))
    await billing.ensure_user(db, 1)
    oid = await billing.create_order(db, 1, settings.tariffs["p10"], "robokassa")
    return client, db, settings, oid, notified


async def post(client, out_sum, inv_id, signature):
    return await client.post("/robokassa/result", data={
        "OutSum": out_sum, "InvId": str(inv_id), "SignatureValue": signature})


async def test_valid_notification_credits_once(env):
    client, db, settings, oid, notified = env
    for _ in range(2):  # Robokassa повторяет уведомление, пока не получит OK
        resp = await post(client, "49.000000", oid, sig("49.000000", oid))
        assert resp.status == 200
        assert await resp.text() == f"OK{oid}"
    assert (await billing.get_quota(db, settings, 1)).credits == 10
    assert notified == [(1, oid)]


async def test_get_also_works(env):
    client, db, settings, oid, notified = env
    resp = await client.get("/robokassa/result", params={
        "OutSum": "49.00", "InvId": str(oid), "SignatureValue": sig("49.00", oid).upper()})
    assert await resp.text() == f"OK{oid}"


async def test_wrong_amount_rejected(env):
    client, db, settings, oid, notified = env
    resp = await post(client, "48.00", oid, sig("48.00", oid))
    assert resp.status == 400
    assert (await billing.get_order(db, oid))["status"] == "new"
    assert (await billing.get_quota(db, settings, 1)).credits == 0
    assert notified == []
    resp = await post(client, "49.40", oid, sig("49.40", oid))  # не округляется до 49
    assert resp.status == 400


async def test_bad_signature_and_garbage(env):
    client, db, settings, oid, notified = env
    assert (await post(client, "49.00", oid, sig("49.00", oid, "pass1"))).status == 403
    assert (await post(client, "49.00", oid, "")).status == 403
    assert (await client.post("/robokassa/result", data={})).status == 403
    assert (await post(client, "abc", oid, sig("abc", oid))).status == 400
    assert (await post(client, "49.00", "x", sig("49.00", "x"))).status == 400
    assert (await post(client, "49.00", 999, sig("49.00", 999))).status == 400
    assert (await billing.get_order(db, oid))["status"] == "new"


async def test_stars_order_cannot_be_paid_by_robokassa(env):
    client, db, settings, oid, notified = env
    stars = await billing.create_order(db, 1, settings.tariffs["p10"], "stars")  # 25 звёзд
    resp = await post(client, "25.00", stars, sig("25.00", stars))
    assert resp.status == 400
    assert (await billing.get_order(db, stars))["status"] == "new"


async def test_notify_failure_still_ok(aiohttp_client, db, settings):
    async def notify(user_id, order_id):
        raise RuntimeError("telegram down")
    client = await aiohttp_client(make_app(settings, db, notify))
    oid = await billing.create_order(db, 1, settings.tariffs["p10"], "robokassa")
    resp = await post(client, "49.00", oid, sig("49.00", oid))
    assert await resp.text() == f"OK{oid}"


async def test_healthz(env):
    client = env[0]
    assert await (await client.get("/healthz")).text() == "ok"
