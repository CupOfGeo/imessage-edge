"""Bearer-token verification for inbound requests."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from imessage_edge.settings import settings


async def require_edge_token(authorization: str | None = Header(default=None)) -> None:
    """Reject requests without a matching ``Authorization: Bearer …`` header.

    Fail-closed: an empty configured token always rejects, so a
    misconfigured daemon can't accidentally accept anonymous traffic.
    """
    expected = settings.edge_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IMESSAGE_EDGE_TOKEN not configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    presented = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
