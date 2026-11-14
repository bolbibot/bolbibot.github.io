"""HTTP-приёмник уведомлений Robokassa."""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable

from aiohttp import web

from . import billing, robokassa
from .config import Settings

log = logging.getLogger(__name__)


def make_app(s: Settings, db, notify: Callable[[int, int], Awaitable[None]]) -> web.Application:
    async def result(request: web.Request) -> web.Response:
        data = dict(request.query)
        if request.method == "POST":
            data.update(await request.post())
        out_sum = str(data.get("OutSum", ""))
        inv_id = str(data.get("InvId", ""))
        signature = str(data.get("SignatureValue", ""))
        if not robokassa.verify_result(s, out_sum, inv_id, signature):
            log.warning("robokassa: неверная подпись, InvId=%r", inv_id[:32])
            return web.Response(status=403, text="bad sign")
        try:
            amount = Decimal(out_sum)
            order_id = int(inv_id)
        except (InvalidOperation, ValueError):
            return web.Response(status=400, text="bad request")
        if amount != amount.to_integral_value():
            return web.Response(status=400, text="bad amount")

        outcome = await billing.credit_order(db, s, order_id, int(amount), method="robokassa")
        if outcome == "credited":
            order = await billing.get_order(db, order_id)
            try:
                await notify(order["user_id"], order_id)
            except Exception:  # деньги уже зачислены; сообщение не критично
                log.exception("robokassa: не удалось уведомить пользователя")
        elif outcome != "already":
            log.warning("robokassa: заказ %s отклонён (%s), сумма %s", order_id, outcome, out_sum)
            return web.Response(status=400, text="bad order")
        return web.Response(text=f"OK{order_id}")

    async def healthz(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application(client_max_size=64 * 1024)
    app.router.add_route("POST", "/robokassa/result", result)
    app.router.add_route("GET", "/robokassa/result", result)
    app.router.add_get("/healthz", healthz)
    return app
