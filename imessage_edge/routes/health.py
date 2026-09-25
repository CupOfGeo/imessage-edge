"""GET /health — liveness + chat.db readability."""

from __future__ import annotations

import platform

from fastapi import APIRouter, Depends

from imessage_edge import __version__
from imessage_edge.auth import require_edge_token
from imessage_edge.chat_db import is_available
from imessage_edge.contracts import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health(_: None = Depends(require_edge_token)) -> HealthResponse:
    return HealthResponse(
        macos_version=platform.mac_ver()[0] or "unknown",
        chat_db_readable=is_available(),
        daemon_version=__version__,
    )
