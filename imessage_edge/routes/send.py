"""POST /imessage/send and POST /imessage/send-to-chat — proxies to
Messages.app via AppleScript.

Both routes share one idempotency cache and one error mapping
(``_run_send``); they differ only in how the target is resolved.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import cast

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from imessage_edge.auth import require_edge_token
from imessage_edge.chat_db import resolve_chat, send_message, send_to_chat
from imessage_edge.contracts import (
    IMessageService,
    SendErrorCode,
    SendErrorResponse,
    SendRequest,
    SendResponse,
    SendToChatRequest,
)
from imessage_edge.logging import log

router = APIRouter()


# Idempotency cache: request_id → (cached_at_unix, status_code, body_dict).
# In-memory; lost on restart. ~120s TTL is enough to cover orchestrator
# retries without growing unbounded.
_IDEM_TTL_SEC = 120
_idem: dict[str, tuple[float, int, dict]] = {}
_idem_lock = threading.Lock()


class _ChatNotFound(LookupError):
    pass


def _classify(stderr_or_msg: str) -> SendErrorCode:
    """Map an osascript ``RuntimeError`` message to a structured error code."""
    low = stderr_or_msg.lower()
    if "-1743" in low or "not allowed" in low:
        return SendErrorCode.PERMISSION_DENIED
    if "participant" in low and ("unknown" in low or "can't get" in low):
        return SendErrorCode.UNKNOWN_PARTICIPANT
    return SendErrorCode.APPLESCRIPT_ERROR


def _idem_get(request_id: str) -> tuple[int, dict] | None:
    with _idem_lock:
        cached = _idem.get(request_id)
        if cached and time.time() - cached[0] < _IDEM_TTL_SEC:
            return cached[1], cached[2]
    return None


def _idem_set(request_id: str, status: int, body: dict) -> None:
    now = time.time()
    with _idem_lock:
        # Sweep expired entries on every write so the cache stays bounded
        # by the number of sends in the last TTL window, not by uptime.
        expired = [k for k, (at, _, _) in _idem.items() if now - at >= _IDEM_TTL_SEC]
        for k in expired:
            del _idem[k]
        _idem[request_id] = (now, status, body)


def _error(request_id: str, status: int, code: SendErrorCode, msg: str) -> JSONResponse:
    body = SendErrorResponse(request_id=request_id, code=code, message=msg).model_dump(
        mode="json"
    )
    _idem_set(request_id, status, body)
    return JSONResponse(status_code=status, content=body)


def _run_send(
    request_id: str, target: str, attempt: Callable[[], SendResponse]
) -> JSONResponse:
    """Idempotency + error mapping around one send attempt.

    ``attempt`` performs the send and returns the success response; it may
    raise ``_ChatNotFound`` (404), ``ValueError`` (422) or ``RuntimeError``
    from osascript (502, classified). Every outcome is cached by request id.
    """
    cached = _idem_get(request_id)
    if cached is not None:
        status, body = cached
        log.info("imessage.send.idempotent_hit", request_id=request_id)
        return JSONResponse(status_code=status, content=body)

    try:
        resp = attempt()
    except _ChatNotFound as exc:
        log.warning(
            "imessage.send.chat_not_found", request_id=request_id, chat=str(exc)
        )
        return _error(
            request_id, 404, SendErrorCode.CHAT_NOT_FOUND, f"no chat with id {exc}"
        )
    except ValueError as exc:
        return _error(request_id, 422, SendErrorCode.INTERNAL, str(exc))
    except RuntimeError as exc:
        log.warning(
            "imessage.send.applescript_error", request_id=request_id, error=str(exc)
        )
        return _error(request_id, 502, _classify(str(exc)), str(exc))

    body = resp.model_dump(mode="json")
    _idem_set(request_id, 200, body)
    log.info(
        "imessage.send.ok", request_id=request_id, target=target, service=resp.sent_via
    )
    return JSONResponse(status_code=200, content=body)


@router.post("/imessage/send")
def send(req: SendRequest, _: None = Depends(require_edge_token)) -> JSONResponse:
    # Map "RCS" to "SMS" at send time — RCS is broken on macOS 15+ (we
    # target SMS for the actual delivery but report what the caller asked).
    delivery_service = "SMS" if req.service == "RCS" else req.service

    def attempt() -> SendResponse:
        send_message(req.recipient, req.body, delivery_service)
        detail = f"Sent to {req.recipient} via {delivery_service}."
        if req.service == "RCS":
            detail += " (downgraded from RCS)"
        return SendResponse(
            request_id=req.request_id, sent_via=req.service, detail=detail
        )

    return _run_send(req.request_id, req.recipient, attempt)


@router.post("/imessage/send-to-chat")
def send_to_chat_route(
    req: SendToChatRequest, _: None = Depends(require_edge_token)
) -> JSONResponse:
    def attempt() -> SendResponse:
        chat = resolve_chat(req.chat_id)
        if chat is None:
            raise _ChatNotFound(req.chat_id)
        send_to_chat(chat["guid"], req.body)
        service = cast(IMessageService, chat["service"])
        return SendResponse(
            request_id=req.request_id,
            sent_via=service,
            detail=f"Sent to chat {req.chat_id} via {service}.",
        )

    return _run_send(req.request_id, req.chat_id, attempt)
