"""FastAPI service and Phase 4 HTTP boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import db
from app.api.metrics import router as metrics_router
from app.api.routes import router
from app.config import settings

app_state: dict[str, db.DictPool] = {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = db.create_pool(settings.database_url)
    pool.open(wait=True)
    with pool.connection() as conn:
        db.run_migrations(conn)
    app_state["db"] = pool
    yield
    pool.close()


app = FastAPI(title="Nimbus Support Ticket Triage", lifespan=lifespan)


@app.middleware("http")
async def enforce_ticket_body_limit(request: Request, call_next: Any) -> Any:
    if request.method == "POST" and request.url.path == "/api/tickets":
        body = await request.body()
        if len(body) > settings.max_request_bytes:
            return JSONResponse(status_code=413, content={"detail": "request body too large"})
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
