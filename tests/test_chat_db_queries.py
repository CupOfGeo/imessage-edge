"""Query-level tests against a synthetic chat.db.

Builds the five tables the daemon reads (``chat``, ``handle``, ``message``,
``chat_message_join``, ``chat_handle_join``) with just the columns the
queries touch, then points ``chat_db.CHAT_DB`` at it. This is the one
place the SQL itself is exercised; everything else mocks around it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from imessage_edge import chat_db
from imessage_edge.chat_db import list_chats, recent_messages, search_messages

ANTHONY = "+16198463882"  # direct: iMessage + SMS chats, also in the group
RICHARD = "+12818130025"  # direct: iMessage chat, also in the group
GROUP = "chat100"

_SCHEMA = """
CREATE TABLE chat (
    ROWID INTEGER PRIMARY KEY, guid TEXT, chat_identifier TEXT,
    service_name TEXT, display_name TEXT
);
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB, date INTEGER,
    is_from_me INTEGER, handle_id INTEGER, service TEXT
);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
"""

AddMessage = Callable[..., None]


def _body(text: str) -> bytes:
    """Minimal typedstream blob carrying ``text`` (UTF-8 under 128 bytes),
    the shape the daemon's extractor expects for a modern chat.db row."""
    payload = text.encode()
    assert len(payload) < 0x80
    return (
        b"\x04\x0bstreamtyped"
        + b"NSString\x01\x95\x84\x01+"
        + bytes([len(payload)])
        + payload
    )


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "chat.db"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO handle (ROWID, id, service) VALUES (?, ?, ?)",
        [(1, ANTHONY, "SMS"), (2, RICHARD, "iMessage")],
    )
    conn.executemany(
        "INSERT INTO chat (ROWID, guid, chat_identifier, service_name, display_name)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (1, f"any;-;{ANTHONY}", ANTHONY, "iMessage", ""),
            (2, f"any;-;{ANTHONY}", ANTHONY, "SMS", ""),
            (3, f"any;+;{GROUP}", GROUP, "iMessage", "Crew"),
            (4, f"any;-;{RICHARD}", RICHARD, "iMessage", ""),
        ],
    )
    conn.executemany(
        "INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?, ?)",
        [(1, 1), (2, 1), (3, 1), (3, 2), (4, 2)],
    )
    conn.commit()
    monkeypatch.setattr(chat_db, "CHAT_DB", path)
    yield conn
    conn.close()


@pytest.fixture
def add_message(db: sqlite3.Connection) -> AddMessage:
    counter = {"rowid": 0}

    def _add(
        chat_rowid: int,
        text: str | None,
        date: int,
        is_from_me: bool = False,
        handle_rowid: int | None = None,
        service: str = "iMessage",
        attributed_body: bytes | None = None,
    ) -> None:
        counter["rowid"] += 1
        db.execute(
            "INSERT INTO message"
            " (ROWID, text, attributedBody, date, is_from_me, handle_id, service)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                counter["rowid"],
                text,
                attributed_body,
                date,
                int(is_from_me),
                handle_rowid,
                service,
            ),
        )
        db.execute(
            "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
            (chat_rowid, counter["rowid"]),
        )
        db.commit()

    return _add


@pytest.fixture
def populated(add_message: AddMessage) -> None:
    """A realistic spread: Anthony 1:1 on both services, a group with
    Anthony + Richard, and a Richard 1:1."""
    add_message(1, "a-imsg-in", 10, False, 1, "iMessage")
    add_message(1, "a-imsg-me", 11, True, None, "iMessage")
    add_message(2, "a-sms-in", 12, False, 1, "SMS")
    add_message(3, "g-anthony", 20, False, 1, "iMessage")
    add_message(3, "g-richard", 21, False, 2, "iMessage")
    add_message(3, "g-me", 22, True, None, "iMessage")
    add_message(4, "r-imsg-in", 30, False, 2, "iMessage")


def _texts(rows: list[chat_db.Message]) -> list[str]:
    return [r["text"] for r in rows]


# ---------------------------------------------------------------------------
# recent_messages
# ---------------------------------------------------------------------------


def test_recent_messages_contact_is_strict_one_to_one(populated: None) -> None:
    texts = _texts(recent_messages(contact=ANTHONY, limit=50))
    assert "g-anthony" not in texts
    assert "g-richard" not in texts
    assert "g-me" not in texts
    assert set(texts) == {"a-imsg-in", "a-imsg-me", "a-sms-in"}


def test_recent_messages_contact_includes_both_directions(populated: None) -> None:
    rows = recent_messages(contact=ANTHONY, limit=50)
    by_text = {r["text"]: r for r in rows}
    assert by_text["a-imsg-in"]["is_from_me"] is False
    assert by_text["a-imsg-in"]["handle"] == ANTHONY
    assert by_text["a-imsg-me"]["is_from_me"] is True
    assert by_text["a-imsg-me"]["handle"] == ""


def test_recent_messages_contact_spans_imessage_and_sms_chats(
    populated: None,
) -> None:
    rows = recent_messages(contact=ANTHONY, limit=50)
    services = {r["text"]: r["service"] for r in rows}
    assert services["a-imsg-in"] == "iMessage"
    assert services["a-sms-in"] == "SMS"


def test_recent_messages_chat_id_reads_group(populated: None) -> None:
    rows = recent_messages(contact=None, limit=50, chat_id=GROUP)
    assert _texts(rows) == ["g-anthony", "g-richard", "g-me"]  # oldest → newest
    assert [r["handle"] for r in rows] == [ANTHONY, RICHARD, ""]
    assert [r["is_from_me"] for r in rows] == [False, False, True]


def test_recent_messages_chat_id_and_contact_equivalent_for_direct_chat(
    populated: None,
) -> None:
    assert recent_messages(contact=ANTHONY, limit=50) == recent_messages(
        contact=None, limit=50, chat_id=ANTHONY
    )


def test_recent_messages_chat_id_wins_when_both_given(populated: None) -> None:
    rows = recent_messages(contact=RICHARD, limit=50, chat_id=GROUP)
    assert _texts(rows) == ["g-anthony", "g-richard", "g-me"]


def test_recent_messages_unknown_chat_returns_empty(populated: None) -> None:
    assert recent_messages(contact="+10000000000", limit=50) == []
    assert recent_messages(contact=None, limit=50, chat_id="chat999") == []


def test_recent_messages_no_filter_returns_global_recent(populated: None) -> None:
    rows = recent_messages(contact=None, limit=3)
    assert _texts(rows) == ["g-richard", "g-me", "r-imsg-in"]  # oldest → newest


def test_recent_messages_limit_applies_to_newest(populated: None) -> None:
    rows = recent_messages(contact=ANTHONY, limit=2)
    assert _texts(rows) == ["a-imsg-me", "a-sms-in"]


# ---------------------------------------------------------------------------
# list_chats
# ---------------------------------------------------------------------------


def test_list_chats_marks_groups_and_lists_participants(populated: None) -> None:
    chats = {c["chat_id"]: c for c in list_chats(limit=50) if c["is_group"]}
    assert set(chats) == {GROUP}
    group = chats[GROUP]
    assert group["name"] == "Crew"
    assert group["message_count"] == 3
    assert sorted(group["participants"]) == sorted([ANTHONY, RICHARD])


def test_list_chats_direct_chat_has_one_participant_and_string_id(
    populated: None,
) -> None:
    richard = [c for c in list_chats(limit=50) if c["chat_id"] == RICHARD]
    assert len(richard) == 1
    assert richard[0]["is_group"] is False
    assert richard[0]["participants"] == [RICHARD]
    assert richard[0]["name"] == RICHARD  # no display_name → identifier


def test_list_chats_direct_contact_on_two_services_is_two_rows(
    populated: None,
) -> None:
    anthony = [c for c in list_chats(limit=50) if c["chat_id"] == ANTHONY]
    assert len(anthony) == 2
    assert {c["message_count"] for c in anthony} == {2, 1}


def test_list_chats_ordered_by_most_recent_activity(populated: None) -> None:
    ids = [c["chat_id"] for c in list_chats(limit=50)]
    assert ids[0] == RICHARD  # date 30
    assert ids[1] == GROUP  # date 22


def test_list_chats_excludes_chats_with_no_messages(db: sqlite3.Connection) -> None:
    assert list_chats(limit=50) == []


# ---------------------------------------------------------------------------
# resolve_chat
# ---------------------------------------------------------------------------


def test_resolve_chat_returns_guid_and_service_for_group(populated: None) -> None:
    assert chat_db.resolve_chat(GROUP) == {
        "guid": f"any;+;{GROUP}",
        "service": "iMessage",
    }


def test_resolve_chat_prefers_most_recently_active_direct_chat(
    populated: None, add_message: AddMessage
) -> None:
    # Anthony has an iMessage chat (rowid 1, last date 11) and an SMS chat
    # (rowid 2, last date 12): SMS is more recent.
    resolved = chat_db.resolve_chat(ANTHONY)
    assert resolved is not None
    assert resolved["service"] == "SMS"
    # A newer iMessage message flips the preference.
    add_message(1, "a-imsg-late", 99, False, 1, "iMessage")
    resolved = chat_db.resolve_chat(ANTHONY)
    assert resolved is not None
    assert resolved["service"] == "iMessage"


def test_resolve_chat_unknown_returns_none(populated: None) -> None:
    assert chat_db.resolve_chat("chat999") is None
    assert chat_db.resolve_chat("+10000000000") is None


def test_resolve_chat_works_for_chat_with_no_messages(db: sqlite3.Connection) -> None:
    resolved = chat_db.resolve_chat(RICHARD)
    assert resolved == {"guid": f"any;-;{RICHARD}", "service": "iMessage"}


# ---------------------------------------------------------------------------
# search_messages
# ---------------------------------------------------------------------------


@pytest.fixture
def searchable(add_message: AddMessage) -> None:
    """Modern rows (text NULL, body in attributedBody) plus one legacy text
    row, spread across Anthony's 1:1 and the group."""
    add_message(1, None, 10, False, 1, attributed_body=_body("yogurt run tonight?"))
    add_message(1, "legacy text row about yogurt", 11, True, None)
    add_message(3, None, 20, False, 2, attributed_body=_body("Café Tuesday"))
    add_message(3, None, 21, True, None, attributed_body=b"")  # -> [attachment]
    add_message(4, None, 30, False, 2, attributed_body=_body("nothing here"))


def test_search_finds_attributed_body_only_message(searchable: None) -> None:
    hits = search_messages("tonight", limit=10)
    assert _texts(hits) == ["yogurt run tonight?"]


def test_search_still_finds_plain_text_message(searchable: None) -> None:
    hits = search_messages("legacy", limit=10)
    assert _texts(hits) == ["legacy text row about yogurt"]


def test_search_is_case_insensitive_unicode(searchable: None) -> None:
    assert _texts(search_messages("CAFÉ", limit=10)) == ["Café Tuesday"]
    assert _texts(search_messages("YOGURT", limit=10)) == [
        "yogurt run tonight?",
        "legacy text row about yogurt",
    ]


def test_search_skips_attachment_only_rows(searchable: None) -> None:
    assert search_messages("attachment", limit=10) == []


def test_search_returns_newest_hits_oldest_first(add_message: AddMessage) -> None:
    for i in range(5):
        add_message(1, None, 100 + i, False, 1, attributed_body=_body(f"ping {i}"))
    assert _texts(search_messages("ping", limit=3)) == ["ping 2", "ping 3", "ping 4"]


def test_search_contact_scopes_to_direct_chat(add_message: AddMessage) -> None:
    add_message(1, None, 10, False, 1, attributed_body=_body("plan A"))
    add_message(3, None, 20, False, 1, attributed_body=_body("plan B"))
    assert _texts(search_messages("plan", limit=10, contact=ANTHONY)) == ["plan A"]


def test_search_chat_id_scopes_to_group(add_message: AddMessage) -> None:
    add_message(1, None, 10, False, 1, attributed_body=_body("plan A"))
    add_message(3, None, 20, False, 1, attributed_body=_body("plan B"))
    assert _texts(search_messages("plan", limit=10, chat_id=GROUP)) == ["plan B"]


def test_search_no_scope_searches_everything(add_message: AddMessage) -> None:
    add_message(1, None, 10, False, 1, attributed_body=_body("plan A"))
    add_message(3, None, 20, False, 1, attributed_body=_body("plan B"))
    assert _texts(search_messages("plan", limit=10)) == ["plan A", "plan B"]


def test_search_no_match_returns_empty(searchable: None) -> None:
    assert search_messages("zebra", limit=10) == []
