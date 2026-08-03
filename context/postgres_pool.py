"""Process-wide PostgreSQL connection pools used by memory repositories."""
from __future__ import annotations

import logging
from threading import RLock
from typing import Any

from settings import MEMORY_CONFIG

logger = logging.getLogger(__name__)

_POOLS: dict[str, Any] = {}
_LOCK = RLock()


def get_postgres_pool(postgres_dsn: str):
    """Return one shared psycopg pool per DSN for the current process."""
    if not postgres_dsn:
        raise ValueError("postgres_dsn is required for PostgreSQL memory")

    with _LOCK:
        pool = _POOLS.get(postgres_dsn)
        if pool is not None:
            return pool

        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        config = MEMORY_CONFIG.get("long_term", {})
        min_size = max(int(config.get("postgres_pool_min_size", 1)), 0)
        max_size = max(int(config.get("postgres_pool_max_size", 10)), 1)
        if min_size > max_size:
            min_size = max_size

        pool = ConnectionPool(
            conninfo=postgres_dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=float(config.get("postgres_pool_timeout_sec", 10.0)),
            kwargs={"autocommit": False, "row_factory": dict_row},
            open=True,
        )
        _POOLS[postgres_dsn] = pool
        logger.info("Opened shared PostgreSQL memory pool")
        return pool


def close_all_postgres_pools() -> None:
    """Close and forget all process-wide pools during graceful shutdown."""
    with _LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        try:
            pool.close()
        except Exception:
            logger.exception("Failed to close PostgreSQL memory pool")


def reset_postgres_pools_for_tests() -> None:
    """Test helper; production code should use close_all_postgres_pools()."""
    close_all_postgres_pools()
