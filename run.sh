#!/usr/bin/env bash
# Spin up the iMessage edge daemon on this Mac.
#
# Prerequisite (one-time): install uv —
#   curl -LsSf https://astral.sh/uv/install.sh | sh
#
# Usage:
#   ./run.sh
#
# Configuration is read from `.env` in this directory (see settings.py
# for the keys), or from the shell env if you'd rather export them
# directly. Required at minimum:
#   IMESSAGE_EDGE_TOKEN     — bearer the orchestrator presents to us
#   IMESSAGE_INBOUND_TOKEN  — bearer we present to the orchestrator
#   ORCHESTRATOR_URL        — base URL of the orchestrator API

set -euo pipefail

cd "$(dirname "$0")"


# 1. Mac-only — osascript and chat.db are macOS-native; the daemon
#    cannot function anywhere else.
if [[ "$(uname)" != "Darwin" ]]; then
  echo "imessage-edge requires macOS (detected $(uname))." >&2
  echo "It depends on osascript and ~/Library/Messages/chat.db, both Mac-only." >&2
  exit 1
fi

# 2. uv handles Python + deps; if it's missing, point the user at the installer.
if ! command -v uv >/dev/null 2>&1; then
  cat >&2 <<'EOF'
error: uv not found on PATH.

Install it once with:
  curl -LsSf https://astral.sh/uv/install.sh | sh

Then re-run ./run.sh. uv handles the right Python version + the daemon's
dependencies; you don't need to manage a virtualenv yourself.
EOF
  exit 1
fi

# 3. Friendly nudge if no config will be available — the daemon would
#    still start but every request would 503 on missing-token.
if [[ ! -f .env && -z "${IMESSAGE_EDGE_TOKEN:-}" ]]; then
  cat >&2 <<'EOF'
warning: no .env file found in this directory and IMESSAGE_EDGE_TOKEN is not
set in your shell. The daemon will start but reject every request with 503
until tokens are configured. See README.md for the required env vars.

EOF
fi

# 4. Resolve + install deps. First run creates .venv; subsequent runs are
#    a near-instant no-op.
uv sync

# 5. Run the daemon in the foreground. Ctrl-C stops it cleanly via the
#    FastAPI lifespan (which also stops the chat.db monitor thread).
echo "Starting imessage-edge…"
exec uv run python -m imessage_edge
