"""
WebUI error handling.

This module owns request_id middleware, application error types, and the
external API error response contract.
"""
import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from utils.logging_safety import sanitize_for_log
from utils.observability import COMPONENT_HTTP, record_app_error, record_http_request

logger = logging.getLogger(__name__)

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # microphone=(self) 允许同源页面 getUserMedia 录音（语音输入 Mode A）；
    # 相机与定位保持禁用。
    "Permissions-Policy": "camera=(), microphone=(self), geolocation=()",
}


def _apply_security_headers(response):
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


class AppError(Exception):
    """Base application error with public response fields and log metadata."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 500,
        details: dict | None = None,
        retryable: bool = False,
        log_level: int = logging.ERROR,
        component: str = COMPONENT_HTTP,
        debug_message: str | None = None,
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.retryable = retryable
        self.log_level = log_level
        self.component = component
        self.debug_message = debug_message
        super().__init__(message)


class ValidationError(AppError):
    def __init__(self, code: str = "VALIDATION_ERROR", message: str = "请求参数格式不正确，请检查后重试", details: dict | None = None):
        super().__init__(code, message, status_code=400, details=details, retryable=False, log_level=logging.WARNING)


class ConfigError(AppError):
    def __init__(self, code: str = "CONFIG_ERROR", message: str = "系统配置暂时不可用，请稍后重试", details: dict | None = None):
        super().__init__(code, message, status_code=500, details=details, retryable=False, log_level=logging.ERROR)


class UpstreamError(AppError):
    def __init__(
        self,
        code: str = "UPSTREAM_ERROR",
        message: str = "上游服务暂时不可用，请稍后重试",
        details: dict | None = None,
        retryable: bool = True,
        component: str = "upstream",
        debug_message: str | None = None,
    ):
        super().__init__(
            code,
            message,
            status_code=502,
            details=details,
            retryable=retryable,
            log_level=logging.ERROR,
            component=component,
            debug_message=debug_message,
        )


class StorageError(AppError):
    def __init__(self, code: str = "STORAGE_ERROR", message: str = "数据保存失败，请稍后重试", details: dict | None = None):
        super().__init__(code, message, status_code=500, details=details, retryable=True, log_level=logging.ERROR, component="storage")


class BusinessError(AppError):
    def __init__(
        self,
        code: str = "BUSINESS_ERROR",
        message: str = "请求无法处理，请检查后重试",
        status_code: int = 400,
        details: dict | None = None,
        retryable: bool = False,
    ):
        super().__init__(code, message, status_code=status_code, details=details, retryable=retryable, log_level=logging.INFO)


class InternalError(AppError):
    def __init__(self, code: str = "INTERNAL_ERROR", message: str = "系统暂时不可用，请稍后再试", details: dict | None = None):
        super().__init__(code, message, status_code=500, details=details, retryable=True, log_level=logging.ERROR, component=COMPONENT_HTTP)


class ApiError(AppError):
    """Compatibility wrapper for the Phase 1 error class signature."""

    def __init__(self, status_code: int, code: str, message: str):
        log_level = logging.WARNING if status_code < 500 else logging.ERROR
        super().__init__(code, message, status_code=status_code, details={}, retryable=status_code >= 500, log_level=log_level)


def request_id(request: Request) -> str:
    """从 request.state 读取当前请求 ID；未经过 middleware 时返回空字符串。"""
    return getattr(request.state, "request_id", "")


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: dict | None = None,
) -> JSONResponse:
    """Build the public Phase 2 API error response."""
    rid = request_id(request)
    response = JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
                "request_id": rid,
            },
        },
        headers={"X-Request-ID": rid} if rid else None,
    )
    return _apply_security_headers(response)


def app_error_response(request: Request, exc: AppError) -> JSONResponse:
    return error_response(request, exc.status_code, exc.code, exc.message, exc.details)


def stream_error_event(request: Request, exc: Exception) -> dict:
    """Convert exceptions to the public stream error event shape."""
    app_exc = exc if isinstance(exc, AppError) else InternalError()
    return {
        "type": "error",
        "code": app_exc.code,
        "message": app_exc.message,
        "request_id": request_id(request),
        "retryable": app_exc.retryable,
    }


async def request_context_middleware(request: Request, call_next):
    """为每个请求补 request_id，并记录基础访问日志。"""
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request.state.request_id = rid
    start = time.perf_counter()
    request.state.request_started_at = start
    status_code = 500
    try:
        response = await call_next(request)
    except Exception as exc:
        app_exc = exc if isinstance(exc, AppError) else InternalError()
        status_code = app_exc.status_code
        duration_ms = int((time.perf_counter() - start) * 1000)
        record_app_error(app_exc.component, app_exc.code, app_exc.status_code)
        record_http_request(request.url.path, request.method, status_code, duration_ms)
        logger.log(
            app_exc.log_level,
            "request_error",
            extra=_error_log_extra(
                request=request,
                status_code=app_exc.status_code,
                error_code=app_exc.code,
                component=app_exc.component,
                duration_ms=duration_ms,
                debug_message=app_exc.debug_message or str(exc),
            ),
        )
        return app_error_response(request, app_exc)
    status_code = response.status_code
    duration_ms = int((time.perf_counter() - start) * 1000)
    record_http_request(request.url.path, request.method, status_code, duration_ms)
    response.headers["X-Request-ID"] = rid
    _apply_security_headers(response)
    logger.info(
        "request",
        extra={
            "request_id": rid,
            "user_id": _path_user_id(request),
            "route": request.url.path,
            "method": request.method,
            "status_code": status_code,
            "duration_ms": duration_ms,
            "component": COMPONENT_HTTP,
        },
    )
    return response


async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """把 FastAPI/Starlette 的 HTTPException 转成统一错误响应。"""
    code = "BAD_REQUEST" if exc.status_code < 500 else "HTTP_ERROR"
    message = str(exc.detail) if exc.status_code < 500 else "请求处理失败，请稍后重试"
    record_app_error(COMPONENT_HTTP, code, exc.status_code)
    logger.warning(
        "http_error",
        extra=_error_log_extra(
            request=request,
            status_code=exc.status_code,
            error_code=code,
            component=COMPONENT_HTTP,
            duration_ms=_duration_ms(request),
            debug_message=str(exc.detail),
        ),
    )
    return error_response(request, exc.status_code, code, message)


async def app_error_handler(request: Request, exc: AppError):
    """Convert AppError into the unified response contract."""
    record_app_error(exc.component, exc.code, exc.status_code)
    logger.log(
        exc.log_level,
        "app_error",
        extra=_error_log_extra(
            request=request,
            status_code=exc.status_code,
            error_code=exc.code,
            component=exc.component,
            duration_ms=_duration_ms(request),
            debug_message=exc.debug_message,
        ),
    )
    return app_error_response(request, exc)


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """隐藏 Pydantic 原始校验细节，避免直接暴露内部字段结构。"""
    record_app_error(COMPONENT_HTTP, "VALIDATION_ERROR", 422)
    logger.warning(
        "validation_error",
        extra=_error_log_extra(
            request=request,
            status_code=422,
            error_code="VALIDATION_ERROR",
            component=COMPONENT_HTTP,
            duration_ms=_duration_ms(request),
            debug_message=str(exc.errors()),
        ),
    )
    return error_response(request, 422, "VALIDATION_ERROR", "请求参数格式不正确，请检查后重试")


def register_error_handlers(app: FastAPI) -> None:
    """在 app 创建后调用一次，集中注册 middleware 和 exception handlers。"""
    app.middleware("http")(request_context_middleware)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)


def _error_log_extra(
    request: Request,
    status_code: int,
    error_code: str,
    component: str,
    duration_ms: int | None,
    debug_message: str | None = None,
) -> dict:
    return {
        "request_id": request_id(request),
        "user_id": _path_user_id(request),
        "route": request.url.path,
        "method": request.method,
        "status_code": status_code,
        "error_code": error_code,
        "component": component,
        "duration_ms": duration_ms,
        "debug_message": sanitize_for_log(debug_message or ""),
    }


def _path_user_id(request: Request) -> str:
    return str(request.path_params.get("user_id") or "")


def _duration_ms(request: Request) -> int | None:
    started_at = getattr(request.state, "request_started_at", None)
    if started_at is None:
        return None
    return int((time.perf_counter() - started_at) * 1000)
