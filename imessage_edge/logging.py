"""Tiny structlog setup for the edge daemon.

Kept minimal because the daemon doesn't need rotating file handlers or
JSON output toggles — uvicorn captures stdout already.
"""

from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(level: int = logging.INFO) -> None:
    """Configure structlog + stdlib once for the daemon process."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


log = structlog.get_logger("imessage_edge")
