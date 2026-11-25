# API reference

Grimoire exposes a REST API. Interactive documentation is available at `/docs` (Swagger UI) and `/redoc` (ReDoc) when the application is running.

The list endpoints `/api/repos/`, `/api/checks/` and `/api/actions/` end with a slash. Without it the server answers `307` and redirects to the slashed URL, which `curl` only follows with `-L`.

## Spaces

Spaces are configured with [`spaces` and `space_files`](configuration.md#spaces). They filter what you see; they are not an access control.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/spaces` | The configured spaces, plus **Unassigned** while repositories belong to no space |

The response is a list of `{id, name, description, repo_count}`. The Unassigned entry has the id `_unassigned`. The list is empty when no spaces are configured.

### Filtering by space

`GET /api/repos/`, `/api/checks/`, `/api/checks/{slug}/results`, `/api/actions/`, `/api/prs`, `/api/problems` and `/api/backlog/export` accept `?spaces=` with comma-separated space ids, for example `?spaces=platform,web`. `_unassigned` selects the repositories that belong to no space.

- Without the parameter, or with no known id, the endpoint returns everything. Unknown ids are ignored. Selecting every entry that `GET /api/spaces` lists, including **Unassigned** when it is listed, is the same as selecting none.
- The API **never** reads the selection cookie that the web pages use. Its result depends on the URL alone.
- A repository is kept if it belongs to any selected space. The `workflow_failures` count of `/api/repos` only counts the workflows that the selected spaces' filters allow.
- Checks and actions are listed when they have no `spaces` or when one of their `spaces` is selected; `_unassigned` alone lists only the ones without `spaces`. This only affects the list: it does not change which repositories a check or action runs against.
- `/api/prs` keeps every group and returns an empty `pull_requests` list for a group with nothing left.

## Repositories

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/repos` | List all tracked repos with latest stats; optional [`?spaces=`](#filtering-by-space) |
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

Every repository in `GET /api/repos` and `GET /api/repos/{owner}/{name}` has a `spaces` list with the ids of the spaces it belongs to (empty when it belongs to none).

## Checks

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/checks` | List check definitions + enabled state, each with its `spaces`; optional [`?spaces=`](#filtering-by-space) |
| `GET` | `/api/checks/{slug}/results` | Latest results for a check; optional [`?spaces=`](#filtering-by-space) keeps the results of repos in those spaces |
| `GET` | `/api/checks/{slug}/runs` | Run history (reverse chronological) |
| `POST` | `/api/checks/{slug}/run` | Trigger check; optional `?repo=owner/name` |
| `POST` | `/api/checks/{slug}/toggle` | Toggle check enabled state |

## Actions

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/actions` | List action definitions, each with its `spaces`; optional [`?spaces=`](#filtering-by-space) |
| `GET` | `/api/actions/{slug}/runs` | Run history (reverse chronological) |
| `GET` | `/api/actions/{slug}/runs/{id}` | Specific run details + logs |
| `POST` | `/api/actions/{slug}/run` | Trigger action; optional `?repo=owner/name`; 409 if running |

## PRs

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/prs` | Open PRs of each [PR group](configuration.md#prs) with their CI, review and merge state; optional [`?spaces=`](#filtering-by-space) |

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
- The only query parameter is [`?spaces=`](#filtering-by-space); filter by anything else on your side.

## Problems

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/problems` | What is currently wrong, as a flat list with links; optional [`?spaces=`](#filtering-by-space) and `?kind=` |

The endpoint answers "what is wrong right now?" in one request: failing workflows, failing PR CI, failing checks and repository warnings. It says *what* and *where*, not *why*: there are no logs, failing steps or analysis, and the items are neither scored nor ranked (they are ordered by repository name). The path has no trailing slash. Unlike the [backlog](#backlog), which is Markdown for people, every item is structured.

```json
{
  "last_refresh": "2026-11-06T21:05:00Z",
  "counts": {"workflow_failure": 1, "pr_ci_failure": 0, "check_failure": 1, "repo_warning": 0},
  "problems": [
    {
      "kind": "workflow_failure", "severity": "error",
      "repo": "org/api", "branch": "main",
      "subject": "CI", "summary": "Workflow 'CI' failing on main",
      "url": "https://github.com/org/api/actions/runs/123",
      "observed_at": "2026-11-06T20:58:00Z",
      "detail": {
        "kind": "workflow_failure", "run_id": 123, "run_attempt": 1, "event": "push",
        "conclusion": "failure", "head_sha": "5d7c…",
        "run_url": "https://github.com/org/api/actions/runs/123"
      }
    }
  ]
}
```

- `last_refresh` is when the data was last refreshed. It is `null` until the first refresh finishes: an empty `problems` list then means there is no data yet, **not** that everything is healthy.
- The endpoint reads Grimoire's cache and the latest check results. It makes no GitHub request, so GitHub data is as fresh as the last periodic refresh, and check results are those of the last check runs. To refresh GitHub data first, call [`POST /api/refresh`](#repositories) (it answers `202` and refreshes in the background), wait until `GET /api/refresh/status` reports `"running": false`, then read `GET /api/problems`.
- `counts` always lists every kind (`0` when there is none). It covers the selected spaces and ignores `?kind=`, so it shows what the filter left out.
- `?kind=` takes comma-separated kinds, for example `?kind=workflow_failure,check_failure`. An unknown kind answers `422`.
- Fields are only ever added to the response, never renamed or removed. Read the fields you need and ignore the others.

Every item has these fields:

| Field | Description |
|-------|-------------|
| `kind` | One of the kinds below; it also selects the shape of `detail` |
| `severity` | `error` or `warning`. A check defined with `severity: warning` reports `warning`; repository warnings are `warning`; everything else is `error` |
| `repo` | `owner/name` |
| `branch` | Branch the problem is about; `null` when there is none |
| `subject` | What is affected: the workflow or check name, or `#number title` for a PR |
| `summary` | One line describing the problem |
| `url` | Where to look on GitHub; `null` when there is no link |
| `observed_at` | When Grimoire last saw the problem (see below); `null` when unknown |
| `detail` | Kind-specific fields |

`observed_at` is **not** when the problem started: Grimoire stores only the latest run of a workflow or check, so "failing since" is unknown.

| Kind | One item per | `branch` | `url` | `observed_at` |
|------|--------------|----------|-------|---------------|
| `workflow_failure` | failing workflow on a tracked branch | the branch | the run | start of the latest run (its creation time when it has no start) |
| `pr_ci_failure` | open PR whose CI state is `failure` | the PR's head branch | the PR | when GitHub last reported its CI |
| `check_failure` | enabled check whose latest result on a tracked branch failed | the branch | `null` | time of that result |
| `repo_warning` | warning of a repository (for example a failed GitHub request) | `null` | `null` | when the repository was fetched |

`detail` fields by kind:

| Kind | `detail` fields |
|------|-----------------|
| `workflow_failure` | `run_id`, `run_attempt`, `event`, `conclusion`, `head_sha`, `run_url` |
| `pr_ci_failure` | `number`, `author`, `draft`, `labels`, `review_decision`, `merge_state`, `auto_merge`, `head_sha`, `ci_url`, `ci_checked_at` |
| `check_failure` | `check_slug`, `output`, `output_truncated`, `results_path`, `timestamp` |
| `repo_warning` | `warning` |

- **`workflow_failure`** follows the [workflow fields](#workflow-fields): the workflows the selected spaces' filters allow, with `status` `failure`. Cancelled and timed-out runs count as failures; `detail.conclusion` (`failure`, `cancelled`, `timed_out`, ...) tells them apart.
- **`pr_ci_failure`** only covers PRs that match a [PR group](configuration.md#prs): Grimoire fetches the CI state of those PRs only, so a PR that no group matches never appears here. A group needs at least one matcher; to cover every PR, add two groups, one with `draft: true` and one with `draft: false`. A PR in several groups appears once.
- **`check_failure`** matches the backlog: checks that are enabled, on the tracked branches (the configured `branches`, or the default branch), whose latest result failed. `output` is the last 2000 characters of the check's output, because failures show at the end; `output_truncated` is `true` when it was cut. `results_path` is the path of the full results (`/api/checks/{slug}/results`), including the prefix the application is served under.
- **`repo_warning`**: `detail.warning` is the warning text.

## Cross-site requests

Requests that change state (`POST`, `PUT`, `PATCH`, `DELETE`) are rejected with `403` when a browser reports that they come from another site: a `Sec-Fetch-Site` header other than `same-origin` or `none`, or, when that header is missing, an `Origin` header whose host is not the one the request was sent to. This stops a web page you visit from triggering checks or actions on a Grimoire that only your network can reach.

Requests without these headers, such as `curl` or a script, are not affected, so API automation keeps working. If a reverse proxy rewrites the `Host` header, browsers that do not send `Sec-Fetch-Site` are rejected: make the proxy pass the original `Host` (see [Deploy Grimoire](../how-to/deploy.md#reverse-proxy)).

## Backlog

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/backlog/export` | The backlog as Markdown, grouped by priority tier |

The response has the content type `text/markdown`. All parameters are optional:

| Parameter | Description |
|-----------|-------------|
| `categories` | Comma-separated problem categories to keep, for example `failing_workflow,stale_pr` |
| `repos` | Comma-separated repository full names to keep |
| `min_score` | Only items with at least this score |
| `spaces` | Comma-separated space ids; see [Filtering by space](#filtering-by-space). The backlog page passes its own selection here, so its export matches what the page shows |

## Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check (Docker HEALTHCHECK / k8s probes) |

## OpenAPI

Auto-generated OpenAPI schema is available at:

- `/docs` — Swagger UI (interactive)
- `/redoc` — ReDoc (readable)
- `/openapi.json` — Raw OpenAPI JSON schema
