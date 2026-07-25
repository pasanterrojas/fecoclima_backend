from __future__ import annotations

import time
import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import ORJSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.endpoints.health import health, ready
from app.api.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging

configure_logging()
settings.validate_for_production()
logger = structlog.get_logger(__name__)

REQUESTS = Counter(
    "fecoclima_http_requests_total",
    "Solicitudes HTTP procesadas",
    ["method", "path", "status"],
)
DURATION = Histogram(
    "fecoclima_http_request_duration_seconds",
    "Duración de solicitudes HTTP",
    ["method", "path"],
)

app = FastAPI(
    title=settings.app_name,
    description="Backend de monitoreo agrometeorológico, fenología, alertas determinísticas, RAG y reportes FECOCLIMA IA.",
    version="1.1.0",
    default_response_class=ORJSONResponse,
    docs_url="/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url="/openapi.json",
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
app.add_middleware(GZipMiddleware, minimum_size=1200)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Accept", "Content-Type", "X-CSRF-Token", "X-Request-ID"],
    expose_headers=["X-Request-ID", "X-Checksum-SHA256"],
)


@app.middleware("http")
async def security_and_logging(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()
    route_path = request.url.path
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "request_failed",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        raise
    elapsed = time.perf_counter() - started
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = (
            "no-store" if request.url.path.startswith(settings.api_v1_prefix) else "no-cache"
        )
    if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if settings.metrics_enabled:
        REQUESTS.labels(request.method, route_path, str(response.status_code)).inc()
        DURATION.labels(request.method, route_path).observe(elapsed)
    logger.info(
        "request_complete",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=round(elapsed * 1000, 2),
    )
    return response


def _serializable_validation_errors(exc: RequestValidationError) -> list[dict]:
    errors: list[dict] = []
    for item in exc.errors():
        clean = dict(item)
        ctx = clean.get("ctx")
        if isinstance(ctx, dict):
            clean["ctx"] = {
                key: str(value) if isinstance(value, BaseException) else value
                for key, value in ctx.items()
            }
        input_value = clean.get("input")
        try:
            import orjson
            orjson.dumps(input_value)
        except Exception:
            clean["input"] = str(input_value)
        errors.append(clean)
    return errors


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    errors = _serializable_validation_errors(exc)
    message = next(
        (str(item.get("msg", "")).removeprefix("Value error, ").strip() for item in errors if item.get("msg")),
        "Datos de entrada inválidos",
    )
    return ORJSONResponse(
        status_code=422,
        content={
            "detail": message or "Datos de entrada inválidos",
            "errors": errors,
            "request_id": getattr(request.state, "request_id", None),
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    return ORJSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "request_id": getattr(request.state, "request_id", None)},
    )


@app.exception_handler(Exception)
async def unhandled_error(request: Request, _: Exception):
    return ORJSONResponse(
        status_code=500,
        content={
            "detail": "Ocurrió un error interno. Consulte el identificador de solicitud en los logs.",
            "request_id": getattr(request.state, "request_id", None),
        },
    )


app.include_router(api_router, prefix=settings.api_v1_prefix)
app.add_api_route("/health", health, methods=["GET"], include_in_schema=False)
app.add_api_route("/ready", ready, methods=["GET"], include_in_schema=False)


@app.get("/metrics", include_in_schema=False)
async def metrics():
    if not settings.metrics_enabled:
        return Response(status_code=404)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": settings.app_name,
        "version": "1.1.0",
        "docs": "/docs",
        "health": "/health",
        "ready": "/ready",
    }
