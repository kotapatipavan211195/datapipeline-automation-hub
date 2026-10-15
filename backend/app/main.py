"""FastAPI application: JSON API under /api plus the NiceGUI web UI (one container, one IAP-protected URL).

    uvicorn app.main:create_app --factory --reload     # local (SF_AUTH_MODE=local, workspace in ./output/workspace)
    gunicorn -k uvicorn.workers.UvicornWorker 'app.main:create_app()'   # container
"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import APP_NAME, VERSION
from .api import copy_files, enhance, files, gcs, generate, meta, pipeline, processes, schema, sql, test_runs
from .app_logging import configure_logging, request_id_var, user_var
from .deps import Services, build_services
from .services.workspace import WorkspaceError
from .settings import Settings, get_settings

log = logging.getLogger("dahub.http")


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_format, settings.log_level)
    app = FastAPI(title=APP_NAME, version=VERSION, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.services = services or build_services(settings)

    if settings.auth_mode == "local":  # Vite dev server on another port
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-cloud-trace-context", "").split("/")[0] or uuid.uuid4().hex[:12]
        request_id_var.set(rid)
        user_var.set("-")
        started = time.perf_counter()
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            log.info(
                "%s %s -> %s",
                request.method,
                request.url.path,
                response.status_code,
                extra={"fields": {"status": response.status_code, "latency_ms": int((time.perf_counter() - started) * 1000)}},
            )
        response.headers["x-request-id"] = rid
        return response

    @app.exception_handler(WorkspaceError)
    async def workspace_error(_request: Request, exc: WorkspaceError):
        return JSONResponse(status_code=exc.status, content={"detail": {"message": str(exc)}})

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, exc: Exception):
        if isinstance(exc, HTTPException):
            raise exc
        log.exception("unhandled error")
        return JSONResponse(
            status_code=500,
            content={"detail": {"message": "Something went wrong on the server. Try again; if it keeps happening, share the request id.",
                                "request_id": request_id_var.get()}},
        )

    for router in (meta.router, processes.router, sql.router, schema.router, gcs.router, pipeline.router, generate.router,
                   test_runs.router, enhance.router, copy_files.router, files.router):
        app.include_router(router)

    if settings.ui_enabled:
        from .ui import setup_ui

        setup_ui(app, app.state.services, storage_secret=settings.ui_storage_secret)
    else:
        @app.get("/", include_in_schema=False)
        def root() -> JSONResponse:
            return JSONResponse({"message": f"{APP_NAME} API is running (UI disabled). See /api/docs."})
    return app
