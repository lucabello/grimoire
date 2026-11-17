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

## PRs

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/prs` | Open PRs of each [PR group](configuration.md#prs) with their CI, review and merge state |

The endpoint reads Grimoire's cache: it makes no GitHub request. It returns the groups in configuration order:

```json
{
  "last_refresh": "2026-11-06T21:05:00Z",
  "groups": [
    {
      "name": "Maintenance",
      "total": 12, "failing": 3, "pending": 1, "passing": 7, "no_checks": 1,
      "pull_requests": [
        {
          "repo": "org/api", "number": 12, "title": "Update dependency foo to v2",
          "url": "https://github.com/org/api/pull/12",
          "author": "renovate[bot]",
          "created_at": "2026-11-01T09:00:00Z", "updated_at": "2026-11-06T08:30:00Z",
          "draft": false, "labels": ["dependencies"],
          "base": "main", "head_ref": "renovate/foo-2.x", "head_sha": "5d7c…",
          "auto_merge": false,
          "ci": "failure", "ci_url": "https://github.com/org/api/pull/12/checks",
          "review_decision": "review_required", "merge_state": "blocked",
          "ci_checked_at": "2026-11-06T21:05:00Z"
        }
      ]
    }
  ]
}
```

- `author` is the login GitHub reports (`renovate[bot]` for a bot app).
- `ci` is `success`, `failure`, `pending` or `none` (the PR has no checks); `review_decision` is `approved`, `changes_requested` or `review_required`; `merge_state` is GitHub's merge state in lower case (`clean`, `dirty`, `behind`, `blocked`, `unstable`, `has_hooks`, `unknown`). These and `ci_checked_at` are `null` when GitHub has not reported them (for example when its answer failed).
- A PR that matches several groups is listed in each of them.
- There are no query parameters: filter the list on your side.

## Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check (Docker HEALTHCHECK / k8s probes) |

## OpenAPI

Auto-generated OpenAPI schema is available at:

- `/docs` — Swagger UI (interactive)
- `/redoc` — ReDoc (readable)
- `/openapi.json` — Raw OpenAPI JSON schema
