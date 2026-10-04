"""Private FastAPI entrypoint. Only Spring-signed execution tokens are accepted."""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

import jwt
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from webui_new.auth.deps import get_current_user
from webui_new.auth.storage import User
from webui_new.core.errors import register_error_handlers
from utils.io_executor import shutdown_io_executor
from context.postgres_pool import close_all_postgres_pools
from evaluation.sink import close_evaluation_sink
from .business import execution_credential


def decode_execution_token(token):
    path = Path(os.environ["HOMMEY_JWT_PUBLIC_KEY_PATH"])
    claims = jwt.decode(
        token,
        path.read_text(encoding="utf-8"),
        algorithms=["RS256"],
        audience="hommey-agent",
        issuer=os.getenv("HOMMEY_JWT_ISSUER", "hommey-backend"),
        options={"require": ["sub", "exp", "iat", "iss", "aud", "type"]},
    )
    if claims["type"] != "agent" or not str(claims["sub"]).isdigit():
        raise jwt.InvalidTokenError("Invalid execution identity")
    return claims


@asynccontextmanager
async def lifespan(_app):
    # Database migrations belong exclusively to Spring / Flyway in split mode.
    if not os.getenv("HOMMEY_BUSINESS_API_URL") or not os.getenv(
        "HOMMEY_JWT_PUBLIC_KEY_PATH"
    ):
        raise RuntimeError(
            "AI service requires business API and JWT public key configuration"
        )
    try:
        yield
    finally:
        await close_evaluation_sink()
        await shutdown_io_executor()
        close_all_postgres_pools()


async def current_execution_user(request: Request):
    claims = request.state.execution_claims
    return User(
        id=int(claims["sub"]),
        email="",
        password_hash="",
        created_at="",
        role=claims.get("role", "user"),
    )


def create_app(capability_routes=None):
    app = FastAPI(
        title="Hommey AI Service",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    register_error_handlers(app)
    app.dependency_overrides[get_current_user] = current_execution_user

    @app.middleware("http")
    async def authenticate(request, call_next):
        if request.url.path in {"/healthz", "/readyz"}:
            return await call_next(request)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        try:
            claims = decode_execution_token(token)
            user_match = re.match(
                r"^/internal/capabilities/api/(\d+)(?:/|$)", request.url.path
            )
            if user_match and user_match.group(1) != str(claims["sub"]):
                raise jwt.InvalidTokenError("Wrong user scope")
            if request.url.path.endswith(
                ("/chat", "/chat/stream", "/orchestration/interrupt")
            ):
                body = await request.json()
                if not claims.get("session_id") or not claims.get("request_id"):
                    raise jwt.InvalidTokenError("Missing execution scope")
                if (
                    body.get("session_id") != claims["session_id"]
                    or body.get("client_request_id", claims["request_id"])
                    != claims["request_id"]
                ):
                    raise jwt.InvalidTokenError("Wrong execution scope")
            session_match = re.search(
                r"/sessions/([^/]+)/execution-plans$", request.url.path
            )
            if session_match and session_match.group(1) != claims.get("session_id"):
                raise jwt.InvalidTokenError("Wrong session scope")
        except (jwt.InvalidTokenError, ValueError, KeyError):
            return JSONResponse(
                status_code=401,
                content={
                    "success": False,
                    "error": {
                        "code": "UNAUTHORIZED",
                        "message": "执行凭证无效",
                        "details": {},
                    },
                },
            )
        request.state.execution_claims = claims
        if claims.get("request_id"):
            request.scope["headers"] = [
                (k, v)
                for k, v in request.scope["headers"]
                if k.lower() != b"x-request-id"
            ] + [(b"x-request-id", claims["request_id"].encode("ascii"))]
        marker = execution_credential.set(token)
        try:
            response = await call_next(request)
        finally:
            execution_credential.reset(marker)
        iterator = response.body_iterator

        async def scoped_body():
            marker = execution_credential.set(token)
            try:
                async for chunk in iterator:
                    yield chunk
            finally:
                execution_credential.reset(marker)
                await iterator.aclose()

        response.body_iterator = scoped_body()
        return response

    if capability_routes is None:
        # Reuse the existing AI-facing adapters during migration. Public auth,
        # profile, session CRUD and preference routes are deliberately excluded.
        from webui_new.server import app as legacy

        allowed = (
            r"/api/\{user_id\}/chat(?:/stream)?",
            r"/api/\{user_id\}/orchestration/interrupt",
            r"/api/\{user_id\}/sessions/\{session_id\}/execution-plans",
            r"/api/intents",
            r"/api/\{user_id\}/(?:attachments(?:/\{attachment_id\}(?:/content)?)?|asr/transcribe|places/(?:suggest|map))",
            r"/api/admin/skills(?:/\{skill_name\})?",
            r"/api/knowledge/.*",
        )
        capability_routes = [
            route
            for route in legacy.routes
            if any(re.fullmatch(pattern, route.path) for pattern in allowed)
        ]
    router = APIRouter()
    router.routes.extend(capability_routes)
    app.include_router(router, prefix="/internal/capabilities")

    @app.get("/healthz")
    async def health():
        return {"ok": True, "service": "hommey-ai"}

    @app.get("/readyz")
    async def ready():
        from utils.preflight import run_preflight

        result = await run_preflight(include_network=False)
        return JSONResponse(
            status_code=200 if result.get("ok") else 503, content=result
        )

    return app


app = create_app()
