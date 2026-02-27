"""Sentry MCP Server — FastMCP tools for Sentry issue tracking and monitoring."""

import argparse
import logging
import logging.config
import re
import sys
import urllib.parse
from typing import Any

from fastmcp import FastMCP

from clr_sentry_mcp.config import Settings
from clr_sentry_mcp.middleware import ToolValidationMiddleware
from clr_sentry_mcp.sentry_client import SentryClient

mcp = FastMCP("Sentry Extra")
mcp.add_middleware(ToolValidationMiddleware())
_client: SentryClient | None = None

WRITE_TOOLS: list[str] = []


# ── Issue tools ─────────────────────────────────────────────────────


@mcp.tool
def sentry_get_issue(
    identifier: str,
    include_latest_event: bool = False,
    grep_pattern: str | None = None,
) -> dict[str, Any]:
    """Get a Sentry issue by numeric ID, short ID, or full URL.

    Automatically detects the identifier type and resolves it:
    - Numeric ID (e.g. "9232") -- fetches directly
    - Short ID (e.g. "NODUS-PROD-1W") -- resolves via shortids API first
    - Full URL (e.g. "https://sentry.../issues/9232/...") -- extracts numeric ID from path
    - Search URL (e.g. "https://sentry.../issues/?query=NODUS-PROD-1W") -- extracts short ID from query

    Args:
        identifier: Numeric issue ID, short ID, or full Sentry URL.
        include_latest_event: Include the latest event details in the response.
        grep_pattern: Regex pattern to filter response keys (not implemented yet, reserved).

    Returns:
        Issue details dict.
    """
    numeric_id: str | None = None

    if identifier.isdigit():
        # Plain numeric ID
        numeric_id = identifier
    elif identifier.startswith("http"):
        # Try to extract numeric ID from URL path: /issues/9232/
        path_match = re.search(r"/issues/(\d+)", identifier)
        if path_match:
            numeric_id = path_match.group(1)
        else:
            # Search URL like /issues/?query=NODUS-PROD-1W
            parsed = urllib.parse.urlparse(identifier)
            qs = urllib.parse.parse_qs(parsed.query)
            short_id = qs.get("query", [None])[0]
            if short_id:
                resolved = _client.get_simple(_client.org_path(f"shortids/{short_id}/"))
                numeric_id = str(resolved["group"]["id"])
            else:
                raise ValueError(f"Could not extract issue ID or short ID from URL: {identifier}")
    else:
        # Assume short ID like NODUS-PROD-1W
        resolved = _client.get_simple(_client.org_path(f"shortids/{identifier}/"))
        numeric_id = str(resolved["group"]["id"])

    params: dict[str, Any] = {}
    if include_latest_event:
        params["collapse"] = "release"

    return _client.get_simple(f"/issues/{numeric_id}/", params=params or None)


# ── Main entry point ─────────────────────────────────────────────────


def main() -> None:
    """Main entry point for the Sentry MCP server."""
    global _client

    settings = Settings()

    parser = argparse.ArgumentParser(description="Sentry MCP Server")
    parser.add_argument("--transport", type=str, choices=["stdio", "http"], default=None)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        default=None,
        help="Run in read-only mode (hide write tools)",
    )
    args = parser.parse_args()

    transport = args.transport or settings.sentry_transport
    log_level = args.log_level or settings.sentry_log_level

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "console": {
                    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "console",
                    "stream": "ext://sys.stderr",
                }
            },
            "root": {"level": log_level, "handlers": ["console"]},
        }
    )

    logger = logging.getLogger(__name__)

    creds = settings.load_credentials()
    url = creds.get("url", "")
    auth_token = creds.get("auth_token", "")
    org_slug = creds.get("org_slug", "")

    if not url:
        logger.error("SENTRY_URL is required")
        sys.exit(1)

    if not auth_token:
        logger.error("SENTRY_AUTH_TOKEN is required")
        sys.exit(1)

    if not org_slug:
        logger.warning("SENTRY_ORG_SLUG not set — org-scoped tools will fail")

    logger.info("Starting Sentry MCP Server")
    _client = SentryClient(url, auth_token, org_slug)

    read_only = args.read_only if args.read_only is not None else settings.sentry_read_only
    if read_only and WRITE_TOOLS:
        for name in WRITE_TOOLS:
            mcp.remove_tool(name)
        logger.info("Read-only mode: %d write tools removed", len(WRITE_TOOLS))

    try:
        if transport == "stdio":
            mcp.run(transport="stdio")
        else:
            mcp.run(transport="http", host=args.host, port=args.port)
    except Exception as e:
        logger.error("Failed to start MCP server: %s", e)
        sys.exit(1)
    finally:
        _client.close()


if __name__ == "__main__":
    main()
