"""Entry point: ``python -m imessage_edge``."""

from __future__ import annotations

import uvicorn

from imessage_edge.settings import settings


def main() -> None:
    uvicorn.run(
        "imessage_edge.app:app",
        host=settings.bind_host,
        port=settings.bind_port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
