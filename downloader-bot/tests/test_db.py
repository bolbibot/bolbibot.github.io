from bot.db import SCHEMA, open_db


async def test_tables_exist(tmp_path):
    path = str(tmp_path / "x" / "bot.db")
    db = await open_db(path)
    await db.close()
    db = await open_db(path)  # second open must not fail
    cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in await cur.fetchall()}
    assert {"users", "orders", "downloads", "file_cache", "blocked_videos"} <= names
    await db.close()
    assert SCHEMA
