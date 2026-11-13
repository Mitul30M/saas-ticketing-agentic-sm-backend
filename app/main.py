"""FastAPI service and Phase 4 HTTP boundary."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import db
from app.api.admin import router as admin_router
from app.api.metrics import router as metrics_router
from app.api.routes import router
from app.config import settings
from app.retrieval.hybrid import warm_encoder

logger = logging.getLogger(__name__)

app_state: dict[str, db.DictPool] = {}


async def _warm_embedding_model() -> None:
    """Preload the embedding model so the first kb_search is not a cold start.

    A failure is logged, not fatal: tools then hit the timeout and the ticket
    escalates (the safe path) instead of the API failing to boot.
    """
    try:
        seconds = await asyncio.to_thread(warm_encoder)
    except Exception:
        logger.warning("embedding model warmup failed; first kb_search may time out")
        return
    logger.info("embedding model warm in %.2fs", seconds)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    pool = db.create_pool(settings.database_url)
    pool.open(wait=True)
    with pool.connection() as conn:
        db.run_migrations(conn)
    app_state["db"] = pool
    await _warm_embedding_model()
    yield
    pool.close()


app = FastAPI(title="Nimbus Support Ticket Triage", lifespan=lifespan)


@app.middleware("http")
async def enforce_request_body_limit(request: Request, call_next: Any) -> Any:
    """Reject oversized mutations without buffering them first.

    `Content-Length` is checked before the body is read; a chunked request
    without the header is buffered and checked as a fallback.
    """
    if request.method == "POST" and request.url.path.startswith("/api/"):
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                declared_bytes = int(declared)
            except ValueError:
                return JSONResponse(
                    status_code=400, content={"detail": "invalid Content-Length"}
                )
            if declared_bytes > settings.max_request_bytes:
                return JSONResponse(
                    status_code=413, content={"detail": "request body too large"}
                )
        else:
            body = await request.body()
            if len(body) > settings.max_request_bytes:
                return JSONResponse(
                    status_code=413, content={"detail": "request body too large"}
                )
    return await call_next(request)


# The separate frontend repo talks to this backend; origins configurable via env.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_origin_regex=None,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(metrics_router)
if settings.enable_admin_routes:
    app.include_router(admin_router)


def get_pool() -> db.DictPool:
    if "db" not in app_state:
        raise RuntimeError("db pool not initialized")
    return app_state["db"]


@app.get("/healthz")
def healthz() -> dict[str, str]:
    try:
        with get_pool().connection() as conn:
            conn.execute("SELECT 1").fetchone()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="starting up") from None
    except Exception:
        raise HTTPException(status_code=503, detail="database not ready") from None
    return {"status": "ok"}
