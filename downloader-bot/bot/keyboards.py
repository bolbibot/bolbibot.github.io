"""Кнопки под сообщениями."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import texts
from .config import Settings
from .downloader import Option


def quality_kb(token: str, options: list[Option]) -> InlineKeyboardMarkup:
    rows = []
    for o in options:
        size = texts.size_label(o.size)
        label = f"{o.label} · {size}" if size else o.label
        if o.quality == "audio":
            label = f"Только звук {label}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"q:{token}:{o.quality}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tariffs_kb(s: Settings) -> InlineKeyboardMarkup:
    rows = []
    for t in s.tariffs.values():
        rows.append([InlineKeyboardButton(
            text=texts.STARS_BUTTON.format(title=t.title, stars=t.stars),
            callback_data=f"buy:stars:{t.key}")])
    if s.robokassa_enabled:
        for t in s.tariffs.values():
            rows.append([InlineKeyboardButton(
                text=texts.CARD_BUTTON.format(title=t.title, rub=t.rub),
                callback_data=f"buy:rk:{t.key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def promo_kb(s: Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=texts.PROMO_BUTTON, url=s.bolbibot_url)]])


def pay_url_kb(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=texts.PAY_CARD_BUTTON, url=url)]])
