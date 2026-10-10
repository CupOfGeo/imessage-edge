# imessage-edge

A small HTTP daemon that runs on a Mac and exposes iMessage send/read
operations to a remote orchestrator over Tailscale. The orchestrator
calls this daemon instead of opening `chat.db` or invoking
`osascript` directly, so the orchestrator can run on Linux.

The orchestrator is not part of this repo. Anything that speaks the
contract in [Orchestrator contract](#orchestrator-contract) below works.

## Install (Homebrew)

```bash
brew tap CupOfGeo/tap
brew install CupOfGeo/tap/imessage-edge
brew services start CupOfGeo/tap/imessage-edge
```

See `PACKAGING.md` for config location, Full Disk Access setup, and
how releases are cut.

## Run (development)

```bash
uv sync
uv run python -m imessage_edge
# or: ./run.sh
```

By default the daemon binds to `127.0.0.1:8765`. To accept connections
from your tailnet, set the bind host to your Tailscale IP:

```bash
IMESSAGE_EDGE_BIND_HOST=$(tailscale ip -4 | head -1) \
IMESSAGE_EDGE_TOKEN=replace-me-with-a-long-random-string \
uv run python -m imessage_edge
```

## Required env

| Var | Purpose |
|---|---|
| `IMESSAGE_EDGE_TOKEN` | Bearer token the orchestrator presents on every call to this daemon |
| `IMESSAGE_INBOUND_TOKEN` | Bearer token this daemon presents to the orchestrator on `POST /events/imessage` |
| `ORCHESTRATOR_URL` | Base URL of the orchestrator API. `http://` sends `IMESSAGE_INBOUND_TOKEN` in cleartext, so only use it over Tailscale |

Optional:

| Var | Default | Purpose |
|---|---|---|
| `IMESSAGE_EDGE_BIND_HOST` | `127.0.0.1` | Address to bind to |
| `IMESSAGE_EDGE_BIND_PORT` | `8765` | Port to bind to |
| `IMESSAGE_EDGE_STATE_DIR` | `~/.imessage-edge` | Persistent state (poll cursor) |
| `IMESSAGE_POLL_INTERVAL_SEC` | `5` | chat.db poll cadence |
| `IMESSAGE_EDGE_CONFIG` | `.env` | Path to an env file to load settings from |

## macOS permissions

For sending: Messages.app must be signed in. The first send will trigger
an Automation permission prompt for the terminal/process running the
daemon. Grant it.

For reading: grant Full Disk Access to the process (System Settings →
Privacy & Security → Full Disk Access). Without it, `chat.db` is
unreadable and `/health` reports `chat_db_readable: false`. Homebrew
installs grant it to the `imessage-edged` launcher, not to Python; see
`PACKAGING.md`.

## Endpoints

All endpoints require `Authorization: Bearer $IMESSAGE_EDGE_TOKEN`.

| Method | Path | Body | Purpose |
|---|---|---|---|
| `GET` | `/health` | — | Liveness + chat.db readability + macOS version |
| `POST` | `/imessage/send` | `SendRequest` | Send an iMessage / SMS / RCS |
| `POST` | `/imessage/send-to-chat` | `SendToChatRequest` | Send into an existing chat by `chat_id` (groups); service comes from the chat |
| `GET` | `/imessage/messages?contact=&limit=` | — | Recent messages in the 1:1 conversation with `contact` |
| `GET` | `/imessage/messages?chat_id=&limit=` | — | Recent messages in any chat by `chat_identifier` (groups included) |
| `GET` | `/imessage/chats?limit=` | — | Recent conversations with `is_group` + `participants` |
| `GET` | `/imessage/search?query=&limit=&contact=\|chat_id=` | — | Case-insensitive substring search over message bodies (incl. `attributedBody`); optional scope to one chat |
| `GET` | `/imessage/last-inbound-service?handle=` | — | Service of last inbound from `handle` |

Wire schemas live in `imessage_edge/contracts.py`. The daemon does not
serve `/docs` or `/openapi.json`; those would bypass the bearer check.

## Orchestrator contract

The daemon is one half of a pair. The other half, the orchestrator, is
any HTTP service that:

- Calls the endpoints above with `Authorization: Bearer $IMESSAGE_EDGE_TOKEN`.
- Accepts `POST $ORCHESTRATOR_URL/events/imessage` with
  `Authorization: Bearer $IMESSAGE_INBOUND_TOKEN` and an `IncomingBatch`
  body (see `contracts.py`), returning `200` once the batch is stored.

The chat.db poller delivers inbound messages at-least-once with
exponential backoff, so the orchestrator should dedupe on
`date_ns`. A `401` or `403` stops the poller advancing its
cursor; any other failure is retried. If `ORCHESTRATOR_URL` or
`IMESSAGE_INBOUND_TOKEN` is unset, the poller stays off and the daemon
is read/send only.

## Why a separate process?

This daemon needs only a handful of small libraries; the orchestrator
pulls in heavy LLM/agent deps that have no business running on a Mac
just to send a message. Splitting the process means the orchestrator
can live on a Linux home server while the Mac runs only this thin shim.
