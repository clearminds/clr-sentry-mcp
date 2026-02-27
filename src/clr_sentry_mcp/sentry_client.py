"""REST client for Sentry API with array params, cursor pagination, and rate limits."""

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class SentryClient:
    """Sentry REST API client.

    Handles:
    - Bearer token auth
    - Array query params (field[] sent as repeated params)
    - Link-header cursor pagination
    - Rate limit awareness
    - Project slug → numeric ID resolution (cached)

    Attributes:
        org_slug: The Sentry organization slug used for path construction.
    """

    def __init__(self, base_url: str, auth_token: str, org_slug: str) -> None:
        """Initialize the Sentry client.

        Args:
            base_url: Sentry instance URL (e.g. ``https://sentry.io``).
                Trailing slashes are stripped and ``/api/0`` is appended if missing.
            auth_token: Bearer token for authentication.
            org_slug: Organization slug for API paths.
        """
        self.org_slug = org_slug
        self._project_id_cache: dict[str, str] = {}
        # Strip trailing slash, ensure /api/0 base
        base = base_url.rstrip("/")
        if not base.endswith("/api/0"):
            base = f"{base}/api/0"
        self._http = httpx.Client(
            base_url=base,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {auth_token}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def resolve_project_id(self, project_slug: str) -> str:
        """Resolve a project slug to its numeric ID (cached).

        The Sentry Events/Discover API requires numeric project IDs, not slugs.
        This method fetches the project once and caches the mapping.

        Args:
            project_slug: The project's slug identifier.

        Returns:
            Numeric project ID as a string.

        Raises:
            ValueError: If the project slug cannot be resolved.
        """
        if project_slug in self._project_id_cache:
            return self._project_id_cache[project_slug]

        resp = self._http.get(
            f"/projects/{self.org_slug}/{project_slug}/",
            params=[],
        )
        resp.raise_for_status()
        data = resp.json()
        project_id = str(data["id"])
        self._project_id_cache[project_slug] = project_id
        logger.debug("Resolved project '%s' → ID %s", project_slug, project_id)
        return project_id

    def _build_params(self, params: dict[str, Any] | None) -> list[tuple[str, str]]:
        """Build query params, expanding lists into repeated keys.

        Args:
            params: Dict where values may be lists (expanded to repeated params).

        Returns:
            List of (key, value) tuples suitable for httpx params.
        """
        if not params:
            return []
        parts: list[tuple[str, str]] = []
        for k, v in params.items():
            if isinstance(v, list):
                for item in v:
                    parts.append((k, str(item)))
            elif v is not None:
                parts.append((k, str(v)))
        return parts

    def _check_rate_limit(self, resp: httpx.Response) -> None:
        """Log a warning if rate limit is nearly exhausted.

        Args:
            resp: HTTP response to check for rate limit headers.
        """
        remaining = resp.headers.get("X-Sentry-Rate-Limit-Remaining")
        if remaining is not None and int(remaining) <= 2:
            logger.warning("Sentry rate limit nearly exhausted: %s remaining", remaining)

    def _parse_next_cursor(self, resp: httpx.Response) -> str | None:
        """Extract next cursor from Link header if more results exist.

        Args:
            resp: HTTP response containing Link header.

        Returns:
            Cursor string if there are more results, None otherwise.
        """
        link = resp.headers.get("Link", "")
        # Pattern: <url>; rel="next"; results="true"; cursor="value"
        match = re.search(
            r'<[^>]+>;\s*rel="next";\s*results="true";\s*cursor="([^"]+)"', link
        )
        return match.group(1) if match else None

    def get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> tuple[Any, str | None]:
        """Send a GET request, return (data, next_cursor).

        Args:
            path: API endpoint path (e.g. ``/organizations/{org}/monitors/``).
            params: Query parameters. List values become repeated params.

        Returns:
            Tuple of (parsed JSON body, next cursor or None).
        """
        resp = self._http.get(path, params=self._build_params(params))
        resp.raise_for_status()
        self._check_rate_limit(resp)
        return resp.json(), self._parse_next_cursor(resp)

    def get_simple(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Send a GET request, return just the data (no pagination).

        Args:
            path: API endpoint path.
            params: Query parameters.

        Returns:
            Parsed JSON body.
        """
        data, _ = self.get(path, params)
        return data

    def post(self, path: str, data: Any = None) -> Any:
        """Send a POST request and return parsed JSON.

        Args:
            path: API endpoint path.
            data: JSON-serializable request body.

        Returns:
            Parsed JSON response body.
        """
        resp = self._http.post(path, json=data)
        resp.raise_for_status()
        self._check_rate_limit(resp)
        return resp.json()

    def put(self, path: str, data: Any = None) -> Any:
        """Send a PUT request and return parsed JSON.

        Args:
            path: API endpoint path.
            data: JSON-serializable request body.

        Returns:
            Parsed JSON response body.
        """
        resp = self._http.put(path, json=data)
        resp.raise_for_status()
        self._check_rate_limit(resp)
        return resp.json()

    def delete(self, path: str) -> dict[str, str]:
        """Send a DELETE request.

        Args:
            path: API endpoint path.

        Returns:
            Dict with status confirmation.
        """
        resp = self._http.delete(path)
        resp.raise_for_status()
        self._check_rate_limit(resp)
        return {"status": "deleted"}

    # -- Convenience paths ---------------------------------------------------

    def org_path(self, suffix: str = "") -> str:
        """Build ``/organizations/{org_slug}/{suffix}`` path.

        Args:
            suffix: Path segment to append after the org slug.

        Returns:
            Full API path string.
        """
        return f"/organizations/{self.org_slug}/{suffix}"

    def project_path(self, project_slug: str, suffix: str = "") -> str:
        """Build ``/projects/{org_slug}/{project_slug}/{suffix}`` path.

        Args:
            project_slug: The project's slug identifier.
            suffix: Path segment to append after the project slug.

        Returns:
            Full API path string.
        """
        return f"/projects/{self.org_slug}/{project_slug}/{suffix}"
