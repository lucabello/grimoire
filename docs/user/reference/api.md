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

## Maintenance

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/maintenance/groups` | GitHub PR search `query` and `url` for each [maintenance group](configuration.md#maintenance) |

The endpoint only reads the configuration and the tracked repositories: it makes no GitHub request. It returns one item per search, in configuration order:

```json
[
  {
    "name": "renovate",
    "query": "is:pr is:open (repo:org/api OR repo:org/web) author:app/renovate",
    "url": "https://github.com/pulls?q=is%3Apr+is%3Aopen+%28repo%3Aorg%2Fapi+OR+repo%3Aorg%2Fweb%29+author%3Aapp%2Frenovate",
    "part": 1,
    "parts": 1
  }
]
```

A group with very many repositories is split into several items with the same `name` (`part` of `parts`); a group with no tracked repository in scope has no item.

To run a query yourself, use GitHub's advanced search (the queries contain `OR` in parentheses). The REST call with `advanced_search=true` is the recommended way; run it once for every part of a split group:

```bash
gh api -X GET search/issues -f advanced_search=true -f q="$query" --jq .total_count
gh api -X GET search/issues -f advanced_search=true -f q="$query" -f per_page=100 --paginate --jq '.items[].html_url'
```

Search returns at most 1000 results, 100 per page (GitHub's documented limits).

Other `gh` commands are not reliable for these queries. Verified on `gh` 2.46.0, read-only:

- `gh search prs "$query"` (the whole query as one argument) sends it as a single quoted term and silently returns no results. The same was observed on `gh` 2.103.3.
- `gh search prs -- $query` (the query split into words) fails with a 422 on 2.46.0, because that command cannot select the advanced engine. It works only on a recent `gh` (observed on 2.103.3).
- `gh pr list --search "$query"` works per repository only. On 2.46.0 a parenthesised `OR` over qualifiers such as `author:` or `draft:` silently returned 0 results; it is evaluated on 2.103.3. Do not rely on it with an older `gh`.

## Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check (Docker HEALTHCHECK / k8s probes) |

## OpenAPI

Auto-generated OpenAPI schema is available at:

- `/docs` — Swagger UI (interactive)
- `/redoc` — ReDoc (readable)
- `/openapi.json` — Raw OpenAPI JSON schema
