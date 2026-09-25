"""Edge-daemon configuration loaded from env / .env."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# When run as a launchd service (e.g. via Homebrew), the wrapper points
# IMESSAGE_EDGE_CONFIG at /opt/homebrew/etc/imessage-edge/config.env so
# we don't need a bash launcher to source env vars — see the formula's
# service block. For local dev, the default `.env` next to the cwd works.
_ENV_FILE = os.environ.get("IMESSAGE_EDGE_CONFIG", ".env")


class EdgeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Inbound auth — orchestrator presents this on every call to us.
    edge_token: str = Field(
        default="",
        validation_alias="IMESSAGE_EDGE_TOKEN",
        description="Bearer token verified on every inbound request.",
    )

    # Outbound auth — we present this to the orchestrator on
    # POST /events/imessage.
    inbound_token: str = Field(
        default="",
        validation_alias="IMESSAGE_INBOUND_TOKEN",
        description="Bearer token presented to the orchestrator.",
    )

    orchestrator_url: str = Field(
        default="",
        validation_alias="ORCHESTRATOR_URL",
        description="Base URL of the orchestrator.",
    )

    bind_host: str = Field(
        default="127.0.0.1", validation_alias="IMESSAGE_EDGE_BIND_HOST"
    )
    bind_port: int = Field(default=8765, validation_alias="IMESSAGE_EDGE_BIND_PORT")

    poll_interval_sec: int = Field(
        default=5, validation_alias="IMESSAGE_POLL_INTERVAL_SEC"
    )

    state_dir: Path = Field(
        default=Path.home() / ".imessage-edge",
        validation_alias="IMESSAGE_EDGE_STATE_DIR",
    )

    # Bearer tokens never legitimately have surrounding whitespace, and
    # httpx rejects header values with trailing spaces — strip on load
    # so a stray paste in config.env doesn't surface as a confusing
    # "Illegal header value" or silent 401.
    @field_validator("edge_token", "inbound_token", mode="before")
    @classmethod
    def _strip_token(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


settings = EdgeSettings()
