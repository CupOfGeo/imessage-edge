"""Monitor tests: cursor persistence, push retry semantics, poll loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from imessage_edge import monitor
from imessage_edge.chat_db import IncomingMessage
from imessage_edge.contracts import IncomingBatch, IncomingMessageEvent
from imessage_edge.settings import settings


@pytest.fixture(autouse=True)
def _state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "state_dir", tmp_path)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(monitor.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


class _Resp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


def _batch() -> IncomingBatch:
    return IncomingBatch(
        events=[
            IncomingMessageEvent(
                handle="+15550001111",
                chat_id="+15550001111",
                is_group=False,
                text="hi",
                service="iMessage",
                timestamp_iso="2026-01-01 00:00:00",
                date_ns=123,
            )
        ]
    )


def _row(date_ns: int) -> IncomingMessage:
    return IncomingMessage(
        handle="+15550001111",
        chat_id="+15550001111",
        is_group=False,
        text="hi",
        service="iMessage",
        timestamp_iso="2026-01-01 00:00:00",
        date_ns=date_ns,
    )


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


def test_load_cursor_missing_file_defaults_to_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(monitor, "_apple_now_ns", lambda: 4242)
    assert monitor._load_cursor() == 4242


def test_load_cursor_corrupt_file_defaults_to_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "cursor").write_text("not a number")
    monkeypatch.setattr(monitor, "_apple_now_ns", lambda: 9999)
    assert monitor._load_cursor() == 9999


def test_save_cursor_roundtrip(tmp_path: Path) -> None:
    monitor._save_cursor(123456789)
    assert monitor._load_cursor() == 123456789
    assert (tmp_path / "cursor").exists()
    assert not (tmp_path / "cursor.tmp").exists()


# ---------------------------------------------------------------------------
# _push_with_retry
# ---------------------------------------------------------------------------


def test_push_200_returns_true(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    monkeypatch.setattr(monitor.httpx, "post", lambda *a, **k: _Resp(200))
    assert monitor._push_with_retry(_batch()) is True
    assert no_sleep == []


@pytest.mark.parametrize("status", [401, 403])
def test_push_auth_failure_returns_false_without_retry(
    status: int, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        monitor.httpx, "post", lambda *a, **k: calls.append(1) or _Resp(status)
    )
    assert monitor._push_with_retry(_batch()) is False
    assert len(calls) == 1
    assert no_sleep == []


def test_push_5xx_then_200_retries_with_backoff(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    responses = iter([_Resp(500, "boom"), _Resp(503), _Resp(502), _Resp(200)])
    monkeypatch.setattr(monitor.httpx, "post", lambda *a, **k: next(responses))
    assert monitor._push_with_retry(_batch()) is True
    assert no_sleep == [1.0, 2.0, 4.0]


def test_push_network_error_then_200_retries(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    attempts = {"n": 0}

    def fake_post(*a: Any, **k: Any) -> _Resp:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("refused")
        return _Resp(200)

    monkeypatch.setattr(monitor.httpx, "post", fake_post)
    assert monitor._push_with_retry(_batch()) is True
    assert no_sleep == [1.0]


def test_push_sends_bearer_and_events_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _Resp:
        seen["url"] = url
        seen["headers"] = kwargs["headers"]
        seen["json"] = kwargs["json"]
        return _Resp(200)

    monkeypatch.setattr(settings, "orchestrator_url", "http://orch.example/")
    monkeypatch.setattr(settings, "inbound_token", "inbound-secret")
    monkeypatch.setattr(monitor.httpx, "post", fake_post)
    monitor._push_with_retry(_batch())
    assert seen["url"] == "http://orch.example/events/imessage"
    assert seen["headers"] == {"Authorization": "Bearer inbound-secret"}
    assert seen["json"]["edge_id"] == "imessage"
    assert seen["json"]["events"][0]["date_ns"] == 123


# ---------------------------------------------------------------------------
# _poll_once
# ---------------------------------------------------------------------------


def test_poll_once_no_rows_returns_same_cursor_without_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(monitor, "poll_incoming_since", lambda since: [])
    monkeypatch.setattr(
        monitor, "_push_with_retry", lambda b: pytest.fail("should not push")
    )
    assert monitor._poll_once(100) == 100


def test_poll_once_advances_cursor_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor, "poll_incoming_since", lambda since: [_row(101), _row(150)]
    )
    monkeypatch.setattr(monitor, "_push_with_retry", lambda b: True)
    assert monitor._poll_once(100) == 150
    assert monitor._load_cursor() == 150


def test_poll_once_keeps_cursor_on_auth_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(monitor, "poll_incoming_since", lambda since: [_row(101)])
    monkeypatch.setattr(monitor, "_push_with_retry", lambda b: False)
    assert monitor._poll_once(100) == 100
    assert not (tmp_path / "cursor").exists()


# ---------------------------------------------------------------------------
# start_monitor
# ---------------------------------------------------------------------------


def test_start_monitor_disabled_without_orchestrator_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "orchestrator_url", "")
    monkeypatch.setattr(settings, "inbound_token", "x")
    assert monitor.start_monitor() is None


def test_start_monitor_disabled_without_inbound_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "orchestrator_url", "http://orch.example")
    monkeypatch.setattr(settings, "inbound_token", "")
    assert monitor.start_monitor() is None
