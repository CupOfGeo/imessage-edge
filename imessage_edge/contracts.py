"""Wire-protocol Pydantic models.

This file is the canonical schema for both directions: the HTTP API the
daemon serves and the ``IncomingBatch`` it POSTs to the orchestrator.
Orchestrator implementations may vendor this file; treat any change
here as a wire change and version accordingly.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

# The three transports Messages.app can send over. `service` on a send
# request selects one; on rows and events it reports which one was used.
IMessageService = Literal["iMessage", "SMS", "RCS"]

# Upper bound on a send body. Far above a real text; stops an oversized
# payload from reaching osascript's argv.
MAX_BODY_CHARS = 10_000


class HealthResponse(BaseModel):
    status: Literal["alive"] = "alive"
    macos_version: str
    chat_db_readable: bool
    daemon_version: str


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


class SendRequest(BaseModel):
    recipient: str
    body: str = Field(max_length=MAX_BODY_CHARS)
    service: IMessageService = "iMessage"
    request_id: str = Field(
        description=(
            "Caller-supplied UUID. The daemon caches the response keyed on "
            "this id for ~120s; duplicate POSTs return the cached response "
            "without re-sending."
        ),
    )


class SendResponse(BaseModel):
    request_id: str
    sent_via: IMessageService
    detail: str


class SendToChatRequest(BaseModel):
    """Send into an existing chat — the only way to post to a group.

    ``service`` is not a field: a chat is bound to one service, so the
    daemon reports what it used in ``SendResponse.sent_via``.
    """

    chat_id: str = Field(
        description=(
            "chat.chat_identifier — same value as IncomingMessageEvent.chat_id "
            "and ChatSummary.chat_id. `chat…` for groups; a handle also works "
            "and targets that contact's most recently active direct chat."
        )
    )
    body: str = Field(max_length=MAX_BODY_CHARS)
    request_id: str = Field(
        description="Caller-supplied UUID; idempotent for ~120s like /imessage/send."
    )


class SendErrorCode(str, Enum):
    APPLESCRIPT_ERROR = "applescript_error"
    UNKNOWN_PARTICIPANT = "unknown_participant"
    PERMISSION_DENIED = "permission_denied"
    CHAT_NOT_FOUND = "chat_not_found"
    # request_id reused with a different request, or still in flight.
    REQUEST_ID_CONFLICT = "request_id_conflict"
    INTERNAL = "internal"


class SendErrorResponse(BaseModel):
    request_id: str
    code: SendErrorCode
    message: str


# ---------------------------------------------------------------------------
# Inbound events (edge → orchestrator)
# ---------------------------------------------------------------------------


class IncomingMessageEvent(BaseModel):
    handle: str
    chat_id: str
    is_group: bool
    text: str
    service: IMessageService
    timestamp_iso: str
    date_ns: int


class IncomingBatch(BaseModel):
    events: list[IncomingMessageEvent]
    edge_id: str = "imessage"


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


class MessageRow(BaseModel):
    is_from_me: bool
    text: str
    timestamp: str
    handle: str
    service: IMessageService


class ChatSummary(BaseModel):
    chat_id: str = Field(
        description=(
            "chat.chat_identifier — a handle for a direct chat, `chat…` for a "
            "group. Same value as IncomingMessageEvent.chat_id and the "
            "`chat_id` filter on GET /imessage/messages."
        )
    )
    name: str
    message_count: int
    last_timestamp: str
    is_group: bool
    participants: list[str] = Field(
        description="Other parties' handles (phone numbers / emails)."
    )


class RecentMessagesResponse(BaseModel):
    items: list[MessageRow]


class ChatsResponse(BaseModel):
    items: list[ChatSummary]


class SearchResponse(BaseModel):
    items: list[MessageRow]


class LastInboundServiceResponse(BaseModel):
    handle: str
    service: IMessageService | None
