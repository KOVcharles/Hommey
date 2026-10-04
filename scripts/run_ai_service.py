"""Run the private AI service against the isolated local development stack."""

from pathlib import Path
import os
import sys
from urllib.parse import quote

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    load_dotenv(ROOT / ".env.engineering", override=False)
    password = os.getenv("HOMMEY_AI_DB_PASSWORD")
    if not password:
        raise SystemExit(
            "Run scripts/prepare_engineering.py before starting the AI service."
        )
    port = int(os.getenv("HOMMEY_DB_PORT", "15432"))
    dsn = f"postgresql://hommey_ai:{quote(password, safe='')}@127.0.0.1:{port}/hommey?connect_timeout=5"
    local = {
        "HOMMEY_SCHEMA_OWNER": "spring",
        "HOMMEY_BUSINESS_API_URL": "http://localhost:8080",
        "HOMMEY_JWT_PUBLIC_KEY_PATH": str(ROOT / ".secrets" / "jwt-public.pem"),
        "HOMMEY_LONG_TERM_BACKEND": "postgres",
        "HOMMEY_POSTGRES_DSN": dsn,
        "HOMMEY_SHORT_TERM_BACKEND": "redis",
        "HOMMEY_REDIS_HOST": "localhost",
        "HOMMEY_REDIS_PORT": "56380",
        "HOMMEY_RAG_VECTOR_BACKEND": "postgres",
        "HOMMEY_RAG_REFRESH_BACKEND": "postgres",
        "HOMMEY_RAG_POSTGRES_DSN": dsn,
    }
    for key, value in local.items():
        os.environ.setdefault(key, value)
    import uvicorn

    uvicorn.run("ai_service.server:app", host="127.0.0.1", port=18001)


if __name__ == "__main__":
    main()
