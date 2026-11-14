"""Обработчики Telegram: команды, ссылки, кнопки качества, оплата, администрирование."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, LabeledPrice, Message,
                           PreCheckoutQuery)

from . import billing, downloader, keyboards, robokassa, texts
from .core import Ctx, PendingLink, get_pending, is_blocked, process_download, put_pending
from .downloader import DownloadError, VideoInfo
from .links import LinkError, check_url, extract_url

log = logging.getLogger(__name__)
SEND_TIMEOUT = 60 * 60


def parse_payload(payload: str | None) -> int | None:
    match = re.fullmatch(r"order:(\d+)", payload or "")
    return int(match.group(1)) if match else None


def order_payable(order, user_id: int, amount: int, currency: str) -> bool:
    return bool(
        order is not None and order["status"] == "new" and order["method"] == "stars"
        and order["user_id"] == user_id and order["amount"] == amount and currency == "XTR")


def filename(title: str, ext: str) -> str:
    name = re.sub(r"[^\w\s.,()\-]", "", title, flags=re.UNICODE)
    name = re.sub(r"\s+", " ", name).strip(" .")[:80]
    return (name or "video") + ext


def make_sender(bot: Bot, chat_id: int):
    async def send(source: Path | str, kind: str, info: VideoInfo) -> str:
        if isinstance(source, str):
            file, size = source, None
        else:
            file = FSInputFile(source, filename=filename(info.title, source.suffix))
            size = await downloader.video_size(source) if kind == "video" else None
        if kind == "audio":
            msg = await bot.send_audio(chat_id, file, title=info.title[:64],
                                       duration=info.duration, request_timeout=SEND_TIMEOUT)
            media = msg.audio or msg.document or msg.voice
        else:
            msg = await bot.send_video(
                chat_id, file, caption=texts.caption(info), duration=info.duration,
                width=size[0] if size else None, height=size[1] if size else None,
                supports_streaming=True, request_timeout=SEND_TIMEOUT)
            media = msg.video or msg.document or msg.animation
        return media.file_id if media else ""
    return send


def build_router(ctx: Ctx) -> Router:
    s, db = ctx.settings, ctx.db
    router = Router()
    router.message.filter(F.chat.type == "private")
    probing: set[int] = set()
    probe_sem = asyncio.Semaphore(5)

    def is_admin(user_id: int) -> bool:
        return user_id in s.admin_ids

    async def subscribed(bot: Bot, user_id: int) -> bool:
        try:
            member = await bot.get_chat_member(s.required_channel, user_id)
        except Exception:
            log.exception("не удалось проверить подписку на %s", s.required_channel)
            return True  # ошибка настройки канала не должна ломать бота
        if member.status in ("creator", "administrator", "member"):
            return True
        return member.status == "restricted" and getattr(member, "is_member", False)

    async def safe_edit(message: Message, text: str, **kwargs) -> None:
        try:
            await message.edit_text(text, **kwargs)
        except Exception:
            log.debug("сообщение не изменено", exc_info=True)

    # ---------- команды ----------

    @router.message(CommandStart())
    async def on_start(message: Message):
        await billing.ensure_user(db, message.from_user.id)
        await db.execute("UPDATE users SET accepted_terms=1 WHERE id=?", (message.from_user.id,))
        await message.answer(texts.welcome(s), reply_markup=keyboards.promo_kb(s))

    @router.message(Command("help"))
    async def on_help(message: Message):
        await message.answer(texts.HELP)

    @router.message(Command("terms"))
    async def on_terms(message: Message):
        await message.answer(texts.TERMS)

    @router.message(Command("paysupport"))
    async def on_paysupport(message: Message):
        await message.answer(texts.paysupport(message.from_user.id))

    @router.message(Command("balance"))
    async def on_balance(message: Message):
        quota = await billing.get_quota(db, s, message.from_user.id)
        await message.answer(texts.balance(quota, s), reply_markup=keyboards.tariffs_kb(s))

    # ---------- администратор ----------

    @router.message(Command("stats"))
    async def on_stats(message: Message):
        if not is_admin(message.from_user.id):
            return await message.answer(texts.ADMIN_ONLY)
        today = billing.moscow_today()

        async def one(sql: str, *args):
            return (await (await db.execute(sql, args)).fetchone())[0] or 0

        stars = await one("SELECT SUM(amount) FROM orders WHERE status='paid' AND method='stars'")
        rub = await one("SELECT SUM(amount) FROM orders WHERE status='paid' AND method='robokassa'")
        lines = [
            f"Пользователей: {await one('SELECT COUNT(*) FROM users')}",
            f"Скачиваний всего: {await one('SELECT COUNT(*) FROM downloads')}",
            f"Скачиваний за сегодня: {await one('SELECT COUNT(*) FROM downloads WHERE created_at >= ?', today)}",
            f"Оплачено звёздами: {stars} ⭐",
            f"Оплачено картой: {rub} ₽",
            f"В очереди сейчас: {ctx.jobs.waiting}",
        ]
        await message.answer("\n".join(lines))

    @router.message(Command("refund"))
    async def on_refund(message: Message, command: CommandObject, bot: Bot):
        if not is_admin(message.from_user.id):
            return await message.answer(texts.ADMIN_ONLY)
        if not (command.args or "").strip().isdigit():
            return await message.answer("Формат: /refund номер_заказа")
        order = await billing.get_order(db, int(command.args.strip()))
        if order is None or order["status"] != "paid":
            return await message.answer("Заказ не найден или не оплачен.")
        if order["method"] == "stars":
            try:
                await bot.refund_star_payment(
                    user_id=order["user_id"],
                    telegram_payment_charge_id=order["tg_charge_id"])
            except Exception as e:
                return await message.answer(f"Telegram отказал в возврате: {e}")
            note = "Звёзды возвращены."
        else:
            note = "Начисление снято. Деньги верните вручную в кабинете Robokassa."
        await billing.refund_order(db, s, order["id"])
        await message.answer(f"Заказ {order['id']}: {note}")

    @router.message(Command("block", "unblock"))
    async def on_block(message: Message, command: CommandObject):
        if not is_admin(message.from_user.id):
            return await message.answer(texts.ADMIN_ONLY)
        arg = (command.args or "").strip()
        flag = 1 if command.command == "block" else 0
        if arg.isdigit():
            await billing.ensure_user(db, int(arg))
            await db.execute("UPDATE users SET blocked=? WHERE id=?", (flag, int(arg)))
            return await message.answer("Готово.")
        try:
            site = check_url(arg, s.allowed_hosts)
            info = await downloader.probe(s, arg, site)
        except (LinkError, DownloadError) as e:
            return await message.answer(f"Формат: /{command.command} ID_пользователя или ссылка. ({e})")
        if flag:
            await db.execute("INSERT OR IGNORE INTO blocked_videos VALUES (?,?)",
                             (site, info.video_id))
            await db.execute("DELETE FROM file_cache WHERE site=? AND video_id=?",
                             (site, info.video_id))
        else:
            await db.execute("DELETE FROM blocked_videos WHERE site=? AND video_id=?",
                             (site, info.video_id))
        await message.answer("Готово.")

    # ---------- оплата ----------

    @router.callback_query(F.data.startswith("buy:"))
    async def on_buy(cb: CallbackQuery, bot: Bot):
        try:
            _, method, key = cb.data.split(":", 2)
        except ValueError:
            return await cb.answer()
        tariff = s.tariffs.get(key)
        if tariff is None:
            return await cb.answer(texts.EXPIRED, show_alert=True)
        user_id = cb.from_user.id
        if method == "stars":
            order_id = await billing.create_order(db, user_id, tariff, "stars")
            await bot.send_invoice(
                chat_id=user_id, title=tariff.title,
                description=texts.invoice_description(tariff), payload=f"order:{order_id}",
                currency="XTR", prices=[LabeledPrice(label=tariff.title, amount=tariff.stars)],
                provider_token="")
        elif method == "rk" and s.robokassa_enabled:
            order_id = await billing.create_order(db, user_id, tariff, "robokassa")
            url = robokassa.payment_url(s, order_id, tariff.rub, tariff.title)
            await bot.send_message(user_id, texts.pay_card(tariff),
                                   reply_markup=keyboards.pay_url_kb(url))
        else:
            return await cb.answer(texts.EXPIRED, show_alert=True)
        await cb.answer()

    @router.pre_checkout_query()
    async def on_pre_checkout(query: PreCheckoutQuery):
        order_id = parse_payload(query.invoice_payload)
        order = await billing.get_order(db, order_id) if order_id else None
        if order_payable(order, query.from_user.id, query.total_amount, query.currency):
            await query.answer(ok=True)
        else:
            await query.answer(ok=False, error_message=texts.ORDER_GONE)

    @router.message(F.successful_payment)
    async def on_paid(message: Message):
        payment = message.successful_payment
        order_id = parse_payload(payment.invoice_payload)
        outcome = "unknown"
        if order_id and payment.currency == "XTR":
            outcome = await billing.credit_order(
                db, s, order_id, payment.total_amount,
                payment.telegram_payment_charge_id, method="stars")
        if outcome in ("credited", "already"):
            order = await billing.get_order(db, order_id)
            return await message.answer(texts.paid(s.tariffs[order["tariff"]]))
        log.error("оплата звёздами не зачислена: order=%s outcome=%s charge=%s",
                  order_id, outcome, payment.telegram_payment_charge_id)
        await message.answer(texts.PAY_FAILED)

    # ---------- ссылка и выбор качества ----------

    @router.message(F.text)
    async def on_text(message: Message, bot: Bot):
        user_id = message.from_user.id
        await billing.ensure_user(db, user_id)
        url = extract_url(message.text)
        if not url:
            return await message.answer(texts.NO_LINK)
        try:
            site = check_url(url, s.allowed_hosts)
        except LinkError as e:
            return await message.answer(texts.error(e.code))
        quota = await billing.get_quota(db, s, user_id)
        if quota.source is None:
            return await message.answer(texts.limit_reached(s), reply_markup=keyboards.tariffs_kb(s))
        if quota.source == "free" and s.required_channel and not await subscribed(bot, user_id):
            return await message.answer(texts.need_channel(s.required_channel))
        if user_id in probing or ctx.jobs.has(user_id):
            return await message.answer(texts.BUSY)

        note = await message.answer(texts.LOOKING)
        probing.add(user_id)
        try:
            async with probe_sem:
                info = await downloader.probe(s, url, site)
        except DownloadError as e:
            log.info("probe не удался: %s %s", e.code, e.detail[:300])
            return await safe_edit(note, texts.error(e.code))
        except Exception:
            log.exception("probe упал")
            return await safe_edit(note, texts.error("unknown"))
        finally:
            probing.discard(user_id)
        if await is_blocked(ctx, user_id, info.site, info.video_id):
            return await safe_edit(note, texts.BLOCKED)
        token = put_pending(ctx, PendingLink(url, info, user_id, time.time()))
        await safe_edit(note, texts.choose_quality(info),
                        reply_markup=keyboards.quality_kb(token, info.options))

    @router.callback_query(F.data.startswith("q:"))
    async def on_quality(cb: CallbackQuery, bot: Bot):
        try:
            _, token, quality = cb.data.split(":", 2)
        except ValueError:
            return await cb.answer()
        user_id = cb.from_user.id
        pending = get_pending(ctx, token)
        if pending is None or pending.user_id != user_id or cb.message is None:
            return await cb.answer(texts.EXPIRED, show_alert=True)
        if ctx.jobs.has(user_id):
            return await cb.answer(texts.BUSY, show_alert=True)
        await cb.answer()
        note = cb.message
        if ctx.jobs.full:
            await safe_edit(note, texts.queued(ctx.jobs.waiting + 1))

        async def status(code: str) -> None:
            await safe_edit(note, texts.SENDING if code == "sending" else texts.DOWNLOADING)

        outcome = await process_download(ctx, user_id, pending, quality,
                                         make_sender(bot, user_id), status)
        if outcome == "sent":
            try:
                await note.delete()
            except Exception:
                pass
            quota = await billing.get_quota(db, s, user_id)
            await bot.send_message(user_id, texts.after_download(quota, s),
                                   reply_markup=keyboards.promo_kb(s))
        elif outcome == "no_quota":
            await safe_edit(note, texts.limit_reached(s), reply_markup=keyboards.tariffs_kb(s))
        elif outcome == "busy":
            await bot.send_message(user_id, texts.BUSY)
        elif outcome == "blocked":
            await safe_edit(note, texts.BLOCKED)
        else:
            code = outcome.split(":", 1)[-1]
            markup = None
            if code in ("too_big", "send", "unknown", "blocked_by_site"):
                markup = keyboards.quality_kb(token, pending.info.options)  # можно выбрать другое
            await safe_edit(note, texts.error(code), reply_markup=markup)

    return router
