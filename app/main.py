"""FastAPI service: GET /healthz for now; tickets/approvals routers come in Phase 4."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app import db
from app.config import settings

app_state: dict[str, db.DictConn] = {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    conn = db.connect(settings.database_url)
    db.run_migrations(conn)
    app_state["db"] = conn
    yield
    conn.close()


app = FastAPI(title="Nimbus Support Ticket Triage", lifespan=lifespan)

# The separate frontend repo talks to this backend; origins configurable via env.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"^http://localhost(:\d+)?$",  # local dev origins only for now
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_conn() -> db.DictConn:
    import time

    deadline = time.monotonic() + 1.0
    while "db" not in app_state and time.monotonic() < deadline:
        time.sleep(0.05)
    if "db" not in app_state:
        raise RuntimeError("db connection not initialized")
    return app_state["db"]


@app.get("/healthz")
def healthz() -> dict[str, str]:
    try:
        conn = get_conn()
        conn.execute("SELECT 1").fetchone()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="starting up") from None
    except Exception:
        raise HTTPException(status_code=503, detail="database not ready") from None
    return {"status": "ok"}
