import pytest

from bot.config import load_settings
from bot.db import open_db


@pytest.fixture
def settings(tmp_path):
    return load_settings({
        "BOT_TOKEN": "123:abc",
        "ADMIN_IDS": "1",
        "TMP_DIR": str(tmp_path / "tmp"),
        "DB_PATH": ":memory:",
        "ROBOKASSA_LOGIN": "shop",
        "ROBOKASSA_PASS1": "pass1",
        "ROBOKASSA_PASS2": "pass2",
    })


@pytest.fixture
async def db():
    conn = await open_db(":memory:")
    yield conn
    await conn.close()
