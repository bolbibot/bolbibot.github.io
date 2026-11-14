import hashlib
import json
from urllib.parse import parse_qs, unquote, urlsplit

from bot import robokassa
from bot.config import load_settings

ENV = {"BOT_TOKEN": "t", "ROBOKASSA_LOGIN": "shop", "ROBOKASSA_PASS1": "p1", "ROBOKASSA_PASS2": "p2"}


def test_sign():
    assert robokassa.sign(["a", "b"], "md5") == hashlib.md5(b"a:b").hexdigest()
    assert robokassa.sign(["a", "b"], "sha256") == hashlib.sha256(b"a:b").hexdigest()


def test_payment_url():
    s = load_settings(ENV)
    url = robokassa.payment_url(s, 7, 49, "10 скачиваний")
    parts = urlsplit(url)
    assert parts.scheme == "https" and parts.netloc == "auth.robokassa.ru"
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q["MerchantLogin"] == "shop" and q["OutSum"] == "49.00" and q["InvId"] == "7"
    assert q["Description"] == "10 скачиваний"
    assert q["SignatureValue"] == hashlib.md5(b"shop:49.00:7:p1").hexdigest()
    assert "IsTest" not in q and "Receipt" not in q


def test_payment_url_test_mode_and_receipt():
    s = load_settings({**ENV, "ROBOKASSA_TEST": "1", "ROBOKASSA_RECEIPT": "1", "ROBOKASSA_ALGO": "sha256"})
    url = robokassa.payment_url(s, 7, 49, "10 скачиваний")
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert q["IsTest"] == "1"
    encoded_once = q["Receipt"]            # в ссылке закодировано дважды
    receipt = json.loads(unquote(encoded_once))
    item = receipt["items"][0]
    assert item["name"] == "10 скачиваний" and item["sum"] == 49 and item["quantity"] == 1
    assert item["payment_object"] == "service" and item["tax"] == "none"
    expected = hashlib.sha256(f"shop:49.00:7:{encoded_once}:p1".encode()).hexdigest()
    assert q["SignatureValue"] == expected


def test_verify_result():
    s = load_settings(ENV)
    good = hashlib.md5(b"49.000000:7:p2").hexdigest()
    assert robokassa.verify_result(s, "49.000000", "7", good)
    assert robokassa.verify_result(s, "49.000000", "7", good.upper())
    assert not robokassa.verify_result(s, "49.000000", "8", good)
    assert not robokassa.verify_result(s, "49.000000", "7", hashlib.md5(b"49.000000:7:p1").hexdigest())
    assert not robokassa.verify_result(s, "49.000000", "7", "")
