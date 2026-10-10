"""iMessage chat.db reader + AppleScript send.

All direct access to ``~/Library/Messages/chat.db`` and to Messages.app
lives here; the HTTP routes and the poller call in through this module.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from imessage_edge.applescript import escape_applescript_string, run_applescript
from imessage_edge.logging import log

CHAT_DB: Path = Path.home() / "Library" / "Messages" / "chat.db"

# Apple's Core Data epoch: 2001-01-01 00:00:00 UTC
APPLE_EPOCH_OFFSET = 978307200


class Message(TypedDict):
    is_from_me: bool
    text: str
    timestamp: str
    handle: str
    service: str


class ChatSummary(TypedDict):
    chat_id: str
    name: str
    message_count: int
    last_timestamp: str
    is_group: bool
    participants: list[str]


class IncomingMessage(TypedDict):
    handle: str
    chat_id: str
    is_group: bool
    text: str
    service: str
    timestamp_iso: str
    date_ns: int


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def is_available() -> bool:
    """True if this process can talk to iMessage (macOS + chat.db readable).

    File existence alone is misleading: every process can stat chat.db,
    but only those with Full Disk Access can sqlite-open it. Try a real
    read so /health reflects what poll/query code paths actually see.
    """
    if sys.platform != "darwin":
        return False
    try:
        if not CHAT_DB.is_file():
            return False
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return True
    except OSError, sqlite3.Error:
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def apple_ts_to_iso(ns_timestamp: int) -> str:
    if ns_timestamp == 0:
        return ""
    unix_ts = (ns_timestamp / 1_000_000_000) + APPLE_EPOCH_OFFSET
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _query_chat_db(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


# U+FFFC OBJECT REPLACEMENT CHARACTER — Apple uses this inside attributedBody
# to mark attachment / sticker / link-preview placeholders.
_OBJECT_REPLACEMENT_CHAR = "￼"


def _extract_attributed_body_text(blob: bytes) -> str | None:
    """Best-effort typedstream extractor.

    ``attributedBody`` is an NSArchiver typedstream. Rather than parse
    it fully, find the ``NSString`` marker, seek to the ``+`` that
    precedes the string payload, then read a length-prefixed UTF-8
    payload: one length byte, or ``0x81``/``0x82`` followed by a 2- or
    4-byte little-endian length.
    """
    if not blob:
        return None
    idx = blob.find(b"NSString")
    if idx == -1:
        return None
    plus = blob.find(b"+", idx)
    if plus == -1 or plus + 1 >= len(blob):
        return None

    pos = plus + 1
    length_byte = blob[pos]
    if length_byte < 0x80:
        length = length_byte
        pos += 1
    elif length_byte == 0x81:
        if pos + 3 > len(blob):
            return None
        length = int.from_bytes(blob[pos + 1 : pos + 3], "little")
        pos += 3
    elif length_byte == 0x82:
        if pos + 5 > len(blob):
            return None
        length = int.from_bytes(blob[pos + 1 : pos + 5], "little")
        pos += 5
    else:
        return None

    end = pos + length
    if end > len(blob):
        return None
    try:
        return blob[pos:end].decode("utf-8")
    except UnicodeDecodeError:
        return None


def _resolve_message_text(text: str | None, attributed_body: bytes | None) -> str:
    if text:
        return text
    if attributed_body:
        extracted = _extract_attributed_body_text(attributed_body)
        if extracted:
            stripped = extracted.strip(_OBJECT_REPLACEMENT_CHAR + " \t\n")
            if stripped:
                return extracted
    return "[attachment]"


def _normalize_service(raw: str | None) -> str:
    upper = (raw or "").upper()
    if upper == "SMS":
        return "SMS"
    if upper == "RCS":
        return "RCS"
    return "iMessage"


def _row_to_message(row: sqlite3.Row) -> Message:
    raw_service = row["service"] if "service" in row.keys() else None
    return Message(
        is_from_me=bool(row["is_from_me"]),
        text=_resolve_message_text(row["text"], row["attributed_body"]),
        timestamp=apple_ts_to_iso(row["ts"]),
        handle=row["handle"] or "",
        service=_normalize_service(raw_service),
    )


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


_VALID_SERVICES = ("iMessage", "SMS", "RCS")


def send_message(recipient: str, body: str, service: str = "iMessage") -> None:
    """Send via Messages.app over iMessage / SMS / RCS.

    Raises ``RuntimeError`` on osascript / Messages.app failure;
    ``ValueError`` for an unknown service.
    """
    if service not in _VALID_SERVICES:
        raise ValueError(
            f"invalid service {service!r}; expected one of {_VALID_SERVICES}"
        )

    r = escape_applescript_string(recipient)
    b = escape_applescript_string(body)
    script = f'''
    tell application "Messages"
        set targetService to 1st account whose service type = {service}
        set targetBuddy to participant "{r}" of targetService
        send "{b}" to targetBuddy
    end tell
    '''
    log.info("imessage.send", recipient=f"…{recipient[-4:]}", service=service)
    run_applescript(script)


class ResolvedChat(TypedDict):
    guid: str  # Messages.app chat id — verbatim chat.db ``chat.guid``
    service: str


def resolve_chat(chat_id: str) -> ResolvedChat | None:
    """Map a ``chat_identifier`` to the id Messages.app wants for sending.

    AppleScript's ``chat id`` is the ``chat.guid`` column verbatim (macOS
    26 shows ``any;+;chat…`` for groups, ``any;-;<handle>`` for direct).
    The format has changed across macOS versions, so it is always read
    from the database, never constructed. A direct contact can have one
    chat per service sharing the identifier; the most recently active
    one wins so a reply lands where the conversation actually is.
    """
    rows = _query_chat_db(
        """
        SELECT
            c.guid AS guid,
            c.service_name AS service_name,
            MAX(m.date) AS last_msg
        FROM chat c
        LEFT JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
        LEFT JOIN message m ON m.ROWID = cmj.message_id
        WHERE c.chat_identifier = ?
        GROUP BY c.ROWID
        ORDER BY last_msg DESC
        LIMIT 1
        """,
        (chat_id,),
    )
    if not rows:
        return None
    return ResolvedChat(
        guid=rows[0]["guid"], service=_normalize_service(rows[0]["service_name"])
    )


def send_to_chat(chat_guid: str, body: str) -> None:
    """Send into an existing chat (group or direct) by Messages.app chat id.

    Raises ``RuntimeError`` on osascript / Messages.app failure. AppleScript
    cannot create chats, so the guid must come from ``resolve_chat``.
    """
    g = escape_applescript_string(chat_guid)
    b = escape_applescript_string(body)
    script = f'''
    tell application "Messages"
        send "{b}" to chat id "{g}"
    end tell
    '''
    log.info("imessage.send_to_chat", chat_guid=f"…{chat_guid[-4:]}")
    run_applescript(script)


def get_last_inbound_service(handle: str) -> str | None:
    """Service of the most recent inbound message from ``handle`` (or None)."""
    rows = _query_chat_db(
        """
        SELECT m.service AS service
        FROM message m
        JOIN handle h ON m.handle_id = h.ROWID
        WHERE h.id = ? AND m.is_from_me = 0
        ORDER BY m.date DESC
        LIMIT 1
        """,
        (handle,),
    )
    if not rows:
        return None
    raw = (rows[0]["service"] or "").upper()
    if raw == "SMS":
        return "SMS"
    if raw == "RCS":
        return "RCS"
    if raw == "IMESSAGE":
        return "iMessage"
    return None


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def recent_messages(
    contact: str | None, limit: int, chat_id: str | None = None
) -> list[Message]:
    """Return recent messages (oldest → newest), optionally scoped to one chat.

    ``chat_id`` is a ``chat.chat_identifier``: a handle for a direct chat
    (``+15551234567``), or ``chat…`` for a group. ``contact`` is a
    backward-compatible alias — for a direct chat the identifier *is* the
    handle, so both hit the same row. Scoping by chat (rather than by
    chat membership) is what keeps a contact's group traffic out of
    their 1:1 history. Passing both is a caller error (rejected at the
    route); if it reaches here, ``chat_id`` wins.
    """
    scope = chat_id or contact
    if scope:
        rows = _query_chat_db(
            """
            SELECT
                m.is_from_me,
                m.text,
                m.attributedBody AS attributed_body,
                m.date AS ts,
                m.service AS service,
                h.id AS handle
            FROM message m
            JOIN chat_message_join cmj
                ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE c.chat_identifier = ?
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (scope, limit),
        )
    else:
        rows = _query_chat_db(
            """
            SELECT
                m.is_from_me,
                m.text,
                m.attributedBody AS attributed_body,
                m.date AS ts,
                m.service AS service,
                h.id AS handle
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (limit,),
        )

    return [_row_to_message(r) for r in reversed(rows)]


def list_chats(limit: int) -> list[ChatSummary]:
    """Recent conversations ordered by most recent activity.

    ``chat_id`` is the ``chat_identifier`` (the same value events and the
    ``recent_messages`` filter use). A contact with both an iMessage and
    an SMS direct chat shows up as two rows sharing that identifier.
    ``participants`` are the other parties' handles; ``is_group`` follows
    Apple's ``chat…`` identifier prefix, matching the poller.
    """
    rows = _query_chat_db(
        """
        SELECT
            c.chat_identifier AS chat_id,
            COALESCE(NULLIF(c.display_name, ''), c.chat_identifier) AS name,
            COUNT(DISTINCT m.ROWID) AS msg_count,
            MAX(m.date) AS last_msg,
            (
                SELECT group_concat(h.id, ',')
                FROM chat_handle_join chj
                JOIN handle h ON h.ROWID = chj.handle_id
                WHERE chj.chat_id = c.ROWID
            ) AS participants
        FROM chat c
        JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
        JOIN message m ON cmj.message_id = m.ROWID
        GROUP BY c.ROWID
        ORDER BY last_msg DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [
        ChatSummary(
            chat_id=row["chat_id"] or "",
            name=row["name"] or "",
            message_count=row["msg_count"],
            last_timestamp=apple_ts_to_iso(row["last_msg"]),
            is_group=(row["chat_id"] or "").startswith("chat"),
            participants=[p for p in (row["participants"] or "").split(",") if p],
        )
        for row in rows
    ]


# Upper bound on rows pulled into Python per search. Purely a memory guard:
# a full scan of a ~50k-row chat.db takes well under 100 ms, so this is far
# above anything a personal database reaches.
_SEARCH_SCAN_CAP = 250_000


def search_messages(
    query: str,
    limit: int,
    contact: str | None = None,
    chat_id: str | None = None,
) -> list[Message]:
    """Case-insensitive substring search over message bodies (oldest → newest).

    Almost every row in a modern chat.db has an empty ``text`` column with
    the body living in ``attributedBody``, so the match runs in Python
    over the resolved text rather than as ``LIKE`` in SQL. Rows that
    resolve to ``[attachment]`` never match. Newest ``limit`` hits are
    returned in ascending date order. ``contact``/``chat_id`` scope the
    search to one conversation exactly as in :func:`recent_messages`;
    with neither the whole database is scanned (capped at
    ``_SEARCH_SCAN_CAP`` rows, newest first).
    """
    scope = chat_id or contact
    if scope:
        rows = _query_chat_db(
            """
            SELECT
                m.is_from_me,
                m.text,
                m.attributedBody AS attributed_body,
                m.date AS ts,
                m.service AS service,
                h.id AS handle
            FROM message m
            JOIN chat_message_join cmj
                ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE c.chat_identifier = ?
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (scope, _SEARCH_SCAN_CAP),
        )
    else:
        rows = _query_chat_db(
            """
            SELECT
                m.is_from_me,
                m.text,
                m.attributedBody AS attributed_body,
                m.date AS ts,
                m.service AS service,
                h.id AS handle
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (_SEARCH_SCAN_CAP,),
        )

    needle = query.casefold()
    hits: list[Message] = []
    for row in rows:
        # Resolve text first; only build the full record (timestamp
        # conversion etc.) for actual hits.
        text = _resolve_message_text(row["text"], row["attributed_body"])
        if text == "[attachment]":
            continue
        if needle in text.casefold():
            hits.append(_row_to_message(row))
            if len(hits) >= limit:
                break
    hits.reverse()
    return hits


# ---------------------------------------------------------------------------
# Monitor poll (feeds IncomingMessageEvent; includes the `service` column)
# ---------------------------------------------------------------------------


def poll_incoming_since(since_ns: int) -> list[IncomingMessage]:
    """Return incoming (``is_from_me=0``) messages with ``m.date > since_ns``."""
    rows = _query_chat_db(
        """
        SELECT
            m.text,
            m.attributedBody AS attributed_body,
            m.date AS ts,
            m.service AS service,
            h.id AS handle,
            c.chat_identifier AS chat_id
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        JOIN chat c ON cmj.chat_id = c.ROWID
        WHERE m.date > ? AND m.is_from_me = 0
        ORDER BY m.date ASC
        """,
        (since_ns,),
    )

    results: list[IncomingMessage] = []
    for row in rows:
        text = _resolve_message_text(row["text"], row["attributed_body"])
        if text == "[attachment]":
            continue
        chat_id = row["chat_id"] or ""
        results.append(
            IncomingMessage(
                handle=row["handle"] or "",
                chat_id=chat_id,
                is_group=chat_id.startswith("chat"),
                text=text,
                service=_normalize_service(row["service"]),
                timestamp_iso=apple_ts_to_iso(row["ts"]),
                date_ns=row["ts"],
            )
        )
    return results
