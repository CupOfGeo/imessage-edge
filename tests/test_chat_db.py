"""Pure-function tests for chat_db: typedstream extraction, text
resolution, service normalisation, timestamps, and the send wrapper."""

from __future__ import annotations

import pytest

from imessage_edge import chat_db
from imessage_edge.chat_db import (
    _OBJECT_REPLACEMENT_CHAR,
    _extract_attributed_body_text,
    _normalize_service,
    _resolve_message_text,
    apple_ts_to_iso,
    send_message,
)

# ---------------------------------------------------------------------------
# _extract_attributed_body_text
# ---------------------------------------------------------------------------


def _blob(payload: bytes, length_prefix: bytes) -> bytes:
    """Build a minimal typedstream-ish blob: junk, NSString marker, '+',
    length prefix, payload."""
    return (
        b"\x04\x0bstreamtyped" + b"NSString\x01\x95\x84\x01+" + length_prefix + payload
    )


def test_extract_short_length_prefix() -> None:
    assert _extract_attributed_body_text(_blob(b"hello", b"\x05")) == "hello"


def test_extract_0x81_two_byte_length() -> None:
    payload = b"x" * 300
    prefix = b"\x81" + (300).to_bytes(2, "little")
    assert _extract_attributed_body_text(_blob(payload, prefix)) == "x" * 300


def test_extract_0x82_four_byte_length() -> None:
    payload = b"y" * 70_000
    prefix = b"\x82" + (70_000).to_bytes(4, "little")
    assert _extract_attributed_body_text(_blob(payload, prefix)) == "y" * 70_000


def test_extract_returns_none_without_nsstring_marker() -> None:
    assert _extract_attributed_body_text(b"no marker here +\x05hello") is None


def test_extract_returns_none_for_empty_blob() -> None:
    assert _extract_attributed_body_text(b"") is None


def test_extract_returns_none_when_length_exceeds_blob() -> None:
    # Claims 50 bytes, only 5 present.
    assert _extract_attributed_body_text(_blob(b"hello", b"\x32")) is None


def test_extract_returns_none_on_unknown_length_marker() -> None:
    assert _extract_attributed_body_text(_blob(b"hello", b"\x83")) is None


def test_extract_returns_none_on_invalid_utf8() -> None:
    assert _extract_attributed_body_text(_blob(b"\xff\xfe\xfd", b"\x03")) is None


# ---------------------------------------------------------------------------
# _resolve_message_text
# ---------------------------------------------------------------------------


def test_resolve_prefers_text_over_attributed_body() -> None:
    body = _blob(b"from body", b"\x09")
    assert _resolve_message_text("from text", body) == "from text"


def test_resolve_falls_back_to_attributed_body() -> None:
    body = _blob(b"from body", b"\x09")
    assert _resolve_message_text(None, body) == "from body"
    assert _resolve_message_text("", body) == "from body"


def test_resolve_attachment_when_body_is_only_object_replacement_char() -> None:
    payload = _OBJECT_REPLACEMENT_CHAR.encode("utf-8")
    body = _blob(payload, bytes([len(payload)]))
    assert _resolve_message_text(None, body) == "[attachment]"


def test_resolve_attachment_when_both_empty() -> None:
    assert _resolve_message_text(None, None) == "[attachment]"
    assert _resolve_message_text("", b"") == "[attachment]"


# ---------------------------------------------------------------------------
# _normalize_service / apple_ts_to_iso
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sms", "SMS"),
        ("SMS", "SMS"),
        ("rcs", "RCS"),
        ("RCS", "RCS"),
        ("iMessage", "iMessage"),
        ("IMESSAGE", "iMessage"),
        (None, "iMessage"),
        ("garbage", "iMessage"),
    ],
)
def test_normalize_service(raw: str | None, expected: str) -> None:
    assert _normalize_service(raw) == expected


def test_apple_ts_to_iso_zero_is_empty() -> None:
    assert apple_ts_to_iso(0) == ""


def test_apple_ts_to_iso_known_value() -> None:
    # One second after the Apple epoch (2001-01-01T00:00:00Z), in ns.
    assert apple_ts_to_iso(1_000_000_000) == "2001-01-01 00:00:01"


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


def test_send_message_rejects_unknown_service(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(chat_db, "run_applescript", lambda s, **kw: calls.append(s))
    with pytest.raises(ValueError, match="invalid service"):
        send_message("+15550001111", "hi", "Telegram")
    assert calls == []


def test_send_message_escapes_quotes_in_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []
    monkeypatch.setattr(
        chat_db, "run_applescript", lambda s, **kw: captured.append(s) or ""
    )
    send_message('who"ever', 'say "hi" \\ bye', "SMS")
    assert len(captured) == 1
    script = captured[0]
    assert 'participant "who\\"ever"' in script
    assert 'send "say \\"hi\\" \\\\ bye"' in script
    assert "service type = SMS" in script


# ---------------------------------------------------------------------------
# send_to_chat
# ---------------------------------------------------------------------------


def test_send_to_chat_uses_chat_id_form_and_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []
    monkeypatch.setattr(
        chat_db, "run_applescript", lambda s, **kw: captured.append(s) or ""
    )
    chat_db.send_to_chat('any;+;chat"1', 'say "hi"')
    assert len(captured) == 1
    script = captured[0]
    assert 'send "say \\"hi\\"" to chat id "any;+;chat\\"1"' in script
    assert "participant" not in script
