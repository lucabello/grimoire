# API reference

Grimoire exposes a REST API. Interactive documentation is available at `/docs` (Swagger UI) and `/redoc` (ReDoc) when the application is running.

## Repositories

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/repos` | List all tracked repos with latest stats |
| `GET` | `/api/repos/{owner}/{name}` | Detailed stats for one repo |
| `POST` | `/api/refresh` | Trigger an immediate data refresh |

### Workflow fields

Each entry in the `workflows` list of `GET /api/repos/{owner}/{name}` describes the latest run of one workflow on one tracked branch:

| Field | Description |
|-------|-------------|
| `name`, `branch`, `url`, `run_url` | Workflow name, branch, workflow page and run page |
| `status` | `success`, `failure`, `pending` or `unknown`. `cancelled` and `timed_out` runs report `failure` |
| `conclusion` | Raw GitHub conclusion (`success`, `failure`, `cancelled`, `timed_out`, ...); `null` while the run is in progress |
| `run_id` | GitHub run ID |
| `run_attempt` | Attempt number of the run (`1` for the first attempt, higher after re-runs) |
| `event` | Event that triggered the run, for example `push`, `schedule` or `release` |
| `head_sha` | Commit the run executed on |
| `created_at`, `run_started_at`, `updated_at` | Run timestamps in ISO 8601 (UTC) |

The run fields are `null` when no value is available.

## Checks

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/checks` | List all check definitions + enabled state |
| `GET` | `/api/checks/{slug}/results` | Latest results for a check |
| `GET` | `/api/checks/{slug}/runs` | Run history (reverse chronological) |
| `POST` | `/api/checks/{slug}/run` | Trigger check; optional `?repo=owner/name` |
| `POST` | `/api/checks/{slug}/toggle` | Toggle check enabled state |

## Actions

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/actions` | List all action definitions |
| `GET` | `/api/actions/{slug}/runs` | Run history (reverse chronological) |
| `GET` | `/api/actions/{slug}/runs/{id}` | Specific run details + logs |
| `POST` | `/api/actions/{slug}/run` | Trigger action; optional `?repo=owner/name`; 409 if running |

## Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check (Docker HEALTHCHECK / k8s probes) |

## OpenAPI

Auto-generated OpenAPI schema is available at:

- `/docs` — Swagger UI (interactive)
- `/redoc` — ReDoc (readable)
- `/openapi.json` — Raw OpenAPI JSON schema
