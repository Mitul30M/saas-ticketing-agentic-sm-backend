"""Dev-only admin routes (seed / ingest KB / load tickets).

Mounted only when `ENABLE_ADMIN_ROUTES=true`; they call the exact same
functions as the Makefile targets so the two paths cannot drift.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.dependencies import Pool, Protected
from app.config import settings

router = APIRouter(prefix="/api/admin", dependencies=[])


@router.post("/seed")
def admin_seed(_: Protected, __: Pool) -> dict[str, Any]:
    from db.seed.seed import main as seed_main

    seed_main()
    return {"status": "seeded"}


@router.post("/ingest-kb")
def admin_ingest_kb(_: Protected, __: Pool) -> dict[str, Any]:
    from scripts.ingest_kb import KB_DIR, ingest

    articles, chunks = ingest(KB_DIR, settings.embed_model)
    return {"articles": articles, "chunks": chunks}


@router.post("/load-tickets")
def admin_load_tickets(_: Protected, __: Pool) -> dict[str, Any]:
    from scripts.load_tickets import load_tickets

    return load_tickets()
