# clr-sentry-mcp

MCP server extending Sentry with performance insights, cron monitors, alerts, and smart issue lookup.

Companion to the third-party `selfhosted-sentry-mcp` — this server adds capabilities that the base server doesn't support (Discover API with array fields, short ID resolution, monitor/alert CRUD).

## Features

- **Smart issue lookup** — auto-detects numeric IDs, short IDs (e.g. `MYPROJECT-1W`), and full Sentry URLs
- **Performance insights** — top transactions, slow DB queries, slow HTTP requests, queue performance
- **Custom Discover queries** — proper array field support (`field[]` as repeated params)
- **Events timeseries** — trend visualization over time
- **Cron monitor CRUD** — list, get, create, update, delete
- **Issue alert CRUD** — project-scoped alert rules
- **Metric alert CRUD** — org-scoped metric alert rules
- **Read-only mode** — hide all write tools with `SENTRY_READ_ONLY=true`
- **Response trimming** — strips bloated fields (activity, participants) and truncates long SQL strings

## Installation

```bash
uvx clr-sentry-mcp
```

Or from source:

```bash
uv run --directory ~/mcp-work/clr-sentry-mcp clr-sentry-mcp
```

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SENTRY_URL` | Yes | Sentry instance URL (e.g. `https://sentry.io`) |
| `SENTRY_AUTH_TOKEN` | Yes | API auth token (Bearer) |
| `SENTRY_ORG_SLUG` | Yes | Organization slug |
| `SENTRY_READ_ONLY` | No | Set to `true` to hide write tools |

### Credentials File

Alternatively, create `~/.config/sentry/credentials.json`:

```json
{
  "url": "https://sentry.example.com",
  "auth_token": "your-token",
  "org_slug": "your-org"
}
```

Config file values override environment variables.

## Tools (22)

### Issue Lookup (1)

| Tool | Description |
|------|-------------|
| `sentry_get_issue` | Get issue by numeric ID, short ID, or URL (auto-detects) |

### Performance Insights (6)

| Tool | Description |
|------|-------------|
| `sentry_top_transactions` | Slowest endpoints by p95 response time |
| `sentry_slow_db_queries` | Slowest database queries |
| `sentry_slow_http_requests` | Slowest outbound HTTP requests |
| `sentry_queue_performance` | Queue/task worker performance |
| `sentry_discover_query` | Custom Discover query (any fields/dataset) |
| `sentry_events_timeseries` | Events over time for trend visualization |

### Cron Monitors (5)

| Tool | Description |
|------|-------------|
| `sentry_list_monitors` | List all cron monitors |
| `sentry_get_monitor` | Get monitor details |
| `sentry_create_monitor` | Create a cron monitor |
| `sentry_update_monitor` | Update a cron monitor |
| `sentry_delete_monitor` | Delete a cron monitor |

### Issue Alerts (5)

| Tool | Description |
|------|-------------|
| `sentry_list_issue_alerts` | List issue alerts for a project |
| `sentry_get_issue_alert` | Get issue alert details |
| `sentry_create_issue_alert` | Create an issue alert rule |
| `sentry_update_issue_alert` | Update an issue alert rule |
| `sentry_delete_issue_alert` | Delete an issue alert rule |

### Metric Alerts (5)

| Tool | Description |
|------|-------------|
| `sentry_list_metric_alerts` | List all metric alert rules |
| `sentry_get_metric_alert` | Get metric alert details |
| `sentry_create_metric_alert` | Create a metric alert rule |
| `sentry_update_metric_alert` | Update a metric alert rule |
| `sentry_delete_metric_alert` | Delete a metric alert rule |

## License

MIT
