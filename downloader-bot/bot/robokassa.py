"""Robokassa: ссылка на оплату и проверка уведомления об оплате (Result URL)."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Sequence
from urllib.parse import quote, urlencode

from .config import Settings

PAY_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"


def sign(parts: Sequence[str], algo: str) -> str:
    return hashlib.new(algo, ":".join(parts).encode("utf-8")).hexdigest()


def _receipt(description: str, amount_rub: int) -> str:
    """Номенклатура для чека: JSON, закодированный для URL один раз."""
    data = {"items": [{
        "name": description[:128], "quantity": 1, "sum": amount_rub,
        "payment_method": "full_payment", "payment_object": "service", "tax": "none",
    }]}
    return quote(json.dumps(data, ensure_ascii=False, separators=(",", ":")), safe="")


def payment_url(s: Settings, order_id: int, amount_rub: int, description: str) -> str:
    out_sum = f"{amount_rub:.2f}"
    params = {"MerchantLogin": s.robokassa_login, "OutSum": out_sum, "InvId": str(order_id),
              "Description": description}
    parts = [s.robokassa_login, out_sum, str(order_id)]
    if s.robokassa_receipt:
        receipt = _receipt(description, amount_rub)
        parts.append(receipt)
        params["Receipt"] = receipt  # urlencode ниже кодирует второй раз — так требует Robokassa
    parts.append(s.robokassa_pass1)
    params["SignatureValue"] = sign(parts, s.robokassa_algo)
    if s.robokassa_test:
        params["IsTest"] = "1"
    return f"{PAY_URL}?{urlencode(params)}"


def verify_result(s: Settings, out_sum: str, inv_id: str, signature: str) -> bool:
    if not signature or not s.robokassa_pass2:
        return False
    expected = sign([out_sum, inv_id, s.robokassa_pass2], s.robokassa_algo)
    return hmac.compare_digest(expected.lower(), signature.strip().lower())
