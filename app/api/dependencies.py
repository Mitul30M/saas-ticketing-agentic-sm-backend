"""Shared API dependencies for pool access and fail-closed authentication."""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException

from app.config import settings


def protected(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if not settings.agent_token:
        raise HTTPException(status_code=503, detail="dashboard authentication is not configured")
    expected = f"Bearer {settings.agent_token}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def pool_dependency() -> Any:
    from app.main import get_pool

    return get_pool()


Pool = Annotated[Any, Depends(pool_dependency)]
Protected = Annotated[None, Depends(protected)]
