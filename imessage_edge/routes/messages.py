"""Read-side endpoints — recent messages, chats, search, last-inbound-service."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, status

from imessage_edge.auth import require_edge_token
from imessage_edge.chat_db import (
    Message,
    get_last_inbound_service,
    list_chats,
    recent_messages,
    search_messages,
)
from imessage_edge.contracts import (
    ChatsResponse,
    IMessageService,
    LastInboundServiceResponse,
    MessageRow,
    RecentMessagesResponse,
    SearchResponse,
)
from imessage_edge.contracts import ChatSummary as ChatSummaryModel

router = APIRouter(prefix="/imessage")


def _to_message_row(m: Message) -> MessageRow:
    return MessageRow(
        is_from_me=m["is_from_me"],
        text=m["text"],
        timestamp=m["timestamp"],
        handle=m["handle"],
        service=cast(IMessageService, m["service"]),
    )


@router.get("/messages", response_model=RecentMessagesResponse)
def messages(
    contact: str | None = Query(
        default=None,
        description="Direct (1:1) conversation with this handle. Alias for chat_id.",
    ),
    chat_id: str | None = Query(
        default=None,
        description="Any chat by chat_identifier (handle for direct, `chat…` for group).",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    _: None = Depends(require_edge_token),
) -> RecentMessagesResponse:
    if contact and chat_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="pass either contact or chat_id, not both",
        )
    rows = recent_messages(contact=contact, chat_id=chat_id, limit=limit)
    return RecentMessagesResponse(items=[_to_message_row(r) for r in rows])


@router.get("/chats", response_model=ChatsResponse)
def chats(
    limit: int = Query(default=50, ge=1, le=500),
    _: None = Depends(require_edge_token),
) -> ChatsResponse:
    rows = list_chats(limit=limit)
    return ChatsResponse(
        items=[
            ChatSummaryModel(
                chat_id=r["chat_id"],
                name=r["name"],
                message_count=r["message_count"],
                last_timestamp=r["last_timestamp"],
                is_group=r["is_group"],
                participants=r["participants"],
            )
            for r in rows
        ]
    )


@router.get("/search", response_model=SearchResponse)
def search(
    query: str = Query(min_length=1),
    contact: str | None = Query(
        default=None,
        description="Limit to the direct (1:1) conversation with this handle.",
    ),
    chat_id: str | None = Query(
        default=None,
        description="Limit to any chat by chat_identifier (handle or `chat…`).",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    _: None = Depends(require_edge_token),
) -> SearchResponse:
    if contact and chat_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="pass either contact or chat_id, not both",
        )
    rows = search_messages(query=query, limit=limit, contact=contact, chat_id=chat_id)
    return SearchResponse(items=[_to_message_row(r) for r in rows])


@router.get("/last-inbound-service", response_model=LastInboundServiceResponse)
def last_inbound_service(
    handle: str = Query(min_length=1),
    _: None = Depends(require_edge_token),
) -> LastInboundServiceResponse:
    service = get_last_inbound_service(handle)
    return LastInboundServiceResponse(
        handle=handle,
        service=cast(IMessageService | None, service),
    )
