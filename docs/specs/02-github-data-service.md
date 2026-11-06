# Module 2: GitHub Data Service

Fetch and cache repository metadata from the GitHub API. Resolve repos from static lists and team memberships. Provide a service layer that the rest of the app queries for repo stats.

**Dependencies:** Module 1 (config, models, database).

## 2.1 — GitHub API Client

**File:** `src/grimoire/github/client.py`

Thin async wrapper around `httpx.AsyncClient` for the GitHub REST API.

### Methods

```python
class GitHubClient:
    def __init__(self, token: str, engine: AsyncEngine): ...

    async def get_team_repos(self, org: str, team_slug: str) -> list[dict]: ...
    async def get_repo(self, full_name: str) -> dict: ...
    async def get_open_issues(self, full_name: str) -> list[dict]: ...
    async def get_open_pull_requests(self, full_name: str) -> list[dict]: ...
    async def get_workflows(self, full_name: str) -> list[dict]: ...
    async def get_workflow_runs(self, full_name: str, branch: str | None = None) -> list[dict]: ...
    async def get_default_branch(self, full_name: str) -> str: ...
    async def get_branches(self, full_name: str) -> list[dict]: ...
    async def get_branch(self, full_name: str, branch: str) -> dict: ...
    async def get_pull_request_rollups(
        self, prs: list[tuple[str, int]]
    ) -> dict[tuple[str, int], PRRollup]: ...
```

`PRRollup(ci_state, review_decision, merge_state)` is a frozen dataclass (`ci_state`: `success`, `failure`, `pending` or `none`; the other two are lower-cased GraphQL enums or `None`). `ROLLUP_BATCH_SIZE = 20` is the number of pull requests one call should carry.

### Core implementation

- Accept `token` in constructor, set `Authorization: Bearer {token}` header.
- Use `httpx.AsyncClient` with base URL `https://api.github.com`.
- Handle pagination via GitHub's `Link` header — follow `rel="next"` until exhausted.
- Use `per_page=100` on all list endpoints to minimize pagination.
- `_request` takes an optional JSON body (`json_body`) so that the GraphQL `POST /graphql` goes through the same retries, metrics and error handling.

### API call optimization

The GitHub REST API has a rate limit of 5,000 requests/hour for authenticated users. With 50+ repos being refreshed periodically, minimizing calls is critical.

**Conditional requests (ETags):**
- On every API response, store the `ETag` and `Last-Modified` headers in the `CachedETag` DB table, keyed by the full request URL.
- On subsequent requests, send `If-None-Match` (ETag) and `If-Modified-Since` headers.
- If GitHub returns `304 Not Modified`, skip parsing and use cached data. These 304 responses do **not** count against the rate limit.
- This is the primary optimization: after the first full fetch, most subsequent refreshes are free.

**Efficient fetching per resource type:**
- **Issues:** Use `GET /repos/{owner}/{repo}/issues?state=open&per_page=100`. This returns both issues and PRs mixed together — filter client-side by excluding items that have a `pull_request` key in the JSON. Full list is needed to compute stale counts.
- **Pull requests:** Use `GET /repos/{owner}/{repo}/pulls?state=open&per_page=100` (the Pulls API, not the Issues API). Full list is needed to compute stale counts. The same list supplies every open PR for the PRs page (see 2.2): `number`, `title`, `html_url`, `user.login`, `created_at`, `updated_at`, `draft`, `labels[].name`, `base.ref`, `head.ref`, `head.sha` and `auto_merge`. Bots are reported as `renovate[bot]` / `dependabot[bot]` (`user.type` `Bot`; checked against real payloads). The list has no CI, review or merge state.
- **Workflows:** Use `GET /repos/{owner}/{repo}/actions/workflows` to list workflows, then `GET /repos/{owner}/{repo}/actions/workflows/{id}/runs?branch={branch}&per_page=1` to get only the latest run per workflow+branch. Before fetching runs, apply the repo's `workflow_include`/`workflow_exclude` glob patterns (via `fnmatch`) to skip irrelevant workflows. For the repo's default branch only, also fetch `GET /repos/{owner}/{repo}/actions/workflows/{id}/runs?per_page=10` (no `branch` param), discard any returned run whose `head_branch` matches another *tracked* branch of the same repo (it belongs to that branch, not the default one), and compare `created_at` of the remaining candidates against the branch-filtered result, keeping whichever is more recent. This handles workflows triggered by non-branch events (e.g. `release`, tag pushes): their `head_branch` is a tag, not the default branch, so the branch-filtered query can return an old (or no) run while a newer run is invisible to the filter. Non-default tracked branches never get this extra check: periodic or release-only workflows never actually ran there and must not be shown as if they did.
- **Workflow run details:** The run that decides a workflow's status (the same run object, from the same requests — no extra calls) also supplies `run_id` (`id`), `run_attempt`, `event`, `head_sha`, `conclusion` (the raw GitHub value, `null` while the run is in progress), `created_at`, `run_started_at` and `updated_at`. `status` keeps its coarse mapping (`cancelled` and `timed_out` map to `failure`); `conclusion` carries the raw value. A status reused from the previous refresh (304 on the runs request, or a failed workflow listing) keeps its previous run details.
- Use `per_page=100` on all list endpoints to minimize pagination.

**GraphQL (PR CI state only):** `get_pull_request_rollups` sends one `POST /graphql` for a batch of `(full_name, number)` pairs. Repositories are aliases (`r0`, `r1`, …) and pull requests aliases inside them (`p12: pullRequest(number: 12) { reviewDecision mergeStateStatus statusCheckRollup { state } }`); owner and name are inlined with `json.dumps`. REST stays the source of everything else.

- **Cost:** a query without connections costs one point of GitHub's separate GraphQL limit (5,000 points/hour, `X-RateLimit-Resource: graphql`). Observed on 2026-11-06 with `rateLimit { cost }` against real repositories: `cost` was 1 for queries of 3, 10, 15, 20, 25 and 30 pull requests.
- **Batch size:** GitHub computes the check rollups while answering and gives up with `502 Bad Gateway` after about 10 s. Observed: 20 pull requests across many repositories took 3.4-6.9 s, 25 took 8.5 s and 50 timed out, so `ROLLUP_BATCH_SIZE` is 20. A refresh of 145 matching PRs sent 8 requests.
- **No ETag:** GraphQL has none, so every refresh queries all matching PRs again (`use_etag=False`); that also keeps pending-to-success transitions current.
- **Rate-limit tracker:** `_update_rate_limit` ignores responses whose `X-RateLimit-Resource` is `graphql`; otherwise the GraphQL bucket would overwrite the REST tracker and trigger degraded mode falsely. The Prometheus gauges stay REST-only; the requests are counted under the endpoint label `/graphql`.
- **Errors:** GraphQL answers HTTP 200 with an `errors` array. A `RATE_LIMITED` error raises `RateLimitError`; `NOT_FOUND` errors (a repository or pull request that no longer exists) leave a `null` alias and the item is simply absent from the result; any other error, or a missing `data`, raises `GitHubAPIError`. 5xx responses are retried like REST ones.

**Rate limit awareness:**
- After every API response, read `X-RateLimit-Remaining` and `X-RateLimit-Reset` headers.
- Track remaining calls and reset time. Expose both as Prometheus metrics.
- If remaining < 10% of limit: enter **degraded mode** — skip non-essential refreshes, only fetch data the user is actively viewing. Show a warning banner on the dashboard.
- If remaining = 0: pause all GitHub API calls until the reset timestamp. Show a global warning with countdown.

### Failure handling

- Every GitHub API call is wrapped in try/except with specific error types.
- Typed exceptions: `GitHubAPIError`, `RateLimitError`, `NotFoundError`.
- **Transient failures** (5xx, timeout, network error): retry up to 3 times with exponential backoff (1s, 2s, 4s).
- **Persistent failures** (4xx other than 404, repeated 5xx): log the error, keep last-known cached data, mark the affected repo with a warning.
- **404 Not Found**: log as warning, mark repo with "Repository not found" warning. Do not retry.
- **403 Forbidden**: could be rate limit or permissions. Check headers to distinguish. If rate limit, trigger degraded mode. If permissions, log and warn.
- Failures never raise to the caller — always return cached data with warnings populated.

## 2.2 — Repository Resolution Service

**File:** `src/grimoire/github/service.py`

High-level service that resolves the configured repo sources into a list of `TrackedRepository` objects and fetches their stats. Backlog weighting is not stored on the repository model; it is resolved later from `backlog.repository_weights` when scoring backlog items.

### Functions

```python
async def resolve_repositories(
    config: GrimoireConfig, client: GitHubClient
) -> list[TrackedRepository]:
    """
    For each entry in config.repositories:
      - StaticRepoSource: validate repo exists, resolve default branch if
        no branches specified.
      - TeamRepoSource: fetch team repos, exclude listed repos, filter out
        archived repos.
    Deduplicate by full_name (merge branches if same repo appears in
    multiple sources).
    Always filter out archived repositories.
    """

async def fetch_repository_stats(
    repo: TrackedRepository,
    client: GitHubClient,
    staleness: StalenessConfig,
) -> RepositoryStats:
    """
    For a single tracked repo, fetch:
      - open issues count + stale issues count
        (no comments in staleness.issues_days)
      - open PRs count + stale PRs count
        (no pushes or comments in staleness.pull_requests_days)
      - workflow statuses for each observed branch
      - last commit time across all observed branches
      - total branch count
    Return a RepositoryStats object. On failure, return stats with
    warnings populated and counts from the last cached values.

    Every open PR of the pulls list is also kept as an OpenPullRequest in
    open_pr_items (no extra request). On a 304 the previous items are kept
    (with their CI state), exactly like the stale items. A successful list
    replaces them, so closed and merged PRs disappear.

    Non-existing branches (e.g., from config) are silently ignored: 404
    responses from GitHub API for branch info or workflow runs do not
    generate warnings and are simply skipped.
    """

async def refresh_all_stats(
    config: GrimoireConfig, client: GitHubClient
) -> list[RepositoryStats]:
    """
    Resolve repos, then fetch stats for all concurrently
    (bounded by asyncio.Semaphore(10)).
    Then, if prs.groups is configured, refresh_pull_request_ci fills CI,
    review and merge state of the PRs that match a group, through
    sequential GraphQL batches (never inside the concurrent fetch).
    Write results to DB cache. Update in-memory cache.
    """
```

### PR CI state

```python
async def refresh_pull_request_ci(
    client: GitHubClient,
    groups: list[PRGroup],
    stats_list: list[RepositoryStats],
    previous: dict[str, RepositoryStats],
) -> None: ...
```

- The targets are the open PRs for which `prs.matches` is true for at least one group; with no groups nothing is requested.
- A PR GitHub reported gets `ci_state`, `review_decision`, `merge_state` and `ci_fetched_at = now`. Mapping: `SUCCESS` → `success`; `FAILURE` and `ERROR` → `failure`; `PENDING` and `EXPECTED` → `pending`; a null rollup → `none` (no checks); the review and merge enums are lower-cased.
- A PR that was not reported (failed batch, or `NOT_FOUND`) keeps the state it had in `previous` when its `head_sha` is unchanged and has none otherwise.
- A failed batch appends `Could not fetch CI status for <repo>: …` to the warnings of each repository in the batch; the list itself is unaffected.
- Known limit, unchanged: `_paginated_get` returns a truncated list when page 1 answers 200 and a later page answers 304, which only matters for repositories with more than 100 open PRs.

## 2.3 — Caching Strategy (disk + memory)

All fetched data is persisted to the SQLite database as the **persistent cache**:

- On startup, load the latest cached data from the DB immediately — the dashboard is functional right away without any GitHub API calls.
- Keep an in-memory copy (loaded from DB) for fast reads by the web layer. The web layer never calls the GitHub API directly.
- The scheduler refreshes data on a cron schedule (`refresh_schedule`): fetches from GitHub API, writes to DB, updates in-memory cache.
- Each cached record stores a `fetched_at` timestamp. The dashboard shows "last updated: X minutes ago" so the user knows data freshness.
- A restart only triggers an API refresh if the cache is older than one cron interval (computed from `refresh_schedule`).

### DB tables for cached data

| Table | Columns |
|---|---|
| `cached_repository` | full_name, default_branch, archived, source, last_commit_at, total_branches, fetched_at |
| `cached_issue` | repo_full_name, title, number, url, author, created_at, last_comment_at, fetched_at |
| `cached_pull_request` | repo_full_name, title, number, url, author, created_at, last_push_at, last_comment_at, fetched_at |
| `cached_open_pull_request` | repo_full_name, number, title, url, author, created_at, updated_at, draft, labels_json, base_ref, head_ref, head_sha, auto_merge, ci_state, review_decision, merge_state, ci_fetched_at, fetched_at (the last four state fields nullable) |
| `cached_workflow_status` | repo_full_name, workflow_name, branch, status, url, run_url, run_id, run_attempt, event, head_sha, conclusion, run_created_at, run_started_at, run_updated_at, fetched_at (all run fields nullable) |
| `cached_etag` | endpoint_url, etag, last_modified |

The rows of `cached_open_pull_request` are replaced per repository on every save (like the stale PR rows), and pruned with the repository. `cached_pull_request` keeps only the stale PRs that the dashboard and backlog use; it is unchanged.

Per-repo backlog weights are not persisted in this layer; the backlog engine resolves them from config when scoring items.

## 2.4 — Repos REST API

**File:** `src/grimoire/github/router.py`

Expose cached repository data and a manual refresh trigger via REST endpoints.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/repos` | List all tracked repos with latest cached stats |
| `GET` | `/api/repos/{owner}/{name}` | Detailed stats for a single repo (issues, PRs, workflows, checks) |
| `POST` | `/api/refresh` | Trigger an immediate data refresh. Returns 202 Accepted; refresh runs as a background task |
| `GET` | `/api/refresh/status` | Returns current refresh progress: `{ running, completed, total }` |

### Refresh progress tracking

`refresh_all_stats()` maintains an in-memory `RefreshProgress(completed, total)` dataclass during execution. Each completed repo increments `completed`. The progress is cleared (set to `None`) in a `finally` block when the refresh finishes or fails.

The initial `total` is computed from config before any async work:
- Static repos are counted directly from `config.repositories`.
- After the DB cache loads (fast), the estimate is refined with the cached repo count (covers team repos from previous refreshes).
- After `resolve_repositories()` returns, the total is set to the exact count.

This ensures the progress bar shows a meaningful denominator immediately.

Helper functions:
- `is_refresh_running() -> bool` — check if a refresh is in progress.
- `get_refresh_progress() -> RefreshProgress | None` — get the current progress tracker.

The web layer polls `GET /api/refresh/status` (or the HTMX partial `GET /partials/refresh-status`) every 2 seconds while a refresh is running to display a progress indicator.

### Response models

```python
class RepoSummary(BaseModel):
    full_name: str
    default_branch: str
    branches: list[str]
    source: str                     # "static" | "team:org/team-name"
    open_issues: int
    stale_issues: int
    open_pull_requests: int
    stale_pull_requests: int
    workflow_failures: int          # count of failing workflows across all branches
    check_failures: int             # count of failing checks across all branches
    last_commit_at: datetime | None # most recent commit across observed branches
    total_branches: int             # total number of branches in the repo
    warnings: list[str]
    fetched_at: datetime

class RepoListResponse(BaseModel):
    repositories: list[RepoSummary]
    last_refresh: datetime | None

class RepoDetailResponse(BaseModel):
    full_name: str
    default_branch: str
    branches: list[str]
    source: str
    open_issues: int
    stale_issues: list[IssueResponse]
    open_pull_requests: int
    stale_pull_requests: list[PullRequestResponse]
    workflows: list[WorkflowStatusResponse]
    checks: list[CheckResultResponse]   # from checks engine
    warnings: list[str]
    fetched_at: datetime

class IssueResponse(BaseModel):
    title: str
    number: int
    url: str
    created_at: datetime
    last_comment_at: datetime | None

class PullRequestResponse(BaseModel):
    title: str
    number: int
    url: str
    author: str
    created_at: datetime
    last_push_at: datetime | None
    last_comment_at: datetime | None

class WorkflowStatusResponse(BaseModel):
    name: str
    branch: str
    status: str
    url: str
    run_url: str
    run_id: int | None
    run_attempt: int | None      # 1 for the first attempt
    event: str | None            # triggering event, e.g. "push", "schedule"
    head_sha: str | None
    conclusion: str | None       # raw GitHub conclusion; None while in progress
    created_at: datetime | None  # ISO 8601 in JSON
    run_started_at: datetime | None
    updated_at: datetime | None

class RefreshResponse(BaseModel):
    message: str   # "Refresh started"
```

### Notes

- `GET /api/repos` reads from the in-memory cache — no GitHub API calls, sub-millisecond.
- `GET /api/repos/{owner}/{name}` also reads from cache, enriched with check results from the DB.
- `POST /api/refresh` schedules an immediate background refresh via the scheduler, returns 202 immediately. The web layer detects completion via the cache's `fetched_at` timestamp.

## Acceptance Criteria

- [ ] `GET /api/repos` returns all tracked repos with correct cached stats
- [ ] `GET /api/repos/{owner}/{name}` returns detailed info including stale items, workflows, checks
- [ ] `GET /api/repos/{owner}/{name}` returns 404 for untracked repos
- [ ] `POST /api/refresh` returns 202 and triggers a background refresh
- [ ] `resolve_repositories` correctly handles static repos, team repos, exclusions, and archived filtering
- [ ] `fetch_repository_stats` returns correct issue/PR/workflow counts
- [ ] Stale detection works: issues with no comments beyond threshold, PRs with no pushes/comments beyond threshold
- [ ] Pagination works for repos with >30 issues/PRs
- [ ] Rate limiting is handled gracefully (back off, retry)
- [ ] Conditional requests with ETags reduce API call count (304 responses don't re-fetch)
- [ ] Every open PR is kept with its pulls-list fields without an additional REST request; a 304 on the pulls list keeps them, and a closed PR is gone after the next successful list
- [ ] A refresh with M group-matching PRs sends ceil(M/20) GraphQL POSTs; with no groups it sends none; a GraphQL response never changes the REST rate-limit tracker
- [ ] A GraphQL failure keeps the PR list, adds a warning and keeps the previous CI state for an unchanged head commit
- [ ] Data is persisted to SQLite and survives application restarts
- [ ] On startup, cached data is loaded from DB without making any API calls
- [ ] Failures are captured as warnings in `RepositoryStats.warnings`, not exceptions
- [ ] Rate limit approaching/exhausted triggers degraded mode with user-visible warning
- [ ] Tests use `respx` to mock GitHub API responses
