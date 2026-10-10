"""FastAPI factory for the iMessage edge daemon."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from imessage_edge import __version__
from imessage_edge.logging import log, setup_logging
from imessage_edge.monitor import MonitorHandle, start_monitor
from imessage_edge.routes import health, messages, send


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    handle: MonitorHandle | None = start_monitor()
    app.state.monitor_handle = handle
    try:
        yield
    finally:
        if handle is not None:
            handle.stop()


def create_app() -> FastAPI:
    setup_logging()
    log.info("imessage_edge.starting", version=__version__)

    app = FastAPI(
        title="imessage-edge",
        version=__version__,
        description=(
            "Mac-side HTTP daemon exposing iMessage send/read for a remote "
            "orchestrator over Tailscale."
        ),
        lifespan=_lifespan,
        # Schema pages would be served without the bearer check.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(health.router)
    app.include_router(send.router)
    app.include_router(messages.router)
    return app


app = create_app()
