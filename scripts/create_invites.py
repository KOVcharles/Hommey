"""Generate one-use invitation codes for the owner to distribute.

Run from the project root: python scripts/create_invites.py --count 5
Requires the same HOMMEY_POSTGRES_DSN as the web service.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webui_new.auth.invites import create_invite, generate_invite_code  # noqa: E402
from webui_new.auth.storage import apply_migration, get_conn  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="创建一次性 Hommey 邀请码")
    parser.add_argument("--count", type=int, default=1, help="生成数量（1-100）")
    args = parser.parse_args()
    if not 1 <= args.count <= 100:
        parser.error("--count 必须在 1 到 100 之间")

    codes: list[str] = []
    with get_conn() as conn:
        apply_migration(conn)
        with conn.transaction():
            while len(codes) < args.count:
                code = generate_invite_code()
                if create_invite(conn, code):
                    codes.append(code)
    for code in codes:
        print(code)


if __name__ == "__main__":
    main()
