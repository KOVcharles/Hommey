"""One-use invitation codes. Only SHA-256 digests are persisted."""
import hashlib
import secrets


_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def normalize_invite_code(code: str) -> str:
    """Allow pasted spaces and optional grouping hyphens."""
    return "".join(code.split()).replace("-", "").upper()


def invite_digest(code: str) -> str:
    return hashlib.sha256(normalize_invite_code(code).encode("utf-8")).hexdigest()


def generate_invite_code() -> str:
    """Generate a 100-bit, grouped code for an owner to distribute."""
    chunks = ["".join(secrets.choice(_ALPHABET) for _ in range(5)) for _ in range(4)]
    return "HMY-" + "-".join(chunks)


def create_invite(conn, code: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO invite_codes (code_hash) VALUES (%s) ON CONFLICT DO NOTHING RETURNING code_hash",
            (invite_digest(code),),
        )
        return cur.fetchone() is not None


def invite_available(conn, code: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM invite_codes WHERE code_hash = %s AND used_at IS NULL",
            (invite_digest(code),),
        )
        return cur.fetchone() is not None


def consume_invite(conn, code: str, user_id: int) -> bool:
    """Call inside the same DB transaction as user creation."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE invite_codes SET used_at = now(), used_by_user_id = %s "
            "WHERE code_hash = %s AND used_at IS NULL RETURNING code_hash",
            (user_id, invite_digest(code)),
        )
        return cur.fetchone() is not None
