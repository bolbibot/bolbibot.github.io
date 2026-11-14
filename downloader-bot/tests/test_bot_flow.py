"""Сквозные сценарии: настоящий диспетчер aiogram, поддельный Telegram."""
import datetime as dt
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.types import (CallbackQuery, Chat, Message, PreCheckoutQuery, SuccessfulPayment,
                           Update, User, Video)

from bot import billing, downloader, texts
from bot.core import Ctx
from bot.downloader import Option, VideoInfo
from bot.handlers import build_router
from bot.jobs import Jobs

USER = User(id=1, is_bot=False, first_name="U")
CHAT = Chat(id=1, type="private")
NOW = dt.datetime(2026, 11, 14, tzinfo=dt.timezone.utc)
INFO = VideoInfo("rutube", "abc", "Ролик", 100, [Option("720", "720p", 50)])


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail = set()

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        self.calls.append(method)
        if name in self.fail:
            raise RuntimeError(f"{name} failed")
        if name in ("SendMessage", "EditMessageText", "SendInvoice"):
            return Message(message_id=100 + len(self.calls), date=NOW, chat=CHAT,
                           text=getattr(method, "text", None) or "x").as_(bot)
        if name == "SendVideo":
            return Message(message_id=100 + len(self.calls), date=NOW, chat=CHAT, video=Video(
                file_id="vid-1", file_unique_id="u", width=1, height=1, duration=1))
        return True

    def of(self, name):
        return [c for c in self.calls if type(c).__name__ == name]


@pytest.fixture
async def app(db, settings, monkeypatch):
    session = FakeSession()
    bot = Bot("123:abc", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    ctx = Ctx(settings=settings, db=db, jobs=Jobs(3))
    dp = Dispatcher()
    dp.include_router(build_router(ctx))

    async def probe(s, url, site):
        return INFO

    async def download(s, url, site, quality, workdir, selector=None):
        Path(workdir).mkdir(parents=True, exist_ok=True)
        path = Path(workdir) / "abc.mp4"
        path.write_bytes(b"video")
        return path

    monkeypatch.setattr(downloader, "probe", probe)
    monkeypatch.setattr(downloader, "download", download)

    counter = iter(range(1, 10_000))

    class App:
        pass

    a = App()
    a.session, a.ctx, a.db, a.settings = session, ctx, db, settings

    async def text(value, user=USER, **extra):
        n = next(counter)
        msg = Message(message_id=n, date=NOW, chat=Chat(id=user.id, type="private"),
                      from_user=user, text=value, **extra)
        await dp.feed_update(bot, Update(update_id=n, message=msg))

    async def tap(data, message, user=USER):
        n = next(counter)
        cb = CallbackQuery(id=str(n), from_user=user, chat_instance="c", data=data, message=message)
        await dp.feed_update(bot, Update(update_id=n, callback_query=cb))

    async def raw(**kwargs):
        await dp.feed_update(bot, Update(update_id=next(counter), **kwargs))

    a.text, a.tap, a.raw = text, tap, raw
    return a


def button_data(method):
    return [b.callback_data for row in method.reply_markup.inline_keyboard for b in row]


async def test_start(app):
    await app.text("/start")
    sent = app.session.of("SendMessage")[0]
    assert "BolBiBot" in sent.text
    assert sent.reply_markup.inline_keyboard[0][0].url == app.settings.bolbibot_url


async def test_link_to_file(app):
    await app.text("вот https://rutube.ru/video/abc/")
    edit = app.session.of("EditMessageText")[-1]
    assert "Ролик" in edit.text
    data = button_data(edit)
    assert len(data) == 1 and data[0].endswith(":720")

    note = Message(message_id=edit.message_id, date=NOW, chat=CHAT, text="x")
    await app.tap(data[0], note)
    video = app.session.of("SendVideo")
    assert len(video) == 1 and video[0].supports_streaming is True
    assert "Готово" in app.session.of("SendMessage")[-1].text
    quota = await billing.get_quota(app.db, app.settings, 1)
    assert quota.free_left == 0
    cache = await (await app.db.execute("SELECT file_id FROM file_cache")).fetchone()
    assert cache["file_id"] == "vid-1"

    # второе видео в тот же день — предложение купить, а не скачивание
    await app.text("https://rutube.ru/video/abc/")
    last = app.session.of("SendMessage")[-1]
    assert last.text == texts.limit_reached(app.settings)
    assert button_data(last) == ["buy:stars:p10", "buy:stars:p50", "buy:stars:unlim30"]
    assert len(app.session.of("SendVideo")) == 1


async def test_send_failure_is_free(app):
    await app.text("https://rutube.ru/video/abc/")
    edit = app.session.of("EditMessageText")[-1]
    app.session.fail.add("SendVideo")
    await app.tap(button_data(edit)[0], Message(message_id=5, date=NOW, chat=CHAT, text="x"))
    assert (await billing.get_quota(app.db, app.settings, 1)).free_left == 1
    assert app.session.of("EditMessageText")[-1].text == texts.error("send")


async def test_bad_links(app):
    await app.text("привет")
    assert app.session.of("SendMessage")[-1].text == texts.NO_LINK
    await app.text("http://127.0.0.1/admin")
    assert app.session.of("SendMessage")[-1].text == texts.error("not_allowed")
    await app.text("https://example.com/video")
    assert app.session.of("SendMessage")[-1].text == texts.error("not_allowed")
    assert app.session.of("EditMessageText") == []


async def test_stale_or_foreign_button(app):
    await app.text("https://rutube.ru/video/abc/")
    data = button_data(app.session.of("EditMessageText")[-1])[0]
    other = User(id=2, is_bot=False, first_name="X")
    await app.tap(data, Message(message_id=5, date=NOW, chat=CHAT, text="x"), user=other)
    await app.tap("q:nope:720", Message(message_id=5, date=NOW, chat=CHAT, text="x"))
    assert app.session.of("SendVideo") == []
    assert all(c.text == texts.EXPIRED for c in app.session.of("AnswerCallbackQuery"))


async def test_stars_purchase_and_refund(app):
    app.settings.admin_ids = {1}
    await app.tap("buy:stars:p10", Message(message_id=5, date=NOW, chat=CHAT, text="x"))
    invoice = app.session.of("SendInvoice")[0]
    assert invoice.currency == "XTR" and invoice.prices[0].amount == 25
    assert invoice.payload == "order:1"

    await app.raw(pre_checkout_query=PreCheckoutQuery(
        id="p", from_user=USER, currency="XTR", total_amount=25, invoice_payload="order:1"))
    assert app.session.of("AnswerPreCheckoutQuery")[-1].ok is True
    await app.raw(pre_checkout_query=PreCheckoutQuery(
        id="p", from_user=USER, currency="XTR", total_amount=1, invoice_payload="order:1"))
    assert app.session.of("AnswerPreCheckoutQuery")[-1].ok is False

    pay = SuccessfulPayment(currency="XTR", total_amount=25, invoice_payload="order:1",
                            telegram_payment_charge_id="ch-1", provider_payment_charge_id="")
    for n in (50, 51):  # Telegram может доставить сообщение дважды
        await app.raw(message=Message(message_id=n, date=NOW, chat=CHAT, from_user=USER,
                                      successful_payment=pay))
    assert (await billing.get_quota(app.db, app.settings, 1)).credits == 10
    assert "10 скачиваний" in app.session.of("SendMessage")[-1].text

    await app.text("/refund 1")
    refund = app.session.of("RefundStarPayment")[0]
    assert (refund.user_id, refund.telegram_payment_charge_id) == (1, "ch-1")
    assert (await billing.get_quota(app.db, app.settings, 1)).credits == 0
    await app.text("/refund 1")
    assert len(app.session.of("RefundStarPayment")) == 1


async def test_refund_keeps_credits_if_telegram_refuses(app):
    app.settings.admin_ids = {1}
    oid = await billing.create_order(app.db, 1, app.settings.tariffs["p10"], "stars")
    await billing.credit_order(app.db, app.settings, oid, 25, "ch")
    app.session.fail.add("RefundStarPayment")
    await app.text(f"/refund {oid}")
    assert (await billing.get_order(app.db, oid))["status"] == "paid"
    assert (await billing.get_quota(app.db, app.settings, 1)).credits == 10


async def test_card_button_only_when_enabled(app):
    await app.tap("buy:rk:p10", Message(message_id=5, date=NOW, chat=CHAT, text="x"))
    assert app.session.of("SendMessage") == []
    app.settings.robokassa_enabled = True
    await app.tap("buy:rk:p10", Message(message_id=5, date=NOW, chat=CHAT, text="x"))
    msg = app.session.of("SendMessage")[-1]
    assert msg.reply_markup.inline_keyboard[0][0].url.startswith("https://auth.robokassa.ru/")
    order = await billing.get_order(app.db, 1)
    assert (order["method"], order["amount"]) == ("robokassa", 49)


async def test_admin_commands_are_protected(app):
    app.settings.admin_ids = set()
    for cmd in ("/stats", "/refund 1", "/block 5"):
        await app.text(cmd)
        assert app.session.of("SendMessage")[-1].text == texts.ADMIN_ONLY
    app.settings.admin_ids = {1}
    await app.text("/stats")
    assert "Пользователей" in app.session.of("SendMessage")[-1].text
    await app.text("/block 5")
    row = await (await app.db.execute("SELECT blocked FROM users WHERE id=5")).fetchone()
    assert row["blocked"] == 1
    await app.text("/block https://rutube.ru/video/abc/")
    assert await (await app.db.execute("SELECT 1 FROM blocked_videos")).fetchone()
    await app.text("https://rutube.ru/video/abc/")
    assert app.session.of("EditMessageText")[-1].text == texts.BLOCKED


async def test_plain_commands(app):
    for cmd in ("/help", "/terms", "/paysupport", "/balance"):
        await app.text(cmd)
    texts_sent = [c.text for c in app.session.of("SendMessage")]
    assert len(texts_sent) == 4 and "<code>1</code>" in texts_sent[2]


async def test_probe_rate_limit(app, monkeypatch):
    calls = []

    async def probe(s, url, site):
        calls.append(url)
        return INFO

    monkeypatch.setattr(downloader, "probe", probe)
    for _ in range(12):
        await app.text("https://rutube.ru/video/abc/")
    assert len(calls) == 10
    assert app.session.of("SendMessage")[-1].text == texts.TOO_MANY


async def test_blocked_user_is_refused_before_probe(app, monkeypatch):
    calls = []

    async def probe(s, url, site):
        calls.append(url)
        return INFO

    monkeypatch.setattr(downloader, "probe", probe)
    await billing.ensure_user(app.db, 1)
    await app.db.execute("UPDATE users SET blocked=1 WHERE id=1")
    await app.text("https://rutube.ru/video/abc/")
    assert calls == []
    assert app.session.of("SendMessage")[-1].text == texts.BLOCKED


async def test_simultaneous_links_start_one_probe(app, monkeypatch):
    import asyncio
    gate, calls = asyncio.Event(), []

    async def probe(s, url, site):
        calls.append(url)
        await gate.wait()
        return INFO

    monkeypatch.setattr(downloader, "probe", probe)
    tasks = [asyncio.create_task(app.text("https://rutube.ru/video/abc/")) for _ in range(4)]
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(*tasks)
    assert len(calls) == 1
