"""Transactional PostgreSQL repository for stage-2A profile facts and conflicts."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.types.json import Jsonb

from context.profile_catalog import get_profile_field, normalize_profile_value
from utils.memory_safety import redact_sensitive_text


class PendingProfileChangeError(RuntimeError):
    """A different unresolved proposal already exists for this profile field."""


class StaleProfileChangeError(RuntimeError):
    """The fact referenced by a pending change is no longer active."""


@dataclass(frozen=True)
class ProfileFactRecord:
    fact_id: uuid.UUID
    user_id: str
    namespace: str
    fact_key: str
    fact_value: Any
    normalized_value: str
    status: str
    write_mode: str
    source_turn_id: uuid.UUID | None
    version: int
    valid_from: datetime
    valid_to: datetime | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ProfileFactRecord":
        return cls(
            fact_id=row["fact_id"],
            user_id=row["user_id"],
            namespace=row["namespace"],
            fact_key=row["fact_key"],
            fact_value=row["fact_value"],
            normalized_value=row["normalized_value"],
            status=row["status"],
            write_mode=row["write_mode"],
            source_turn_id=row["source_turn_id"],
            version=int(row["version"]),
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
        )


@dataclass(frozen=True)
class ProfileChangeRecord:
    change_id: uuid.UUID
    user_id: str
    namespace: str
    fact_key: str
    old_fact_id: uuid.UUID | None
    proposed_value: Any
    proposed_normalized_value: str
    source_turn_id: uuid.UUID
    status: str
    expires_at: datetime
    resolved_at: datetime | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ProfileChangeRecord":
        return cls(
            change_id=row["change_id"],
            user_id=row["user_id"],
            namespace=row["namespace"],
            fact_key=row["fact_key"],
            old_fact_id=row["old_fact_id"],
            proposed_value=row["proposed_value"],
            proposed_normalized_value=row["proposed_normalized_value"],
            source_turn_id=row["source_turn_id"],
            status=row["status"],
            expires_at=row["expires_at"],
            resolved_at=row["resolved_at"],
        )


@dataclass(frozen=True)
class ProfileProposalResult:
    """Outcome of proposing an explicitly stated profile value."""

    status: str  # created, unchanged, or pending
    fact: ProfileFactRecord
    change: ProfileChangeRecord | None = None


@dataclass(frozen=True)
class ProfileResolutionResult:
    """Outcome of confirming, rejecting, or expiring a pending proposal."""

    change: ProfileChangeRecord
    fact: ProfileFactRecord | None


class PostgresProfileRepository:
    """Keep profile versions and conflict resolution atomic per user."""

    _FACT_COLUMNS = """
        fact_id, user_id, namespace, fact_key, fact_value, normalized_value,
        status, write_mode, source_turn_id, version, valid_from, valid_to
    """
    _CHANGE_COLUMNS = """
        change_id, user_id, namespace, fact_key, old_fact_id, proposed_value,
        proposed_normalized_value, source_turn_id, status, expires_at, resolved_at
    """

    def __init__(self, pool, *, change_ttl_days: int = 7):
        self.pool = pool
        self.change_ttl_days = max(int(change_ttl_days), 1)

    @staticmethod
    def _lock_user(cur, user_id: str) -> None:
        # The advisory lock serializes first-write and conflict creation for one user.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (user_id,))

    def get_active_fact(self, user_id: str, fact_key: str) -> ProfileFactRecord | None:
        definition = get_profile_field(fact_key)
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {self._FACT_COLUMNS}
                    FROM user_profile_facts
                    WHERE user_id = %s AND namespace = %s AND fact_key = %s
                      AND status = 'active'
                    """,
                    (user_id, definition.namespace, fact_key),
                )
                row = cur.fetchone()
        return ProfileFactRecord.from_row(row) if row else None

    def list_active_facts(self, user_id: str) -> list[ProfileFactRecord]:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {self._FACT_COLUMNS}
                    FROM user_profile_facts
                    WHERE user_id = %s AND status = 'active'
                    ORDER BY namespace, fact_key
                    """,
                    (user_id,),
                )
                rows = cur.fetchall()
        return [ProfileFactRecord.from_row(row) for row in rows]

    def get_pending_change(self, user_id: str) -> ProfileChangeRecord | None:
        """Return the oldest live proposal for pre-routing confirmation handling."""
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {self._CHANGE_COLUMNS}
                    FROM memory_change_requests
                    WHERE user_id = %s AND status = 'pending' AND expires_at > NOW()
                    ORDER BY created_at
                    LIMIT 1
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
        return ProfileChangeRecord.from_row(row) if row else None

    def propose_explicit_fact(
        self,
        *,
        user_id: str,
        fact_key: str,
        value: Any,
        source_turn_id: str | uuid.UUID,
        source_excerpt: str = "",
        reason: str = "explicit_conflict",
    ) -> ProfileProposalResult:
        """Create the first fact, or create a pending change instead of overwriting."""
        definition = get_profile_field(fact_key)
        normalized = normalize_profile_value(fact_key, value)
        turn_id = self._required_uuid(source_turn_id, field_name="source_turn_id")
        safe_excerpt = redact_sensitive_text(str(source_excerpt or "").strip())[:300] or None

        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._lock_user(cur, user_id)
                    active = self._select_active_fact_for_update(
                        cur, user_id, definition.namespace, fact_key
                    )
                    if active is None:
                        fact = self._insert_fact(
                            cur,
                            user_id=user_id,
                            namespace=definition.namespace,
                            fact_key=fact_key,
                            value=normalized.value,
                            normalized_value=normalized.comparable,
                            write_mode="auto_explicit",
                            source_turn_id=turn_id,
                            source_excerpt=safe_excerpt,
                            version=1,
                        )
                        self._increment_profile_version(cur, user_id)
                        return ProfileProposalResult("created", fact)

                    if active.normalized_value == normalized.comparable:
                        return ProfileProposalResult("unchanged", active)

                    pending = self._select_pending_change_for_update(
                        cur, user_id, definition.namespace, fact_key
                    )
                    if pending is not None and pending.expires_at <= datetime.now(timezone.utc):
                        self._set_change_status(cur, pending.change_id, "expired")
                        pending = None
                    if pending is not None:
                        if pending.proposed_normalized_value == normalized.comparable:
                            return ProfileProposalResult("pending", active, pending)
                        raise PendingProfileChangeError(
                            f"Resolve the pending {fact_key} change before proposing another value"
                        )

                    expires_at = datetime.now(timezone.utc) + timedelta(days=self.change_ttl_days)
                    change_id = uuid.uuid4()
                    cur.execute(
                        f"""
                        INSERT INTO memory_change_requests (
                            change_id, user_id, namespace, fact_key, old_fact_id,
                            proposed_value, proposed_normalized_value, reason,
                            source_turn_id, source_excerpt, status, expires_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                        RETURNING {self._CHANGE_COLUMNS}
                        """,
                        (
                            change_id,
                            user_id,
                            definition.namespace,
                            fact_key,
                            active.fact_id,
                            Jsonb(normalized.value),
                            normalized.comparable,
                            str(reason or "explicit_conflict")[:120],
                            turn_id,
                            safe_excerpt,
                            expires_at,
                        ),
                    )
                    return ProfileProposalResult(
                        "pending", active, ProfileChangeRecord.from_row(cur.fetchone())
                    )

    def resolve_change(
        self,
        *,
        user_id: str,
        change_id: str | uuid.UUID,
        accepted: bool,
    ) -> ProfileResolutionResult:
        """Resolve one pending change idempotently and version the fact on confirmation."""
        resolved_id = self._required_uuid(change_id, field_name="change_id")
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._lock_user(cur, user_id)
                    cur.execute(
                        f"""
                        SELECT {self._CHANGE_COLUMNS}, source_excerpt
                        FROM memory_change_requests
                        WHERE change_id = %s AND user_id = %s
                        FOR UPDATE
                        """,
                        (resolved_id, user_id),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise KeyError(f"Profile change not found: {resolved_id}")
                    change = ProfileChangeRecord.from_row(row)

                    # Repeated confirmation/rejection requests return the durable result.
                    if change.status != "pending":
                        active = self._select_active_fact_for_update(
                            cur, user_id, change.namespace, change.fact_key
                        )
                        return ProfileResolutionResult(change, active)

                    if change.expires_at <= datetime.now(timezone.utc):
                        expired = self._set_change_status(cur, resolved_id, "expired")
                        return ProfileResolutionResult(expired, None)

                    if not accepted:
                        rejected = self._set_change_status(cur, resolved_id, "rejected")
                        return ProfileResolutionResult(rejected, None)

                    active = self._select_active_fact_for_update(
                        cur, user_id, change.namespace, change.fact_key
                    )
                    if active is None or active.fact_id != change.old_fact_id:
                        raise StaleProfileChangeError(
                            "The profile fact changed before this proposal was confirmed"
                        )

                    normalized = normalize_profile_value(change.fact_key, change.proposed_value)
                    cur.execute(
                        """
                        UPDATE user_profile_facts
                        SET status = 'superseded', valid_to = NOW(), updated_at = NOW()
                        WHERE fact_id = %s AND status = 'active'
                        """,
                        (active.fact_id,),
                    )
                    new_fact = self._insert_fact(
                        cur,
                        user_id=user_id,
                        namespace=change.namespace,
                        fact_key=change.fact_key,
                        value=normalized.value,
                        normalized_value=normalized.comparable,
                        write_mode="user_confirmed",
                        source_turn_id=change.source_turn_id,
                        source_excerpt=row.get("source_excerpt"),
                        version=active.version + 1,
                    )
                    confirmed = self._set_change_status(cur, resolved_id, "confirmed")
                    self._increment_profile_version(cur, user_id)
                    return ProfileResolutionResult(confirmed, new_fact)

    def _select_active_fact_for_update(
        self, cur, user_id: str, namespace: str, fact_key: str
    ) -> ProfileFactRecord | None:
        cur.execute(
            f"""
            SELECT {self._FACT_COLUMNS}
            FROM user_profile_facts
            WHERE user_id = %s AND namespace = %s AND fact_key = %s
              AND status = 'active'
            FOR UPDATE
            """,
            (user_id, namespace, fact_key),
        )
        row = cur.fetchone()
        return ProfileFactRecord.from_row(row) if row else None

    def _select_pending_change_for_update(
        self, cur, user_id: str, namespace: str, fact_key: str
    ) -> ProfileChangeRecord | None:
        cur.execute(
            f"""
            SELECT {self._CHANGE_COLUMNS}
            FROM memory_change_requests
            WHERE user_id = %s AND namespace = %s AND fact_key = %s
              AND status = 'pending'
            FOR UPDATE
            """,
            (user_id, namespace, fact_key),
        )
        row = cur.fetchone()
        return ProfileChangeRecord.from_row(row) if row else None

    def _insert_fact(
        self,
        cur,
        *,
        user_id: str,
        namespace: str,
        fact_key: str,
        value: Any,
        normalized_value: str,
        write_mode: str,
        source_turn_id: uuid.UUID,
        source_excerpt: str | None,
        version: int,
    ) -> ProfileFactRecord:
        cur.execute(
            f"""
            INSERT INTO user_profile_facts (
                fact_id, user_id, namespace, fact_key, fact_value,
                normalized_value, status, confidence, write_mode,
                source_turn_id, source_excerpt, sensitivity, version
            ) VALUES (%s, %s, %s, %s, %s, %s, 'active', 1.0, %s, %s, %s, 'normal', %s)
            RETURNING {self._FACT_COLUMNS}
            """,
            (
                uuid.uuid4(),
                user_id,
                namespace,
                fact_key,
                Jsonb(value),
                normalized_value,
                write_mode,
                source_turn_id,
                source_excerpt,
                version,
            ),
        )
        return ProfileFactRecord.from_row(cur.fetchone())

    def _set_change_status(
        self, cur, change_id: uuid.UUID, status: str
    ) -> ProfileChangeRecord:
        cur.execute(
            f"""
            UPDATE memory_change_requests
            SET status = %s, resolved_at = NOW()
            WHERE change_id = %s
            RETURNING {self._CHANGE_COLUMNS}
            """,
            (status, change_id),
        )
        return ProfileChangeRecord.from_row(cur.fetchone())

    @staticmethod
    def _increment_profile_version(cur, user_id: str) -> None:
        cur.execute(
            """
            INSERT INTO memory_versions (user_id, namespace, version)
            VALUES (%s, 'profile', 1)
            ON CONFLICT (user_id, namespace) DO UPDATE
            SET version = memory_versions.version + 1, updated_at = NOW()
            """,
            (user_id,),
        )

    @staticmethod
    def _required_uuid(value: str | uuid.UUID, *, field_name: str) -> uuid.UUID:
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be a UUID") from exc
