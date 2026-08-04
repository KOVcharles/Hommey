"""Ordered, checksum-verified PostgreSQL migrations."""
from __future__ import annotations

import hashlib
from pathlib import Path

from settings import AUTH_CONFIG, MEMORY_CONFIG


MIGRATIONS_DIR = Path(__file__).with_name("migrations")

# The structured-answer branch originally used 0006-0008 while the memory
# foundation was being developed on main with the same versions. Re-map only
# those exact historical names before checksum validation so either deployment
# history can converge on one monotonic sequence without rerunning data moves.
LEGACY_VERSION_REMAP = (
    ("0006", "0009", "0006_answer_documents.sql", "0009_answer_documents.sql"),
    ("0007", "0010", "0007_presentation_documents.sql", "0010_presentation_documents.sql"),
    ("0008", "0011", "0008_user_travel_preferences.sql", "0011_user_travel_preferences.sql"),
)


def _remap_legacy_version_collisions(cur) -> int:
    remapped = 0
    for old_version, new_version, old_name, new_name in LEGACY_VERSION_REMAP:
        cur.execute(
            """
            SELECT version, name, checksum
            FROM schema_migrations
            WHERE version IN (%s, %s)
            """,
            (old_version, new_version),
        )
        rows = cur.fetchall()
        by_version = {
            (row[0] if not isinstance(row, dict) else row["version"]): row
            for row in rows
        }
        old_row = by_version.get(old_version)
        if old_row is None:
            continue
        existing_name = old_row[1] if not isinstance(old_row, dict) else old_row["name"]
        if existing_name != old_name:
            continue

        target_row = by_version.get(new_version)
        if target_row is not None:
            old_checksum = old_row[2] if not isinstance(old_row, dict) else old_row["checksum"]
            target_checksum = (
                target_row[2] if not isinstance(target_row, dict) else target_row["checksum"]
            )
            if old_checksum != target_checksum:
                raise RuntimeError(
                    f"Cannot reconcile migration {old_version}: target {new_version} differs"
                )
            cur.execute("DELETE FROM schema_migrations WHERE version = %s", (old_version,))
        else:
            cur.execute(
                """
                UPDATE schema_migrations
                SET version = %s, name = %s
                WHERE version = %s AND name = %s
                """,
                (new_version, new_name, old_version, old_name),
            )
        remapped += 1
    return remapped


def apply_all_migrations(postgres_dsn: str | None = None) -> int:
    dsn = postgres_dsn if postgres_dsn is not None else (
        MEMORY_CONFIG.get("long_term", {}).get("postgres_dsn", "")
    )
    if not dsn:
        return 0

    import psycopg

    applied = 0
    with psycopg.connect(dsn, autocommit=False, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            _remap_legacy_version_collisions(cur)
        conn.commit()

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem.split("_", 1)[0]
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            with conn.cursor() as cur:
                cur.execute("SELECT checksum FROM schema_migrations WHERE version = %s", (version,))
                row = cur.fetchone()
                if row:
                    existing = row[0] if not isinstance(row, dict) else row["checksum"]
                    if existing != checksum:
                        raise RuntimeError(f"Applied migration {version} checksum changed")
                    continue
                try:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                        (version, path.name, checksum),
                    )
                    conn.commit()
                    applied += 1
                except Exception:
                    conn.rollback()
                    raise
        admin_emails = AUTH_CONFIG.get("admin_emails", ())
        if admin_emails:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET role = 'admin' WHERE LOWER(email) = ANY(%s)",
                    (list(admin_emails),),
                )
            conn.commit()
    return applied
