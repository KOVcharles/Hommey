"""Migrations must be safe when multiple uvicorn workers start concurrently.

Requires a PostgreSQL backend (HOMMEY_POSTGRES_DSN) to run; skipped otherwise.
`apply_all_migrations` acquires a session-level advisory lock, so concurrent
connections serialize the whole migration run and never race on the
schema_migrations primary key.
"""

import threading

import pytest

from settings import MEMORY_CONFIG
from webui_new.auth.migrations import apply_all_migrations


@pytest.mark.skipif(
    not MEMORY_CONFIG["long_term"].get("postgres_dsn"),
    reason="requires postgres backend",
)
def test_migrations_run_concurrently_without_duplicate():
    # 两次调用应都成功返回（advisory lock 保证不重复执行）
    r1 = apply_all_migrations()
    r2 = apply_all_migrations()
    assert r1 >= 0 and r2 >= 0


@pytest.mark.skipif(
    not MEMORY_CONFIG["long_term"].get("postgres_dsn"),
    reason="requires postgres backend",
)
def test_migrations_serialize_across_concurrent_connections():
    # 多线程各自建立独立连接、经 Barrier 同时开跑迁移。advisory lock 串行化
    # 临界区：先到者完成全部迁移并释放锁，其余线程随后进入并跳过已应用迁移。
    # 所有调用都应成功返回（>= 0），不得因 schema_migrations 主键冲突抛错。
    barrier = threading.Barrier(4)
    results = []
    errors = []

    def _run():
        barrier.wait()
        try:
            results.append(apply_all_migrations())
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"concurrent migration run raised: {errors}"
    assert len(results) == 4
    assert all(r >= 0 for r in results)
