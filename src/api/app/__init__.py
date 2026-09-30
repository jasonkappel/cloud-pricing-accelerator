import logging
import os
import sys
import time

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.approvals import approval_settings
from app.auth import AuthMode, auth_mode
from app.data import application_max_age_hours, application_repository
from app.intake import MAX_UPLOAD_BYTES
from app.pricebook_routes import router as pricebook_router
from app.routes import router

MAX_MULTIPART_OVERHEAD_BYTES = 1024 * 1024


def _request_logger() -> logging.Logger:
    # Method, path, status, and duration only: never query strings, headers, or bodies.
    logger = logging.getLogger("app.requests")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def create_app() -> FastAPI:
    # Fail startup on an invalid retention setting instead of on the first request.
    application_max_age_hours()
    mode = auth_mode()
    # Fail startup on invalid approval storage or signing-key settings.
    approval_settings()
    app = FastAPI(
        title="Cloud Pricing Accelerator API",
        description="Application shell for the public-list run-rate benchmark.",
        version="0.1.0",
    )
    if mode == AuthMode.LOCAL:
        # In Azure the web app proxies /api same-origin, so cross-origin calls are never needed there.
        origins = [
            origin.strip()
            for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
            if origin.strip()
        ]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(router)
    app.include_router(pricebook_router)
    request_log = _request_logger()

    @app.middleware("http")
    async def log_request_timing(request: Request, call_next):
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            request_log.info(
                "%s %s %s %.0fms",
                request.method,
                request.url.path,
                status_code,
                (time.perf_counter() - started) * 1000,
            )

    @app.middleware("http")
    async def enforce_intake_content_length(request: Request, call_next):
        if request.method == "POST" and request.url.path == "/api/intakes":
            transfer_encoding = request.headers.get("transfer-encoding", "").casefold()
            content_length = request.headers.get("content-length")
            if transfer_encoding or content_length is None:
                return JSONResponse(
                    status_code=status.HTTP_411_LENGTH_REQUIRED,
                    content={"detail": "Intake uploads require a Content-Length header."},
                )
            try:
                length = int(content_length)
            except ValueError:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Content-Length must be a valid non-negative integer."},
                )
            if length < 0:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Content-Length must be a valid non-negative integer."},
                )
            if length > MAX_UPLOAD_BYTES + MAX_MULTIPART_OVERHEAD_BYTES:
                return JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content={"detail": "The Intake upload exceeds the bounded request size."},
                )
        return await call_next(request)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz():
        if application_repository.is_ready():
            return {"status": "ready"}

        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content={"status": "not ready"})

    return app
