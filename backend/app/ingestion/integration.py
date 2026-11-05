"""Register ingestion in the existing API process and authenticated session."""

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from .history_cleanup import CleanupConflict
from .logging_setup import request_id
from .main import BodyLimit, create_app

logger = logging.getLogger(__name__)
PREFIX = "/api/ingestion/"


class ScopedBodyLimit:
    def __init__(self, app):
        self.app = app
        maximum = int(os.environ.get("MAX_BATCH_MB", "20480")) * 1024**2 + 1024**2
        self.limited = BodyLimit(app, maximum)

    async def __call__(self, scope, receive, send):
        target = self.limited if scope.get("path", "").startswith(PREFIX) else self.app
        return await target(scope, receive, send)


def install(parent, auth):
    child = create_app()
    router = APIRouter()
    router.routes.extend(
        route for route in child.routes
        if isinstance(route, APIRoute) and route.path.startswith(PREFIX)
    )
    parent.include_router(router, tags=["知识入库"], dependencies=[Depends(auth)])
    for kind, handler in child.exception_handlers.items():
        if kind.__module__.startswith("app.ingestion"):
            parent.add_exception_handler(kind, handler)
    parent.add_middleware(ScopedBodyLimit)

    @parent.middleware("http")
    async def intake_guard(request: Request, call_next):
        if not request.url.path.startswith(PREFIX):
            return await call_next(request)
        correlation = uuid.uuid4().hex
        context_token = request_id.set(correlation)
        started = time.monotonic()
        try:
            try:
                # Authenticate before Starlette reads a potentially large multipart body.
                await asyncio.to_thread(auth, request)
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    cleanup = request.url.path.startswith(PREFIX + "history/cleanup")
                    with child.state.mutation_gate.enter(cleanup=cleanup):
                        response = await call_next(request)
                else:
                    response = await call_next(request)
            except HTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            except CleanupConflict as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=409)
            except Exception:
                logger.exception("入库接口处理异常", extra={"method": request.method, "path": request.url.path})
                response = JSONResponse({"detail": "服务处理失败，请凭请求编号查看服务日志"}, status_code=500)
            response.headers.update({
                "X-Request-ID": correlation,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
            })
            response.headers.setdefault("Cache-Control", "no-store")
            if request.url.path != PREFIX + "health":
                logger.log(logging.WARNING if response.status_code >= 400 else logging.INFO,
                    "入库 HTTP 响应头已返回", extra={"method": request.method, "path": request.url.path,
                    "http_status": response.status_code, "elapsed_ms": round((time.monotonic()-started)*1000, 2)})
            return response
        finally:
            request_id.reset(context_token)
    return child


@asynccontextmanager
async def lifecycle(child):
    async with child.router.lifespan_context(child):
        yield
