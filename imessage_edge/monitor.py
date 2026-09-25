"""chat.db poller — pushes new inbound iMessages to the orchestrator.

Runs as a daemon thread started from the FastAPI lifespan. Cursor is
persisted to ``settings.state_dir/cursor`` so a daemon restart doesn't
drop messages received during downtime.

Push semantics: at-least-once, with orchestrator-side dedupe on
``date_ns``. Retry/backoff is 1s → 60s exponential; 401/403 stops the
retry loop (bad creds shouldn't generate infinite traffic).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from imessage_edge.chat_db import APPLE_EPOCH_OFFSET, poll_incoming_since
from imessage_edge.contracts import IncomingBatch, IncomingMessageEvent
from imessage_edge.logging import log
from imessage_edge.settings import settings


def _apple_now_ns() -> int:
    """Current wall-clock as an Apple-epoch nanosecond timestamp."""
    unix = datetime.now(timezone.utc).timestamp()
    return int((unix - APPLE_EPOCH_OFFSET) * 1_000_000_000)


def _cursor_file() -> Path:
    return Path(settings.state_dir).expanduser() / "cursor"


def _load_cursor() -> int:
    p = _cursor_file()
    try:
        return int(p.read_text().strip())
    except FileNotFoundError, ValueError:
        # First boot — start from "now" so we don't replay history.
        return _apple_now_ns()


def _save_cursor(date_ns: int) -> None:
    p = _cursor_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(str(date_ns))
    tmp.replace(p)


@dataclass
class MonitorHandle:
    thread: threading.Thread
    stop_event: threading.Event

    def stop(self, timeout: float = 10.0) -> None:
        self.stop_event.set()
        self.thread.join(timeout=timeout)


def start_monitor() -> MonitorHandle | None:
    """Spawn the chat.db poll loop. Returns ``None`` if disabled."""
    if not settings.orchestrator_url:
        log.warning("monitor.disabled.orchestrator_url_unset")
        return None
    if not settings.inbound_token:
        log.warning("monitor.disabled.inbound_token_unset")
        return None

    stop = threading.Event()

    def _loop() -> None:
        cursor = _load_cursor()
        log.info(
            "monitor.started",
            cursor=cursor,
            poll_interval_sec=settings.poll_interval_sec,
            orchestrator_url=settings.orchestrator_url,
        )
        while not stop.is_set():
            try:
                cursor = _poll_once(cursor)
            except Exception:
                log.exception("monitor.poll_error")
            stop.wait(settings.poll_interval_sec)
        log.info("monitor.stopped")

    thread = threading.Thread(target=_loop, daemon=True, name="edge-imessage-monitor")
    thread.start()
    return MonitorHandle(thread=thread, stop_event=stop)


def _poll_once(cursor: int) -> int:
    rows = poll_incoming_since(cursor)
    if not rows:
        return cursor

    events = [
        IncomingMessageEvent(
            handle=row["handle"],
            chat_id=row["chat_id"],
            is_group=row["is_group"],
            text=row["text"],
            service=row["service"],  # ty: ignore[invalid-argument-type]
            timestamp_iso=row["timestamp_iso"],
            date_ns=row["date_ns"],
        )
        for row in rows
    ]
    batch = IncomingBatch(events=events)
    if _push_with_retry(batch):
        new_cursor = rows[-1]["date_ns"]
        _save_cursor(new_cursor)
        return new_cursor
    # Push gave up (auth failure) — leave cursor alone so we retry the
    # same window once the creds are fixed and the daemon restarts.
    return cursor


def _push_with_retry(batch: IncomingBatch) -> bool:
    """POST to the orchestrator with exponential backoff. Returns True on
    success, False on permanent auth failure (caller should NOT advance
    the cursor in that case)."""
    url = f"{settings.orchestrator_url.rstrip('/')}/events/imessage"
    headers = {"Authorization": f"Bearer {settings.inbound_token}"}
    backoff = 1.0
    while True:
        try:
            r = httpx.post(url, json=batch.model_dump(), headers=headers, timeout=10.0)
            if r.status_code == 200:
                log.info(
                    "monitor.push.ok",
                    received=len(batch.events),
                )
                return True
            if r.status_code in (401, 403):
                log.error("monitor.push.auth_failure", status=r.status_code)
                return False
            log.warning(
                "monitor.push.http_error",
                status=r.status_code,
                body_preview=r.text[:200],
            )
        except httpx.HTTPError as exc:
            log.warning("monitor.push.network_error", error=str(exc))
        time.sleep(backoff)
        backoff = min(60.0, backoff * 2)
