"""Async facade over the synchronous memory manager.

所有 I/O 经 ``utils.io_executor.run_blocking`` 提交到应用自有有界线程池，避免同步
psycopg/redis 阻塞事件循环、并防止默认 asyncio executor 被放大。同步 API 保持不变，
本门面不改变 MemoryManager 的行为。
"""
from __future__ import annotations

from typing import Any

from utils.io_executor import run_blocking


class AsyncMemoryFacade:
    def __init__(self, memory_manager):
        self._m = memory_manager

    async def add_message(self, role: str, content: str, metadata: dict | None = None) -> str | bool:
        return await run_blocking(self._m.add_message, role, content, metadata)

    async def get_preference(self) -> dict:
        return await run_blocking(self._m.long_term.get_preference)

    async def save_preference(self, pref_type: str, value: Any) -> None:
        await run_blocking(self._m.long_term.save_preference, pref_type, value)

    async def get_active_trip(self) -> dict | None:
        return await run_blocking(self._m.get_active_trip)

    async def update_active_trip(self, trip_info: dict) -> dict:
        return await run_blocking(self._m.update_active_trip, trip_info)

    async def complete_active_trip(self, reason: str = "planning_completed") -> dict | None:
        return await run_blocking(self._m.complete_active_trip, reason)

    async def cancel_active_trip(self, reason: str = "user_cancelled") -> dict | None:
        return await run_blocking(self._m.cancel_active_trip, reason)

    async def get_recent_context(self, n_turns: int | None = None) -> list[dict]:
        return await run_blocking(self._m.short_term.get_recent_context, n_turns)

    async def get_statistics(self) -> dict:
        return await run_blocking(self._m.short_term.get_statistics)

    async def save_trip_history(self, trip_info: dict) -> None:
        await run_blocking(self._m.long_term.save_trip_history, trip_info)

    async def get_trip_history(self, limit: int | None = None) -> list[dict]:
        return await run_blocking(self._m.long_term.get_trip_history, limit)
