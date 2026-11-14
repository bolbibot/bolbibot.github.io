"""Все сообщения бота. Меняйте тексты здесь — код трогать не нужно."""
from __future__ import annotations

from html import escape

from .billing import MOSCOW, Quota
from .config import Settings, Tariff
from .downloader import VideoInfo

SITES = "YouTube, VK Видео, Rutube, TikTok, Дзен, Одноклассники, Twitch, Vimeo, Dailymotion"

TERMS = (
    "<b>Условия использования</b>\n\n"
    "• Бот скачивает только общедоступные видео по вашей ссылке.\n"
    "• Скачивайте только то, на что у вас есть право: свои видео, видео с разрешения "
    "автора или с открытой лицензией. За дальнейшее использование отвечаете вы.\n"
    "• Скачанное засчитывается только после того, как файл пришёл в чат. "
    "Если не получилось — ничего не списывается.\n"
    "• Купленные скачивания не сгорают. Безлимит действует 30 дней с момента покупки.\n"
    "• Правообладателям: напишите в поддержку (/paysupport), и мы закроем доступ к видео."
)

HELP = (
    "Пришлите ссылку на видео — я покажу доступное качество и пришлю файл.\n\n"
    f"Поддерживаются: {SITES}.\n\n"
    "/balance — остаток и покупка скачиваний\n"
    "/terms — условия\n"
    "/paysupport — вопросы по оплате"
)

LOOKING = "Смотрю, что по ссылке…"
DOWNLOADING = "Скачиваю… Обычно это занимает до пары минут."
SENDING = "Отправляю файл…"
BUSY = "Я ещё работаю над вашим предыдущим видео. Дождитесь его, потом пришлите следующее."
TOO_MANY = "Слишком много ссылок подряд. Подождите несколько минут и пришлите снова."
EXPIRED = "Эта кнопка устарела. Пришлите ссылку ещё раз."
BLOCKED = "Это видео или ваш доступ к боту ограничены."
NO_LINK = "Пришлите ссылку на видео, например https://rutube.ru/video/…"
STARS_BUTTON = "⭐ {title} — {stars}"
CARD_BUTTON = "💳 {title} — {rub} ₽"
PROMO_BUTTON = "Сайты, приложения и видео — BolBiBot"
PAY_CARD_BUTTON = "Перейти к оплате"
ADMIN_ONLY = "Команда только для администратора."


def welcome(s: Settings) -> str:
    return (
        "Привет! Я скачиваю видео по ссылке.\n\n"
        f"Пришлите ссылку с {SITES} — выберите качество, и файл придёт сюда.\n\n"
        f"Бесплатно: {s.free_per_day} в сутки. Больше — в /balance.\n"
        "Пользуясь ботом, вы принимаете условия: /terms\n\n"
        "Сделано командой BolBiBot."
    )


def _duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    h, rest = divmod(int(seconds), 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def size_label(size: int | None) -> str:
    if not size:
        return ""
    mb = size / 1_000_000
    return f"{mb / 1000:.1f} ГБ" if mb >= 1000 else f"{max(1, round(mb))} МБ"


def choose_quality(info: VideoInfo) -> str:
    duration = _duration(info.duration)
    tail = f" · {duration}" if duration else ""
    return f"<b>{escape(info.title)}</b>{tail}\n\nВыберите качество:"


def caption(info: VideoInfo) -> str:
    return escape(info.title[:900])


def quota_line(q: Quota, s: Settings) -> str:
    if q.unlimited_until:
        until = q.unlimited_until.astimezone(MOSCOW).strftime("%d.%m.%Y")
        return f"Безлимит до {until}."
    parts = [f"Бесплатных сегодня: {q.free_left} из {s.free_per_day}", f"купленных: {q.credits}"]
    return "Осталось — " + ", ".join(parts) + "."


def after_download(q: Quota, s: Settings) -> str:
    return f"Готово! {quota_line(q, s)}\n\nСделано командой BolBiBot."


def balance(q: Quota, s: Settings) -> str:
    return f"{quota_line(q, s)}\n\nБесплатный лимит обновляется в полночь по Москве. Купить ещё:"


def limit_reached(s: Settings) -> str:
    return (
        "Бесплатное скачивание на сегодня использовано.\n"
        "Следующее бесплатное — завтра, или возьмите пакет:"
    )


def need_channel(channel: str) -> str:
    return (f"Бесплатные скачивания доступны подписчикам канала {escape(channel)}. "
            "Подпишитесь и пришлите ссылку ещё раз. Купленные скачивания работают без подписки.")


def queued(position: int) -> str:
    return f"Вы в очереди, перед вами: {position}. Начну, как только освободится место."


def invoice_description(t: Tariff) -> str:
    if t.days:
        return f"Скачивания без ограничений на {t.days} дней."
    return f"{t.downloads} скачиваний видео. Не сгорают."


def pay_card(t: Tariff) -> str:
    return f"{t.title} — {t.rub} ₽. Оплата картой или СБП через Robokassa. " \
           "После оплаты я напишу сюда."


def paid(t: Tariff) -> str:
    return f"Оплата получена: {t.title}. Спасибо! Присылайте ссылку."


PAY_REFUNDED = ("Этот счёт уже был оплачен раньше, поэтому второй платёж я вернул. "
                "Звёзды снова на вашем балансе.")
PAY_FAILED = "Не получилось зачислить оплату. Напишите в поддержку: /paysupport"
ORDER_GONE = "Этот счёт уже недействителен. Откройте /balance и выберите пакет заново."


def paysupport(user_id: int) -> str:
    return ("По вопросам оплаты и возврата напишите администратору и укажите ваш ID: "
            f"<code>{user_id}</code>.\nВозврат делаем, если купленные скачивания не использованы "
            "или сервис не сработал.")


_ERRORS = {
    "not_url": NO_LINK,
    "bad_scheme": "Понимаю только ссылки, которые начинаются с http:// или https://.",
    "not_allowed": f"С этого сайта я не скачиваю. Поддерживаются: {SITES}.",
    "unsupported": "Не нашёл видео по этой ссылке. Пришлите ссылку на страницу самого ролика.",
    "unavailable": "Видео недоступно: удалено, скрыто или закрыто для вашего региона.",
    "private": "Это закрытое видео — доступ к нему есть только у владельца или подписчиков.",
    "too_long": "Видео слишком длинное. Я скачиваю ролики до 3 часов.",
    "too_big": "Файл получается слишком большим. Попробуйте качество пониже.",
    "timeout": "Видео скачивается слишком долго, я остановил загрузку. Попробуйте качество "
               "пониже или позже. Скачивание не списано.",
    "live": "Это прямая трансляция. Пришлите ссылку, когда появится запись.",
    "playlist": "Это плейлист. Пришлите ссылку на отдельное видео.",
    "blocked_by_site": "Сайт сейчас не отдаёт это видео боту. Попробуйте позже. "
                       "Скачивание не списано.",
    "send": "Видео скачалось, но Telegram не принял файл. Попробуйте качество пониже. "
            "Скачивание не списано.",
    "unknown": "Не получилось скачать это видео. Попробуйте позже или другую ссылку. "
               "Скачивание не списано.",
}


def error(code: str) -> str:
    return _ERRORS.get(code, _ERRORS["unknown"])
