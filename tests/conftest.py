"""Shared fixtures.

Environment is pinned *before* any ``imessage_edge`` import: ``settings``
is constructed at module import time and would otherwise read the real
``.env`` in the repo root. Pointing ``IMESSAGE_EDGE_CONFIG`` at a
nonexistent file makes pydantic-settings skip the env file, and leaving
``ORCHESTRATOR_URL`` unset keeps the chat.db monitor disabled.
"""

from __future__ import annotations

import os

os.environ["IMESSAGE_EDGE_CONFIG"] = "/nonexistent/imessage-edge-test.env"
os.environ["IMESSAGE_EDGE_TOKEN"] = "test-token"
os.environ.pop("IMESSAGE_INBOUND_TOKEN", None)
os.environ.pop("ORCHESTRATOR_URL", None)

from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from imessage_edge.app import app  # noqa: E402
from imessage_edge.routes import send as send_route  # noqa: E402

TEST_TOKEN = "test-token"


@pytest.fixture
def client() -> TestClient:
    # No ``with``: the lifespan (and thus the monitor thread) never runs.
    return TestClient(app)


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _clear_idempotency_cache() -> Iterator[None]:
    send_route._idem.clear()
    yield
    send_route._idem.clear()
