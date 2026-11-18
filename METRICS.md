# Grimoire Metrics Reference

All Prometheus metrics exposed at `GET /metrics`. Scraped using standard Prometheus configuration.

## Repository Health

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `grimoire_repositories_total` | Gauge | — | Number of tracked repositories |
| `grimoire_open_issues_total` | Gauge | `repo` | Open issue count |
| `grimoire_stale_issues_total` | Gauge | `repo` | Stale issue count (exceeds configured staleness threshold) |
| `grimoire_open_pull_requests_total` | Gauge | `repo` | Open pull request count |
| `grimoire_stale_pull_requests_total` | Gauge | `repo` | Stale pull request count (exceeds configured staleness threshold) |
| `grimoire_total_branches` | Gauge | `repo` | Total branch count |
| `grimoire_workflow_status` | Gauge | `repo`, `workflow`, `branch` | Workflow status: 1 = success, 0 = failure |
| `grimoire_workflow_failures_total` | Gauge | `repo` | Number of failing workflows |
| `grimoire_check_status` | Gauge | `repo`, `check`, `branch` | Check status: 1 = pass, 0 = fail |
| `grimoire_last_commit_timestamp_seconds` | Gauge | `repo` | Unix timestamp of the most recent commit |
| `grimoire_data_fetched_timestamp_seconds` | Gauge | `repo` | Unix timestamp of the last data fetch |
| `grimoire_oldest_issue_age_seconds` | Gauge | `repo` | Age of the oldest open issue in seconds |
| `grimoire_oldest_pr_age_seconds` | Gauge | `repo` | Age of the oldest open pull request in seconds |

## Spaces

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `grimoire_repository_space_info` | Gauge | `repo`, `space` | Space membership of a repository. Always `1`; one series per (repo, space) membership, so a repository in two spaces has two series |

Repositories that belong to no space (listed only in the top-level `repositories:`) have **no series**. Series of repositories removed from a space disappear on the next update. The existing per-repository series do not carry a `space` label: join on `repo` to add it.

**Join example** (open issues per space; a repository in two spaces counts once in each):

```promql
sum by (space) (
  grimoire_open_issues_total * on(repo) group_right() grimoire_repository_space_info
)
```

Use `group_right`, not `group_left(space)`: a repository in several spaces has several `grimoire_repository_space_info` series, and Prometheus rejects a `group_left` join whose right-hand side is not unique per `repo` ("many-to-many matching not allowed"). With `group_right`, the info series is the many side and the result carries its `repo` and `space` labels. Both queries on this page were checked with `promtool test rules`.

Repositories without a space are not in the join result; to list them, use `unless`:

```promql
grimoire_open_issues_total unless on(repo) grimoire_repository_space_info
```

## Issue/PR Age Distribution

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `grimoire_issue_age_seconds` | Histogram | `repo` | Age distribution of open issues (buckets: 1d, 7d, 30d, 90d, 180d, 365d) |
| `grimoire_pr_age_seconds` | Histogram | `repo` | Age distribution of open PRs (buckets: 1h, 1d, 7d, 14d, 30d, 90d) |

**Derived queries:**
- Average issue age: `grimoire_issue_age_seconds_sum{repo="X"} / grimoire_issue_age_seconds_count{repo="X"}`
- Median issue age: `histogram_quantile(0.5, grimoire_issue_age_seconds_bucket{repo="X"})`
- Issues older than 30 days: query `grimoire_issue_age_seconds_bucket{le="2592000"}` and subtract from total

## Performance

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `grimoire_check_run_duration_seconds` | Histogram | `check` | Check execution time |
| `grimoire_action_run_duration_seconds` | Histogram | `action` | Action execution time |
| `grimoire_data_refresh_duration_seconds` | Histogram | — | Full data refresh cycle time |

## GitHub API

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `grimoire_github_api_requests_total` | Counter | `endpoint`, `status` | Total API calls made (REST, and `/graphql` for the PR CI status) |
| `grimoire_github_api_rate_limit_remaining` | Gauge | — | Current remaining REST rate limit (the GraphQL limit is not tracked) |
| `grimoire_github_api_rate_limit_reset` | Gauge | — | Unix timestamp of next rate limit reset |
