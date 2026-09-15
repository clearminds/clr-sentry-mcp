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

# Imported here (not at the top) on purpose: annotations.py needs ``mcp`` from
# this module, so importing it before the ``mcp = FastMCP(...)`` line above
# would be a circular import. Do not move.
from clr_sentry_mcp.annotations import (  # noqa: E402
    destructive_tool,
    read_tool,
    remove_non_read_tools,
    write_tool,
)

_client: SentryClient | None = None


# ── Issue tools ─────────────────────────────────────────────────────


@read_tool
def get_issue(
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

    data = _client.get_simple(f"/issues/{numeric_id}/", params=params or None)

    # Strip heavy fields that bloat the response (activity alone can be 60K+ chars)
    drop_keys = {"activity", "seenBy", "participants", "pluginActions", "pluginIssues", "pluginContexts"}
    for key in drop_keys:
        data.pop(key, None)

    # Trim release objects to essentials
    for rel_key in ("firstRelease", "lastRelease"):
        rel = data.get(rel_key)
        if isinstance(rel, dict):
            data[rel_key] = {
                "version": rel.get("shortVersion") or rel.get("version"),
                "dateCreated": rel.get("dateCreated"),
            }

    return data


_MAX_STRING_LEN = 300


def _truncate_rows(data: dict[str, Any]) -> dict[str, Any]:
    """Truncate long string values in Discover/Events data rows."""
    rows = data.get("data", data) if isinstance(data, dict) else data
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                for k, v in row.items():
                    if isinstance(v, str) and len(v) > _MAX_STRING_LEN:
                        row[k] = v[:_MAX_STRING_LEN] + f"... ({len(v)} chars total)"
    return data


# ── Insights / Discover tools ────────────────────────────────────────


@read_tool
def top_transactions(
    project_slug: str | None = None,
    stats_period: str = "7d",
    limit: int = 10,
    sort_by: str = "-p95(transaction.duration)",
) -> dict[str, Any]:
    """Get slowest endpoints/transactions ranked by response time.

    Args:
        project_slug: Filter to a specific project. Omit for all projects.
        stats_period: Time range -- 1h, 24h, 7d, 14d, 30d (default: 7d).
        limit: Max results (default: 10, max: 100).
        sort_by: Sort field (default: -p95(transaction.duration)).

    Returns:
        Table of transactions with count, p75, p95, and avg duration.
    """
    params: dict[str, Any] = {
        "field": [
            "transaction",
            "count()",
            "p75(transaction.duration)",
            "p95(transaction.duration)",
            "avg(transaction.duration)",
        ],
        "sort": sort_by,
        "per_page": min(limit, 100),
        "query": "event.type:transaction",
        "statsPeriod": stats_period,
    }
    if project_slug:
        params["project"] = _client.resolve_project_id(project_slug)
    data, cursor = _client.get(_client.org_path("events/"), params)
    result: dict[str, Any] = {"data": _truncate_rows(data).get("data", data), "meta": data.get("meta")}
    if cursor:
        result["next_cursor"] = cursor
    return result


@read_tool
def slow_db_queries(
    project_slug: str | None = None,
    stats_period: str = "7d",
    limit: int = 10,
    sort_by: str = "-p95(span.duration)",
) -> dict[str, Any]:
    """Get slowest database queries.

    Args:
        project_slug: Filter to a specific project. Omit for all projects.
        stats_period: Time range -- 1h, 24h, 7d, 14d, 30d (default: 7d).
        limit: Max results (default: 10, max: 100).
        sort_by: Sort field (default: -p95(span.duration)).

    Returns:
        Table of DB queries with count, avg, p95, and total duration.
    """
    params: dict[str, Any] = {
        "field": [
            "span.description",
            "count()",
            "avg(span.duration)",
            "p95(span.duration)",
            "sum(span.duration)",
        ],
        "dataset": "spans",
        "sort": sort_by,
        "per_page": min(limit, 100),
        "query": "span.op:db",
        "statsPeriod": stats_period,
    }
    if project_slug:
        params["project"] = _client.resolve_project_id(project_slug)
    data, cursor = _client.get(_client.org_path("events/"), params)
    result: dict[str, Any] = {"data": _truncate_rows(data).get("data", data), "meta": data.get("meta")}
    if cursor:
        result["next_cursor"] = cursor
    return result


@read_tool
def slow_http_requests(
    project_slug: str | None = None,
    stats_period: str = "7d",
    limit: int = 10,
    sort_by: str = "-p95(span.duration)",
) -> dict[str, Any]:
    """Get slowest outbound HTTP requests.

    Args:
        project_slug: Filter to a specific project. Omit for all projects.
        stats_period: Time range -- 1h, 24h, 7d, 14d, 30d (default: 7d).
        limit: Max results (default: 10, max: 100).
        sort_by: Sort field (default: -p95(span.duration)).

    Returns:
        Table of HTTP requests with count, avg, p95, and total duration.
    """
    params: dict[str, Any] = {
        "field": [
            "span.description",
            "count()",
            "avg(span.duration)",
            "p95(span.duration)",
            "sum(span.duration)",
        ],
        "dataset": "spans",
        "sort": sort_by,
        "per_page": min(limit, 100),
        "query": "span.op:http.client",
        "statsPeriod": stats_period,
    }
    if project_slug:
        params["project"] = _client.resolve_project_id(project_slug)
    data, cursor = _client.get(_client.org_path("events/"), params)
    result: dict[str, Any] = {"data": _truncate_rows(data).get("data", data), "meta": data.get("meta")}
    if cursor:
        result["next_cursor"] = cursor
    return result


@read_tool
def queue_performance(
    project_slug: str | None = None,
    stats_period: str = "7d",
    limit: int = 10,
    sort_by: str = "-p95(span.duration)",
) -> dict[str, Any]:
    """Get queue/task worker performance.

    Args:
        project_slug: Filter to a specific project. Omit for all projects.
        stats_period: Time range -- 1h, 24h, 7d, 14d, 30d (default: 7d).
        limit: Max results (default: 10, max: 100).
        sort_by: Sort field (default: -p95(span.duration)).

    Returns:
        Table of queue operations with count, avg, and p95 duration.
    """
    params: dict[str, Any] = {
        "field": [
            "span.description",
            "span.op",
            "count()",
            "avg(span.duration)",
            "p95(span.duration)",
        ],
        "dataset": "spans",
        "sort": sort_by,
        "per_page": min(limit, 100),
        "query": "span.op:queue.process OR span.op:queue.publish",
        "statsPeriod": stats_period,
    }
    if project_slug:
        params["project"] = _client.resolve_project_id(project_slug)
    data, cursor = _client.get(_client.org_path("events/"), params)
    result: dict[str, Any] = {"data": _truncate_rows(data).get("data", data), "meta": data.get("meta")}
    if cursor:
        result["next_cursor"] = cursor
    return result


@read_tool
def discover_query(
    fields: list[str],
    query: str = "",
    dataset: str | None = None,
    sort: str | None = None,
    stats_period: str = "7d",
    project_slug: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Run a custom Sentry Discover query with proper array field support.

    Use this for custom performance queries not covered by the preset tools.

    Args:
        fields: List of fields/aggregates to query
            (e.g. ["transaction", "count()", "p95(transaction.duration)"]).
        query: Sentry search query filter
            (e.g. "event.type:transaction browser:Chrome").
        dataset: Dataset to query -- omit for default, or use "spans", "transactions".
        sort: Sort field (prefix with - for descending, e.g. "-count()").
        stats_period: Time range -- 1h, 24h, 7d, 14d, 30d (default: 7d).
        project_slug: Filter to a specific project.
        limit: Max results (default: 20, max: 100).
        cursor: Pagination cursor from a previous response.

    Returns:
        Query results with data rows and metadata.
    """
    params: dict[str, Any] = {
        "field": fields,
        "statsPeriod": stats_period,
        "per_page": min(limit, 100),
    }
    if query:
        params["query"] = query
    if dataset:
        params["dataset"] = dataset
    if sort:
        params["sort"] = sort
    if project_slug:
        params["project"] = _client.resolve_project_id(project_slug)
    if cursor:
        params["cursor"] = cursor
    data, next_cursor = _client.get(_client.org_path("events/"), params)
    result: dict[str, Any] = {"data": _truncate_rows(data).get("data", data), "meta": data.get("meta")}
    if next_cursor:
        result["next_cursor"] = next_cursor
    return result


@read_tool
def events_timeseries(
    fields: list[str],
    y_axis: str = "count()",
    interval: int | None = None,
    stats_period: str = "7d",
    query: str = "",
    project_slug: str | None = None,
    group_by: list[str] | None = None,
) -> dict[str, Any]:
    """Query Sentry events in timeseries format for trend visualization.

    Args:
        fields: List of fields to query.
        y_axis: Aggregate field for the timeseries (default: count()).
        interval: Bucket size in seconds (must be smaller than the time window).
        stats_period: Time range -- 1h, 24h, 7d, 14d, 30d (default: 7d).
        query: Sentry search query filter.
        project_slug: Filter to a specific project.
        group_by: Fields to group the timeseries by.

    Returns:
        Timeseries data with timestamps and values.
    """
    params: dict[str, Any] = {
        "field": fields,
        "yAxis": y_axis,
        "statsPeriod": stats_period,
    }
    if interval:
        params["interval"] = interval
    if query:
        params["query"] = query
    if project_slug:
        params["project"] = _client.resolve_project_id(project_slug)
    if group_by:
        params["groupBy"] = group_by
    data, _ = _client.get(_client.org_path("events-timeseries/"), params)
    return data


# ── Monitor tools ────────────────────────────────────────────────────


@read_tool
def list_monitors(
    project_slug: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List all cron monitors in the organization.

    Args:
        project_slug: Filter to a specific project. Omit for all projects.
        cursor: Pagination cursor from a previous response.

    Returns:
        List of monitors with pagination cursor.
    """
    params: dict[str, Any] = {}
    if project_slug:
        params["project"] = _client.resolve_project_id(project_slug)
    if cursor:
        params["cursor"] = cursor
    data, next_cursor = _client.get(_client.org_path("monitors/"), params or None)
    result: dict[str, Any] = {"monitors": data}
    if next_cursor:
        result["next_cursor"] = next_cursor
    return result


@read_tool
def get_monitor(
    monitor_slug: str,
    project_slug: str | None = None,
) -> dict[str, Any]:
    """Get details of a specific cron monitor.

    Args:
        monitor_slug: The monitor's slug identifier.
        project_slug: Project slug (optional, for disambiguation).

    Returns:
        Monitor details dict.
    """
    params: dict[str, Any] = {}
    if project_slug:
        params["project"] = _client.resolve_project_id(project_slug)
    return _client.get_simple(
        _client.org_path(f"monitors/{monitor_slug}/"), params=params or None
    )


@write_tool
def create_monitor(
    project_slug: str,
    name: str,
    schedule: str,
    schedule_type: str = "crontab",
    checkin_margin: int | None = None,
    max_runtime: int | None = None,
    timezone: str = "UTC",
) -> dict[str, Any]:
    """Create a new cron monitor.

    Args:
        project_slug: Project to create the monitor in.
        name: Display name for the monitor.
        schedule: Cron schedule expression (e.g. "0 * * * *") or interval value.
        schedule_type: Schedule type -- "crontab" or "interval" (default: crontab).
        checkin_margin: Grace period in minutes before a missed check-in is flagged.
        max_runtime: Max expected runtime in minutes before a timeout is flagged.
        timezone: Timezone for the schedule (default: UTC).

    Returns:
        Created monitor details.
    """
    config: dict[str, Any] = {
        "schedule": schedule,
        "schedule_type": schedule_type,
        "timezone": timezone,
    }
    if checkin_margin is not None:
        config["checkin_margin"] = checkin_margin
    if max_runtime is not None:
        config["max_runtime"] = max_runtime
    body: dict[str, Any] = {
        "project": project_slug,
        "name": name,
        "type": "cron_job",
        "config": config,
    }
    return _client.post(_client.org_path("monitors/"), body)


@write_tool
def update_monitor(
    monitor_slug: str,
    name: str | None = None,
    schedule: str | None = None,
    schedule_type: str | None = None,
    checkin_margin: int | None = None,
    max_runtime: int | None = None,
    is_muted: bool | None = None,
) -> dict[str, Any]:
    """Update an existing cron monitor.

    Args:
        monitor_slug: The monitor's slug identifier.
        name: New display name.
        schedule: New cron schedule expression or interval value.
        schedule_type: New schedule type -- "crontab" or "interval".
        checkin_margin: New grace period in minutes.
        max_runtime: New max runtime in minutes.
        is_muted: Mute or unmute the monitor.

    Returns:
        Updated monitor details.
    """
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if is_muted is not None:
        body["isMuted"] = is_muted
    config: dict[str, Any] = {}
    if schedule is not None:
        config["schedule"] = schedule
    if schedule_type is not None:
        config["schedule_type"] = schedule_type
    if checkin_margin is not None:
        config["checkin_margin"] = checkin_margin
    if max_runtime is not None:
        config["max_runtime"] = max_runtime
    if config:
        body["config"] = config
    return _client.put(_client.org_path(f"monitors/{monitor_slug}/"), body)


@destructive_tool
def delete_monitor(monitor_slug: str) -> dict[str, str]:
    """Delete a cron monitor.

    Args:
        monitor_slug: The monitor's slug identifier.

    Returns:
        Deletion confirmation.
    """
    return _client.delete(_client.org_path(f"monitors/{monitor_slug}/"))


# ── Issue alert tools ────────────────────────────────────────────────


@read_tool
def list_issue_alerts(project_slug: str) -> Any:
    """List all issue alert rules for a project.

    Args:
        project_slug: The project's slug identifier.

    Returns:
        List of issue alert rules.
    """
    return _client.get_simple(_client.project_path(project_slug, "rules/"))


@read_tool
def get_issue_alert(project_slug: str, rule_id: str) -> Any:
    """Get details of a specific issue alert rule.

    Args:
        project_slug: The project's slug identifier.
        rule_id: The alert rule ID.

    Returns:
        Issue alert rule details.
    """
    return _client.get_simple(
        _client.project_path(project_slug, f"rules/{rule_id}/")
    )


@write_tool
def create_issue_alert(
    project_slug: str,
    name: str,
    frequency: int,
    action_match: str,
    conditions: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    filter_match: str = "all",
    filters: list[dict[str, Any]] | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """Create a new issue alert rule for a project.

    Args:
        project_slug: The project's slug identifier.
        name: Display name for the alert rule.
        frequency: How often the rule fires in minutes (e.g. 30 = at most once per 30 min).
        action_match: When to trigger -- "all", "any", or "none" (for conditions).
        conditions: List of condition dicts (e.g. first seen, regression, etc.).
        actions: List of action dicts (e.g. send email, Slack notification).
        filter_match: When to apply filters -- "all", "any", or "none" (default: all).
        filters: Optional list of filter dicts (e.g. issue age, event attribute).
        environment: Optional environment name to scope the rule to.

    Returns:
        Created issue alert rule details.
    """
    body: dict[str, Any] = {
        "name": name,
        "frequency": frequency,
        "actionMatch": action_match,
        "conditions": conditions,
        "actions": actions,
        "filterMatch": filter_match,
    }
    if filters is not None:
        body["filters"] = filters
    if environment is not None:
        body["environment"] = environment
    return _client.post(_client.project_path(project_slug, "rules/"), body)


@write_tool
def update_issue_alert(
    project_slug: str,
    rule_id: str,
    name: str | None = None,
    frequency: int | None = None,
    action_match: str | None = None,
    conditions: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
    filter_match: str | None = None,
    filters: list[dict[str, Any]] | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """Update an existing issue alert rule.

    Args:
        project_slug: The project's slug identifier.
        rule_id: The alert rule ID.
        name: New display name.
        frequency: New frequency in minutes.
        action_match: New condition match -- "all", "any", or "none".
        conditions: New list of condition dicts.
        actions: New list of action dicts.
        filter_match: New filter match -- "all", "any", or "none".
        filters: New list of filter dicts.
        environment: New environment name.

    Returns:
        Updated issue alert rule details.
    """
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if frequency is not None:
        body["frequency"] = frequency
    if action_match is not None:
        body["actionMatch"] = action_match
    if conditions is not None:
        body["conditions"] = conditions
    if actions is not None:
        body["actions"] = actions
    if filter_match is not None:
        body["filterMatch"] = filter_match
    if filters is not None:
        body["filters"] = filters
    if environment is not None:
        body["environment"] = environment
    return _client.put(
        _client.project_path(project_slug, f"rules/{rule_id}/"), body
    )


@destructive_tool
def delete_issue_alert(project_slug: str, rule_id: str) -> dict[str, str]:
    """Delete an issue alert rule.

    Args:
        project_slug: The project's slug identifier.
        rule_id: The alert rule ID.

    Returns:
        Deletion confirmation.
    """
    return _client.delete(
        _client.project_path(project_slug, f"rules/{rule_id}/")
    )


# ── Metric alert tools ──────────────────────────────────────────────


@read_tool
def list_metric_alerts() -> Any:
    """List all metric alert rules in the organization.

    Returns:
        List of metric alert rules.
    """
    return _client.get_simple(_client.org_path("alert-rules/"))


@read_tool
def get_metric_alert(rule_id: str) -> Any:
    """Get details of a specific metric alert rule.

    Args:
        rule_id: The metric alert rule ID.

    Returns:
        Metric alert rule details.
    """
    return _client.get_simple(_client.org_path(f"alert-rules/{rule_id}/"))


@write_tool
def create_metric_alert(
    name: str,
    aggregate: str,
    query: str,
    time_window: int,
    triggers: list[dict[str, Any]],
    projects: list[str],
    dataset: str = "events",
    threshold_type: int = 0,
    resolve_threshold: float | None = None,
    environment: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """Create a new metric alert rule.

    Args:
        name: Display name for the alert rule.
        aggregate: Aggregation function (e.g. "count()", "avg(transaction.duration)").
        query: Sentry search query filter for the metric.
        time_window: Time window in minutes to evaluate the metric over.
        triggers: List of trigger dicts with alertThreshold and actions.
        projects: List of project slugs to apply the rule to.
        dataset: Dataset -- "events", "transactions", or "sessions" (default: events).
        threshold_type: 0 = above, 1 = below (default: 0).
        resolve_threshold: Value at which the alert auto-resolves.
        environment: Environment name to scope the rule to.
        owner: Owner identifier (e.g. "team:my-team" or "user:123").

    Returns:
        Created metric alert rule details.
    """
    body: dict[str, Any] = {
        "name": name,
        "aggregate": aggregate,
        "query": query,
        "timeWindow": time_window,
        "triggers": triggers,
        "projects": projects,
        "dataset": dataset,
        "thresholdType": threshold_type,
    }
    if resolve_threshold is not None:
        body["resolveThreshold"] = resolve_threshold
    if environment is not None:
        body["environment"] = environment
    if owner is not None:
        body["owner"] = owner
    return _client.post(_client.org_path("alert-rules/"), body)


@write_tool
def update_metric_alert(
    rule_id: str,
    name: str | None = None,
    aggregate: str | None = None,
    query: str | None = None,
    time_window: int | None = None,
    triggers: list[dict[str, Any]] | None = None,
    threshold_type: int | None = None,
    resolve_threshold: float | None = None,
    environment: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """Update an existing metric alert rule.

    Args:
        rule_id: The metric alert rule ID.
        name: New display name.
        aggregate: New aggregation function.
        query: New search query filter.
        time_window: New time window in minutes.
        triggers: New list of trigger dicts.
        threshold_type: New threshold type (0 = above, 1 = below).
        resolve_threshold: New auto-resolve threshold value.
        environment: New environment name.
        owner: New owner identifier.

    Returns:
        Updated metric alert rule details.
    """
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if aggregate is not None:
        body["aggregate"] = aggregate
    if query is not None:
        body["query"] = query
    if time_window is not None:
        body["timeWindow"] = time_window
    if triggers is not None:
        body["triggers"] = triggers
    if threshold_type is not None:
        body["thresholdType"] = threshold_type
    if resolve_threshold is not None:
        body["resolveThreshold"] = resolve_threshold
    if environment is not None:
        body["environment"] = environment
    if owner is not None:
        body["owner"] = owner
    return _client.put(_client.org_path(f"alert-rules/{rule_id}/"), body)


@destructive_tool
def delete_metric_alert(rule_id: str) -> dict[str, str]:
    """Delete a metric alert rule.

    Args:
        rule_id: The metric alert rule ID.

    Returns:
        Deletion confirmation.
    """
    return _client.delete(_client.org_path(f"alert-rules/{rule_id}/"))


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
    if read_only:
        removed = remove_non_read_tools(mcp)
        logger.info("Read-only mode: %d non-read tools removed", removed)

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
