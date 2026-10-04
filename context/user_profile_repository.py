"""Versioned, user-scoped JSON profile storage. No agent writes this table."""
from __future__ import annotations

from uuid import uuid4

from core.user_profile import PersonalProfile


class ProfileConflict(Exception):
    pass


class UserProfileRepository:
    def __init__(self, pool):
        self.pool = pool

    def get(self, user_id: str):
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT profile, onboarding_status, revision, updated_at FROM user_personal_profiles WHERE user_id=%s", (str(user_id),))
            row = cur.fetchone()
        if not row:
            return {"profile": PersonalProfile().model_dump(mode="json"), "onboarding_status": "pending", "revision": 0, "updated_at": None}
        return {"profile": PersonalProfile.model_validate(row["profile"]).model_dump(mode="json"),
                "onboarding_status": row["onboarding_status"], "revision": row["revision"],
                "updated_at": row["updated_at"].isoformat()}

    def save(self, user_id: str, profile: PersonalProfile, revision: int):
        data = profile.model_dump(mode="json")
        for project in data["funding"]["projects"]:
            if not project["id"]:
                project["id"] = str(uuid4())
        return self._write(user_id, data, "completed", revision)

    def skip(self, user_id: str, revision: int):
        # UPDATE keeps all previously saved data intact; new rows use the defaults.
        return self._write(user_id, None, "skipped", revision)

    def _write(self, user_id, data, status, revision):
        from psycopg.types.json import Jsonb
        default = PersonalProfile().model_dump(mode="json")
        with self.pool.connection() as conn, conn.cursor() as cur:
            if revision == 0:
                cur.execute("""INSERT INTO user_personal_profiles (user_id, profile, onboarding_status, revision)
                    VALUES (%s,%s,%s,1) ON CONFLICT (user_id) DO NOTHING
                    RETURNING profile,onboarding_status,revision,updated_at""", (str(user_id), Jsonb(data or default), status))
            else:
                cur.execute("""UPDATE user_personal_profiles SET profile=COALESCE(%s,profile),
                    onboarding_status=%s,revision=revision+1,updated_at=NOW()
                    WHERE user_id=%s AND revision=%s
                    RETURNING profile,onboarding_status,revision,updated_at""", (Jsonb(data) if data is not None else None, status, str(user_id), revision))
            row = cur.fetchone()
            if not row:
                raise ProfileConflict()
        return {"profile": row["profile"], "onboarding_status": row["onboarding_status"],
                "revision": row["revision"], "updated_at": row["updated_at"].isoformat()}
