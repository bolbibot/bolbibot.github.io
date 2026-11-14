"""Очередь загрузок: одна задача на человека, ограничение на параллельные загрузки."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class Busy(Exception):
    """У пользователя уже есть задача."""


class Jobs:
    def __init__(self, parallel: int):
        self._sem = asyncio.Semaphore(max(1, parallel))
        self._users: set[int] = set()
        self.waiting = 0

    @asynccontextmanager
    async def slot(self, user_id: int):
        if user_id in self._users:
            raise Busy()
        self._users.add(user_id)
        try:
            self.waiting += 1
            try:
                await self._sem.acquire()
            finally:
                self.waiting -= 1
            try:
                yield
            finally:
                self._sem.release()
        finally:
            self._users.discard(user_id)
