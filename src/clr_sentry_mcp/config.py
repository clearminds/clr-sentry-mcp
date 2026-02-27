"""Configuration for Sentry MCP Server."""

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

CREDS_PATH = Path.home() / ".config" / "sentry" / "credentials.json"


class Settings(BaseSettings):
    """Settings loaded from credentials file or environment variables.

    Priority order:
    1. ~/.config/sentry/credentials.json
    2. Environment variables (SENTRY_URL, SENTRY_AUTH_TOKEN, SENTRY_ORG_SLUG)

    Attributes:
        sentry_url: The Sentry instance URL.
        sentry_auth_token: Bearer token for Sentry API authentication.
        sentry_org_slug: Organization slug for API paths.
        sentry_read_only: When true, disable all write tools.
        sentry_transport: MCP transport mode (``stdio`` or ``http``).
        sentry_log_level: Logging verbosity level.
    """

    sentry_url: str = ""
    sentry_auth_token: str = ""
    sentry_org_slug: str = ""
    sentry_read_only: bool = False
    sentry_transport: str = "stdio"
    sentry_log_level: str = "INFO"

    @field_validator("sentry_read_only", mode="before")
    @classmethod
    def _empty_str_to_false(cls, v: Any) -> Any:
        if v == "":
            return False
        return v

    model_config = {"env_prefix": ""}

    def load_credentials(self) -> dict[str, Any]:
        """Load credentials with config-file-first, env-override pattern.

        Returns:
            Dict with url, auth_token, and org_slug.
        """
        creds: dict[str, Any] = {}

        # 1. FIRST: Load from environment variables (base/fallback)
        if self.sentry_url:
            creds["url"] = self.sentry_url
        if self.sentry_auth_token:
            creds["auth_token"] = self.sentry_auth_token
        if self.sentry_org_slug:
            creds["org_slug"] = self.sentry_org_slug

        # 2. THEN: Override with credentials.json file (takes priority)
        if CREDS_PATH.exists():
            try:
                file_creds: dict[str, Any] = json.loads(CREDS_PATH.read_text())

                if "url" in file_creds:
                    creds["url"] = file_creds["url"]
                if "auth_token" in file_creds:
                    creds["auth_token"] = file_creds["auth_token"]
                if "org_slug" in file_creds:
                    creds["org_slug"] = file_creds["org_slug"]

                logger.info("Loaded Sentry credentials from %s", CREDS_PATH)
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Failed to load %s: %s", CREDS_PATH, e)

        if not (creds.get("url") and creds.get("auth_token")):
            logger.warning(
                "No Sentry credentials configured. Set SENTRY_URL/SENTRY_AUTH_TOKEN "
                "env vars or create %s",
                CREDS_PATH,
            )

        return creds
