"""HTTP-level tests through FastAPI's TestClient. chat.db access and
AppleScript are patched out; nothing here touches macOS."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from imessage_edge import __version__
from imessage_edge.chat_db import ChatSummary, Message
from imessage_edge.contracts import MAX_BODY_CHARS
from imessage_edge.routes import health as health_route
from imessage_edge.routes import messages as messages_route
from imessage_edge.routes import send as send_route
from imessage_edge.settings import settings

# ---------------------------------------------------------------------------
# Auth (via /health)
# ---------------------------------------------------------------------------


def test_health_missing_token_401(client: TestClient) -> None:
    assert client.get("/health").status_code == 401


def test_health_wrong_token_401(client: TestClient) -> None:
    r = client.get("/health", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_health_non_bearer_scheme_401(client: TestClient) -> None:
    r = client.get("/health", headers={"Authorization": "Basic dGVzdA=="})
    assert r.status_code == 401


def test_health_non_ascii_token_401(client: TestClient) -> None:
    # compare_digest raises TypeError on non-ASCII str; must not surface as 500.
    r = client.get("/health", headers={b"Authorization": b"Bearer \xe9"})
    assert r.status_code == 401


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_schema_pages_not_served(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_health_503_when_token_not_configured(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "edge_token", "")
    r = client.get("/health", headers=auth)
    assert r.status_code == 503


def test_health_ok_reports_version_and_readability(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_route, "is_available", lambda: True)
    r = client.get("/health", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "alive"
    assert body["chat_db_readable"] is True
    assert body["daemon_version"] == __version__


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


def _msg(text: str = "hi") -> Message:
    return Message(
        is_from_me=False,
        text=text,
        timestamp="2026-01-01 00:00:00",
        handle="+15550001111",
        service="iMessage",
    )


@pytest.mark.parametrize("limit", [0, 501])
def test_messages_limit_out_of_range_422(
    client: TestClient, auth: dict[str, str], limit: int
) -> None:
    r = client.get(f"/imessage/messages?limit={limit}", headers=auth)
    assert r.status_code == 422


def test_messages_maps_rows_to_response(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_recent(
        contact: str | None, limit: int, chat_id: str | None = None
    ) -> list[Message]:
        seen.update(contact=contact, limit=limit)
        return [_msg("one"), _msg("two")]

    monkeypatch.setattr(messages_route, "recent_messages", fake_recent)
    r = client.get("/imessage/messages?contact=%2B15550001111&limit=2", headers=auth)
    assert r.status_code == 200
    assert seen == {"contact": "+15550001111", "limit": 2}
    assert [m["text"] for m in r.json()["items"]] == ["one", "two"]
    assert r.json()["items"][0]["service"] == "iMessage"


def test_chats_maps_rows_including_group_fields(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        messages_route,
        "list_chats",
        lambda limit: [
            ChatSummary(
                chat_id="chat100",
                name="Family",
                message_count=3,
                last_timestamp="2026",
                is_group=True,
                participants=["+15550001111", "+15550002222"],
            )
        ],
    )
    r = client.get("/imessage/chats", headers=auth)
    assert r.status_code == 200
    assert r.json()["items"] == [
        {
            "chat_id": "chat100",
            "name": "Family",
            "message_count": 3,
            "last_timestamp": "2026",
            "is_group": True,
            "participants": ["+15550001111", "+15550002222"],
        }
    ]


def test_messages_rejects_both_contact_and_chat_id(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        messages_route,
        "recent_messages",
        lambda **kw: pytest.fail("query must not run"),
    )
    r = client.get("/imessage/messages?contact=%2B1555&chat_id=chat1", headers=auth)
    assert r.status_code == 422
    assert "not both" in r.json()["detail"]


def test_messages_passes_chat_id_through(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_recent(
        contact: str | None, limit: int, chat_id: str | None = None
    ) -> list[Message]:
        seen.update(contact=contact, chat_id=chat_id, limit=limit)
        return []

    monkeypatch.setattr(messages_route, "recent_messages", fake_recent)
    r = client.get("/imessage/messages?chat_id=chat100&limit=5", headers=auth)
    assert r.status_code == 200
    assert seen == {"contact": None, "chat_id": "chat100", "limit": 5}


def test_search_requires_query(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get("/imessage/search", headers=auth).status_code == 422
    assert client.get("/imessage/search?query=", headers=auth).status_code == 422


def test_search_maps_rows(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        messages_route, "search_messages", lambda query, **_: [_msg(query)]
    )
    r = client.get("/imessage/search?query=yogurt", headers=auth)
    assert r.status_code == 200
    assert r.json()["items"][0]["text"] == "yogurt"


def test_search_passes_scope_to_query(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake(**kw: object) -> list[Message]:
        seen.update(kw)
        return []

    monkeypatch.setattr(messages_route, "search_messages", fake)
    r = client.get("/imessage/search?query=x&chat_id=chat100&limit=5", headers=auth)
    assert r.status_code == 200
    assert seen == {"query": "x", "contact": None, "chat_id": "chat100", "limit": 5}


def test_search_rejects_both_contact_and_chat_id(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.get("/imessage/search?query=x&contact=%2B1&chat_id=chat1", headers=auth)
    assert r.status_code == 422
    assert "not both" in r.json()["detail"]


def test_last_inbound_service_none_when_unknown(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(messages_route, "get_last_inbound_service", lambda h: None)
    r = client.get("/imessage/last-inbound-service?handle=%2B1555", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"handle": "+1555", "service": None}


# ---------------------------------------------------------------------------
# POST /imessage/send
# ---------------------------------------------------------------------------


def _send_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "recipient": "+15550001111",
        "body": "hello",
        "service": "iMessage",
        "request_id": "req-1",
    }
    body.update(overrides)
    return body


def test_send_ok_returns_200_and_caches(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        send_route, "send_message", lambda r, b, s: calls.append((r, b, s))
    )
    r = client.post("/imessage/send", json=_send_body(), headers=auth)
    assert r.status_code == 200
    assert r.json()["sent_via"] == "iMessage"
    assert r.json()["request_id"] == "req-1"
    assert calls == [("+15550001111", "hello", "iMessage")]
    assert "req-1" in send_route._idem


def test_send_idempotent_replay_does_not_resend(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(send_route, "send_message", lambda r, b, s: calls.append(1))
    first = client.post("/imessage/send", json=_send_body(), headers=auth)
    second = client.post("/imessage/send", json=_send_body(), headers=auth)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(calls) == 1


def test_send_rcs_downgrades_to_sms_but_reports_rcs(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(send_route, "send_message", lambda r, b, s: calls.append(s))
    r = client.post("/imessage/send", json=_send_body(service="RCS"), headers=auth)
    assert r.status_code == 200
    assert calls == ["SMS"]
    assert r.json()["sent_via"] == "RCS"
    assert "downgraded from RCS" in r.json()["detail"]


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("AppleScript error: -1743 not allowed", "permission_denied"),
        ('AppleScript error: Can\'t get participant "x"', "unknown_participant"),
        ("AppleScript error: something else", "applescript_error"),
    ],
)
def test_send_applescript_error_502_with_classified_code(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    code: str,
) -> None:
    def boom(r: str, b: str, s: str) -> None:
        raise RuntimeError(stderr)

    monkeypatch.setattr(send_route, "send_message", boom)
    r = client.post("/imessage/send", json=_send_body(), headers=auth)
    assert r.status_code == 502
    assert r.json()["code"] == code
    assert r.json()["request_id"] == "req-1"


def test_send_value_error_422_internal(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def bad(r: str, b: str, s: str) -> None:
        raise ValueError("invalid service")

    monkeypatch.setattr(send_route, "send_message", bad)
    r = client.post("/imessage/send", json=_send_body(), headers=auth)
    assert r.status_code == 422
    assert r.json()["code"] == "internal"


def test_send_error_response_is_also_cached(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def boom(r: str, b: str, s: str) -> None:
        calls.append(1)
        raise RuntimeError("AppleScript error: nope")

    monkeypatch.setattr(send_route, "send_message", boom)
    first = client.post("/imessage/send", json=_send_body(), headers=auth)
    second = client.post("/imessage/send", json=_send_body(), headers=auth)
    assert first.status_code == second.status_code == 502
    assert first.json() == second.json()
    assert len(calls) == 1


def test_send_requires_auth(client: TestClient) -> None:
    assert client.post("/imessage/send", json=_send_body()).status_code == 401


def test_send_rejects_unknown_service_at_validation(
    client: TestClient, auth: dict[str, str]
) -> None:
    r = client.post("/imessage/send", json=_send_body(service="Telegram"), headers=auth)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /imessage/send-to-chat
# ---------------------------------------------------------------------------


def _chat_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "chat_id": "chat100",
        "body": "hello group",
        "request_id": "chat-req-1",
    }
    body.update(overrides)
    return body


@pytest.fixture
def resolved_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        send_route,
        "resolve_chat",
        lambda chat_id: {"guid": "any;+;chat100", "service": "iMessage"},
    )


def test_send_to_chat_ok_reports_chat_service(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    resolved_group: None,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(send_route, "send_to_chat", lambda g, b: calls.append((g, b)))
    r = client.post("/imessage/send-to-chat", json=_chat_body(), headers=auth)
    assert r.status_code == 200
    assert calls == [("any;+;chat100", "hello group")]
    assert r.json()["sent_via"] == "iMessage"
    assert r.json()["request_id"] == "chat-req-1"
    assert "chat100" in r.json()["detail"]


def test_send_to_chat_unknown_chat_404_and_cached(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    lookups: list[str] = []
    monkeypatch.setattr(
        send_route, "resolve_chat", lambda chat_id: lookups.append(chat_id)
    )
    monkeypatch.setattr(
        send_route, "send_to_chat", lambda g, b: pytest.fail("must not send")
    )
    first = client.post("/imessage/send-to-chat", json=_chat_body(), headers=auth)
    second = client.post("/imessage/send-to-chat", json=_chat_body(), headers=auth)
    assert first.status_code == second.status_code == 404
    assert first.json()["code"] == "chat_not_found"
    assert "chat100" in first.json()["message"]
    assert lookups == ["chat100"]  # replay served from cache, no second lookup


def test_send_to_chat_idempotent_replay(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    resolved_group: None,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(send_route, "send_to_chat", lambda g, b: calls.append(1))
    first = client.post("/imessage/send-to-chat", json=_chat_body(), headers=auth)
    second = client.post("/imessage/send-to-chat", json=_chat_body(), headers=auth)
    assert first.json() == second.json()
    assert len(calls) == 1


def test_send_to_chat_applescript_error_502_classified(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    resolved_group: None,
) -> None:
    def boom(g: str, b: str) -> None:
        raise RuntimeError("AppleScript error: -1743 not allowed")

    monkeypatch.setattr(send_route, "send_to_chat", boom)
    r = client.post("/imessage/send-to-chat", json=_chat_body(), headers=auth)
    assert r.status_code == 502
    assert r.json()["code"] == "permission_denied"


def test_send_to_chat_requires_auth(client: TestClient) -> None:
    assert client.post("/imessage/send-to-chat", json=_chat_body()).status_code == 401


def test_send_to_chat_rejects_missing_chat_id(
    client: TestClient, auth: dict[str, str]
) -> None:
    body = _chat_body()
    del body["chat_id"]
    assert (
        client.post("/imessage/send-to-chat", json=body, headers=auth).status_code
        == 422
    )


def test_send_and_send_to_chat_share_idempotency_namespace(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    resolved_group: None,
) -> None:
    monkeypatch.setattr(send_route, "send_message", lambda r, b, s: None)
    monkeypatch.setattr(
        send_route, "send_to_chat", lambda g, b: pytest.fail("must not send")
    )
    first = client.post(
        "/imessage/send", json=_send_body(request_id="shared"), headers=auth
    )
    reuse = client.post(
        "/imessage/send-to-chat", json=_chat_body(request_id="shared"), headers=auth
    )
    assert first.status_code == 200
    assert reuse.status_code == 409
    assert reuse.json()["code"] == "request_id_conflict"


def test_send_reused_request_id_with_different_body_409(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(send_route, "send_message", lambda r, b, s: calls.append(b))
    first = client.post("/imessage/send", json=_send_body(), headers=auth)
    second = client.post("/imessage/send", json=_send_body(body="other"), headers=auth)
    assert first.status_code == 200
    assert second.status_code == 409
    assert "different request" in second.json()["message"]
    assert calls == ["hello"]
    # The original outcome is still replayable.
    third = client.post("/imessage/send", json=_send_body(), headers=auth)
    assert third.json() == first.json()


def test_send_concurrent_duplicate_409_without_second_send(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def send_while_duplicate_arrives(r: str, b: str, s: str) -> None:
        calls.append(1)
        dup = client.post("/imessage/send", json=_send_body(), headers=auth)
        assert dup.status_code == 409
        assert "in flight" in dup.json()["message"]

    monkeypatch.setattr(send_route, "send_message", send_while_duplicate_arrives)
    r = client.post("/imessage/send", json=_send_body(), headers=auth)
    assert r.status_code == 200
    assert calls == [1]


def test_send_body_over_limit_422(client: TestClient, auth: dict[str, str]) -> None:
    body = "x" * (MAX_BODY_CHARS + 1)
    r = client.post("/imessage/send", json=_send_body(body=body), headers=auth)
    assert r.status_code == 422
    r = client.post("/imessage/send-to-chat", json=_chat_body(body=body), headers=auth)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Idempotency cache pruning
# ---------------------------------------------------------------------------


def test_idempotency_cache_prunes_expired_entries_on_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 1_000.0}
    monkeypatch.setattr(send_route.time, "time", lambda: clock["now"])

    assert send_route._idem_claim("old", {"a": 1}) is None
    clock["now"] += send_route._IDEM_TTL_SEC - 1
    assert send_route._idem_claim("fresh", {"b": 2}) is None
    assert set(send_route._idem) == {"old", "fresh"}  # old not yet expired

    clock["now"] += 1  # old is now exactly TTL old
    assert send_route._idem_claim("newest", {"c": 3}) is None
    assert set(send_route._idem) == {"fresh", "newest"}
    assert send_route._idem_claim("old", {"z": 9}) is None  # free to reuse


def test_idempotency_cache_stays_bounded_over_many_sends(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 1_000.0}
    monkeypatch.setattr(send_route.time, "time", lambda: clock["now"])
    monkeypatch.setattr(send_route, "send_message", lambda r, b, s: None)
    for i in range(50):
        clock["now"] += 10  # 50 sends over 500s, TTL is 120s
        r = client.post(
            "/imessage/send", json=_send_body(request_id=f"req-{i}"), headers=auth
        )
        assert r.status_code == 200
    # Only entries younger than the TTL survive: 120s / 10s = 12 (+ the newest).
    assert len(send_route._idem) <= 13
