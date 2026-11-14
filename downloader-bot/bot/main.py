"""Запуск бота: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import BotCommand
from aiohttp import web

from . import billing, texts
from .config import load_settings
from .config import Settings
from .core import Ctx, clean_tmp
from .db import open_db
from .handlers import build_router
from .jobs import Jobs
from .web import make_app

log = logging.getLogger("bot")
CLOUD_API_LIMIT = 49_000_000  # обычный Bot API не принимает файлы больше 50 МБ


def startup_warnings(s: Settings) -> list[str]:
    """Настройки, с которыми бот запустится, но работать будет не так, как ждёт владелец."""
    warnings = []
    if s.robokassa_enabled and s.robokassa_test:
        warnings.append("ROBOKASSA_TEST=1: включён ТЕСТОВЫЙ режим оплаты картой — тестовые "
                        "платежи начисляют настоящие скачивания. Выключите перед запуском в работу.")
    if not s.admin_ids:
        warnings.append("ADMIN_IDS не задан: некому получать сообщения о сбоях оплаты, "
                        "команды /stats, /refund и /block недоступны.")
    return warnings


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = load_settings(os.environ)
    if not s.bot_api_url and s.max_filesize > CLOUD_API_LIMIT:
        log.warning("BOT_API_URL не задан: файлы ограничены 49 МБ")
        s.max_filesize = CLOUD_API_LIMIT

    for warning in startup_warnings(s):
        log.warning(warning)
    clean_tmp(s.tmp_dir)  # остатки после прошлого запуска
    db = await open_db(s.db_path)

    session = None
    if s.bot_api_url:
        session = AiohttpSession(api=TelegramAPIServer.from_base(s.bot_api_url, is_local=True))
    bot = Bot(s.bot_token, session=session, default=DefaultBotProperties(parse_mode="HTML"))

    ctx = Ctx(settings=s, db=db, jobs=Jobs(s.parallel))
    dp = Dispatcher()
    dp.include_router(build_router(ctx))

    runner = None
    if s.robokassa_enabled:
        async def notify(user_id: int, order_id: int) -> None:
            order = await billing.get_order(db, order_id)
            await bot.send_message(user_id, texts.paid(s.tariffs[order["tariff"]]))

        runner = web.AppRunner(make_app(s, db, notify))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", s.web_port).start()
        log.info("Robokassa: приём уведомлений на порту %s", s.web_port)

    await bot.set_my_commands([
        BotCommand(command="start", description="Начать"),
        BotCommand(command="balance", description="Остаток и покупка скачиваний"),
        BotCommand(command="help", description="Как пользоваться"),
        BotCommand(command="terms", description="Условия"),
        BotCommand(command="paysupport", description="Вопросы по оплате"),
    ])
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if runner:
            await runner.cleanup()
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
