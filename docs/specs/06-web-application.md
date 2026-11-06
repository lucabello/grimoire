# Module 6: Web Application (Dashboard, Repo Detail, Actions, Checks, Backlog, Maintenance Pages)

Build the HTMX-powered web frontend with six pages: dashboard overview, individual repository detail, actions management, checks management, backlog prioritization, and maintenance PR groups.

**Dependencies:** Modules 1–5 (all backend services).

## 6.1 — Application Wiring

**File:** `src/grimoire/app.py`

Update `create_app()` to:

- Mount all API routers (checks, actions, repos, maintenance).
- Mount web router for HTML pages.
- Mount static files at `/static`.
- Set up Jinja2 template environment.
- Initialize on startup: database, workspace, scheduler, GitHub client, initial data load from cache.
- Register shutdown hooks: close scheduler, close HTTP clients.

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    config = load_config()
    engine = await get_engine(config.database_path)
    await create_tables(engine)
    # Load cached data from DB (instant, no API calls)
    # Initialize GitHub client, workspace manager, scheduler
    # Start background refresh task
    yield
    # Shutdown: close scheduler, close HTTP clients

def create_app() -> FastAPI:
    app = FastAPI(
        title="Grimoire",
        description="GitHub repository monitoring dashboard",
        lifespan=lifespan,
    )

    # Mount routers
    app.include_router(checks_router, prefix="/api")
    app.include_router(actions_router, prefix="/api")
    app.include_router(maintenance_router, prefix="/api")
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory="..."), name="static")

    return app
```

### URL prefix (root_path)

The app may be served under a path prefix (ASGI `root_path`, e.g. behind an ingress). Templates must never hard-code absolute URLs:

- Jinja: a context processor on `templates` injects `base_path` (`root_path` without trailing slash). Write `href="{{ base_path }}/repo/..."`, `hx-get="{{ base_path }}/partials/..."`.
- JavaScript: `base.html` defines `const BASE_PATH = {{ base_path | tojson }};` — build URLs as `BASE_PATH + '/partials/...'`.
- Server redirects (e.g. `HX-Redirect`) use `_base_path(request)`.

## 6.2 — Loading Page

**Route:** `GET /` (when cache is empty and a refresh is running)
**Template:** `templates/loading.html`

When Grimoire starts for the first time (no DB cache), the initial data refresh runs as a background task so the web server starts immediately. The dashboard route detects this "initial loading" state (no repos in cache AND `is_refresh_running()` returns `True`) and renders a loading page instead of the normal dashboard.

### Layout

- Centered card with the Grimoire crystal ball icon (🔮)
- "Fetching repository data…" heading
- Explanatory text: "Grimoire is loading data from GitHub. This may take a moment on first startup."
- DaisyUI `<progress>` bar showing `completed / total` (indeterminate when `total == 0`)
- Text: "X of Y repositories fetched"

### HTMX Polling

The loading page contains a container that polls `GET /partials/loading-status` every 2 seconds. The partial:

- Returns updated progress HTML while the refresh is running
- Returns an empty body with `HX-Redirect: /` header when the refresh completes, causing a full page reload to the now-populated dashboard

### Non-blocking Startup (`app.py`)

When the DB cache is stale or empty, `lifespan()` launches `refresh_all_stats()` as an `asyncio.create_task()` instead of awaiting it. This allows the server to start serving immediately:

- **Fresh cache**: Load synchronously (instant), no background task
- **Stale cache**: Serve stale data immediately, background refresh updates it
- **No cache**: Show loading page, background refresh populates data

The background task calls the same `_do_refresh()` callback used by the scheduler and manual refresh button, then runs `workspace.setup()` with the refreshed repos.

### Loading Status Endpoint

**Route:** `GET /partials/loading-status`

Returns the `partials/loading_progress.html` template with current `RefreshProgress` data. When the refresh is no longer running, returns `HX-Redirect: /` to redirect the browser to the dashboard.

## 6.3 — Dashboard Page

**Route:** `GET /`
**Template:** `templates/dashboard.html`

### Layout

**Header area:**
- "Grimoire" title/logo.
- "Last updated: X minutes ago" timestamp (from the latest `fetched_at`).
- Manual "Refresh" button.
- Global warning banner (if applicable): rate limit warning, degraded mode notice.

**Stats bar** — horizontal stat cards showing aggregate metrics. Every panel displays its total count as the main value in a neutral theme color, with a color-coded breakdown subtitle (`text-sm`):
- **Repositories** (`text-primary`) — total count, breakdown: `X failing` (red) · `Y warning` (yellow) · `Z healthy` (green). Counts derived from each repo's `health_status`.
- **Open Issues** (`text-info`) — total count, breakdown: `X stale` (yellow) if any.
- **Open PRs** (`text-info`) — total count, breakdown: `X stale` (yellow) if any.
- **Workflows** (`text-accent`) — total count, breakdown: `X failing` (red) · `Y passing` (green).
- **Checks** (`text-accent`) — total count, breakdown: `X failing` (red) · `Y warning` (yellow) · `Z passed` (green). Only shown if checks exist.

**Repository table** — one row per tracked repository:

| Column | Content | Sortable? |
|--------|---------|-----------|
| **Repository** | Name as link to detail page | ✓ (alphabetical) |
| **Issues** | Open count + stale count, e.g., `12 (3 stale)` | ✓ (by open, by stale) |
| **Pull Requests** | Open count + stale count, e.g., `5 (1 stale)` | ✓ (by open, by stale) |
| **Last Activity** | Time since last commit across observed branches, e.g., `3d ago` | ✓ (by last commit time) |
| **Branches** | Total count + stale count with link to GitHub branches page | — |
| **Workflows** | Compact badge grid (see below) | ✓ (by number of failures) |
| **Checks** | Compact badge grid (see below) | ✓ (by number of failures) |

### View Switcher

The dashboard supports three view modes, selectable via a button group next to the sort controls. The user's preference is persisted in `localStorage` (key: `grimoire-view`). Default: Grid.

| View | Partial Endpoint | Description |
|------|-----------------|-------------|
| **Grid** | `GET /partials/dashboard-cards` | Card grid (current default). 3 columns on XL, 2 on MD, 1 on mobile. Best for ≤20 repos. |
| **List** | `GET /partials/dashboard-list` | Expanded one-repo-per-row layout. Each item shows: repo name with stats (issues, PRs, staleness), tracked branches, last activity, GitHub link. Below the header, a two-column grid lists workflows (left) and checks (right) by name with status dot and label. |
| **Table** | `GET /partials/dashboard-table` | Data table with sortable column headers. Maximum density for 50+ repos. Columns: Repo, Issues, PRs, Workflows, Last Activity, Branches. |

**Extensibility:** Each view is a self-contained Jinja2 partial template that controls its own layout. The `#repo-grid` container is layout-agnostic. Adding a new view requires: (1) creating a partial template, (2) adding a router endpoint, (3) registering the view in the `VIEW_PARTIALS` JS object in `dashboard.html`.

All three views accept the same `sort` and `dir` query parameters. The table view additionally supports clickable column headers for sorting (HTMX `hx-get` on `<th>` elements).

**Health status display toggle:** A ⚙ gear-icon dropdown sits next to the view switcher with two checkboxes — "Check results" and "Stale issues/PRs" — controlling whether each contributes to the computed `health_status` (see below). Both default on. Workflow failures always count and are not toggleable. State is persisted per-browser in `localStorage` (`grimoire-health-include-checks`, `grimoire-health-include-stale`); toggling re-fetches both `#repo-grid` and the stats bar (`#stats-bar`, via `GET /partials/dashboard-stats`) so the accent colors, status icons, and the "Repositories" stat panel's failing/warning/healthy breakdown stay consistent. The underlying issue/PR/check/workflow counts themselves are unaffected — only the derived health status.

**Staleness highlighting:** Stale issue/PR counts are highlighted in yellow only when the stale percentage (stale/open) meets or exceeds the configured thresholds (`staleness.problematic_stale_issues_pct`, `staleness.problematic_stale_prs_pct`). Below the threshold, stale counts render without warning color.

### Health Status & Accent Colors

Every repo has a computed `health_status` that drives a left-border accent color across **all views** (matrix, list):

| Status | Accent | Condition |
|--------|--------|-----------|
| **Error** | Red (`border-error`) | At least one workflow failure OR at least one error-severity check failure |
| **Warning** | Yellow (`border-warning`) | No errors, but stale issues/PRs exist |
| **OK** | None (no border) | All workflows and checks pass, no staleness |

Error takes priority over warning. The `severity` field on check definitions (`"error"` or `"warning"`, default `"error"`) controls whether a failing check affects health. Only `"error"`-severity failures contribute to the error tier; `"warning"`-severity failures are reported but do not influence repo health. See Module 4 for details.

Check-result and staleness contributions can each be individually excluded from this computation via the health status display toggle described above (default: both included). Workflow failures always count toward the error tier regardless of toggle state.

**Per-repo warnings:** If a repo has warnings (e.g., "Data is 2h stale"), show an amber ⚠ icon in the row. Hover/click reveals the warning text.

### Workflow visualization (compact)

For each repo, show workflow statuses as FontAwesome icons with semantic colors:

```
✓ ✗ ◷      (single branch — just icons)

main:    ✓ ✓ ✗
develop: ✓ ✓        (multi-branch — grouped with labels)
```

Icons (FontAwesome 6):
- `fa-check` (green `#22c55e`) — success
- `fa-xmark` (red `#ef4444`) — failure
- `fa-clock` (yellow `#eab308`) — pending
- `fa-minus` (gray `#6b7280`) — unknown

Each icon represents one workflow. Hover shows the workflow name. Click links to the GitHub run.

When a repo has a single observed branch, omit the branch label for compactness.

### Workflow groups (columns)

`dashboard.workflow_groups` (spec 1.2) splits workflows into named columns. The grouping is computed at render time by `group_workflows()` in `web/router.py` (no data model or DB change); `RepoViewModel.workflow_columns` holds one `WorkflowColumn` per column.

- No groups configured: a single "Workflows" column (default behaviour).
- Groups configured: one column per group, in config order, plus a trailing "Other" column unless `show_other` is false (then unmatched workflows are hidden). The first group whose glob matches the workflow name wins (case-sensitive, `fnmatchcase`). Hidden workflows still count toward repo health status.
- Matrix view: one `<th>`/`<td>` per column; every column is always rendered so cells align across repos.
- List view and repo detail page: one labelled workflow block per non-empty column (the label is omitted on the repo page when there is a single column).

### Check visualization (compact)

Same visual style as workflows: colored FontAwesome icons, one per applicable check.

Icons:
- `fa-check` (green) — pass
- `fa-xmark` (red) — fail
- `fa-minus` (gray) — not yet run

Hover shows the check name + description. For multi-branch repos, show per-branch results grouped with branch labels (same pattern as workflows).

### Sorting (HTMX)

Clicking a column header triggers an HTMX request that re-renders just the table body:

```html
<th hx-get="/partials/dashboard-table?sort=issues&dir=desc"
    hx-target="#repo-table-body"
    hx-swap="innerHTML">
  Issues ▼
</th>
```

The server sorts the data and returns the `<tbody>` HTML fragment. No full page reload.

Supported sort keys: `name`, `issues`, `stale_issues`, `prs`, `stale_prs`, `workflow_failures`, `check_failures`, `last_activity`.

### Refresh (HTMX)

The refresh button uses a polling partial pattern (consistent with checks and actions). A new partial template `partials/refresh_button.html` handles two states:

- **Idle:** Shows "↻ Refresh" button. Clicking posts to `POST /partials/refresh-trigger` which starts the refresh as a background task and returns the running-state partial.
- **Running:** Shows a "Refreshing… N/M" counter with a spinner. Polls `GET /partials/refresh-status?was_running=1` every 2 seconds. When the refresh completes, the endpoint sends an `HX-Trigger: refreshCompleted` header, which the dashboard listens for to auto-reload the repo grid (no full page reload).

Next to the refresh button, the configured cron schedule is displayed as muted text: `⏱ Scheduled: */30 * * * *`, matching the style used on checks and actions pages.

```html
{# Idle state #}
<button hx-post="/partials/refresh-trigger"
        hx-target="#refresh-btn"
        hx-swap="outerHTML">
  ↻ Refresh
</button>

{# Running state — polls every 2s #}
<div hx-get="/partials/refresh-status?was_running=1"
     hx-trigger="every 2s"
     hx-swap="outerHTML">
    Refreshing… 3/12
</div>
```

This pattern is shared across all long-running operations:
- **Refresh:** `RefreshProgress` in `github/service.py`, polled via `GET /partials/refresh-status`
- **Checks:** `CheckProgress` in `checks/engine.py`, polled via `GET /partials/check-run-status/{slug}`
- **Actions:** `ActionProgress` in `actions/engine.py`, polled via `GET /partials/action-run-status/{slug}`

## 6.4 — Repository Detail Page

**Route:** `GET /repo/{owner}/{name}`
**Template:** `templates/repository.html`

### Sections

**1. Header**
- Repository full name (e.g., `lucabello/grimoire`).
- Link to GitHub: `https://github.com/{owner}/{name}`.
- Default branch name.
- Observed branches list (plain text, dot-separated, matching stats bar style).
- Warning banner if any warnings exist.

**2. Compact Stats Bar**
- Single horizontal row: `(icon) X issues (Y stale) · (icon) X PRs (Y stale) · (icon) N branches · (icon) Last activity: …`
- Font-awesome icons: `fa-circle-dot` (issues), `fa-code-pull-request` (PRs), `fa-code-branch` (branches), `fa-clock` (last activity).
- Issues count links to `https://github.com/{owner}/{name}/issues`, PRs count links to `.../pulls`, branches count links to `.../branches`.
- Stale counts are not linked.
- Stale counts color-coded with `text-warning` when stale/open ≥ configured threshold.
- No card/shadow styling — sits flat on the page background.
- Last activity is right-aligned.

**3. Stale Issues Table**
- Header shows icon-based indicators: `(fa-circle-dot) N total · (fa-clock) N stale (X%)`, with stale count highlighted in warning color when above threshold.
- Table columns:

| # | Title | Author | Last Activity |
|---|-------|--------|---------------|

**4. Stale PRs Table**
- Same header format as stale issues: `(fa-code-pull-request) N total · (fa-clock) N stale (X%)`.
- Table columns:

| # | Title | Author | Last Activity |
|---|-------|--------|---------------|

**5. Workflows & Checks (two-column card)**
- Combined into a single card with a two-column grid layout (workflows left, checks right).
- Each column has a header with a breakdown summary: `X passing · Y warning/pending · Z failing` (success → warnings → failures order), using status icons.
- Flat list per column — one row per item with status icon, name, status text, and link.
- Branch labels shown only when multiple branches are tracked.
- Check rows include an "Output" button (HTMX `hx-get` to fetch full output on demand).

## 6.5 — Actions Page

**Route:** `GET /actions`
**Template:** `templates/actions.html`

### Layout

The actions page follows the same vertical-list layout as the checks page.

**Section 1: Available Actions**

One action per row in a vertical list. Each row contains:

| Element | Position | Style |
|---------|----------|-------|
| **Name** | Left, bold | Semibold text |
| **Description** | Below name | Small muted text |
| **Target summary** | Below description, inline | Monospace, muted (e.g., `regex: .*`, `list: 3 repos`) |
| **Result summary** | Inline after target | Green ✓ / red ✗ counts + last run time |
| **Schedule** | Right-aligned | Small muted text: `⏱ Scheduled: cron` or `⏱ Scheduled: manual` |
| **Run button** | Far right | Primary button with play icon |

**Expandable sections** below each row:
- **Script** — toggle button reveals a collapsible `<pre><code class="language-bash">` block with highlight.js syntax highlighting.
- **Results** — toggle button loads per-action results inline via HTMX (`GET /partials/action-results/{slug}`). Results are grouped by **run** (most recent first), each shown as a collapsible `<details>` element displaying: trigger type badge (manual/cron), timestamp, pass/fail summary. Expanding a run reveals the per-repo result table with status icons and per-row output expansion via `GET /partials/action-output/{result_id}`.

Up to 10 recent completed runs are shown.

The "Run" button triggers the action via HTMX and reloads the page after 1.5s:

```html
<button hx-post="/api/actions/{slug}/run"
        hx-swap="none"
        hx-on::after-request="if(event.detail.successful) { showToast('Action triggered', 'success'); setTimeout(() => location.reload(), 1500); }">
  Run
</button>
```

## 6.6 — Checks Page

**Route:** `GET /checks`
**Template:** `templates/checks.html`

### Layout

One check per row in a vertical list (similar to dashboard List view). Each row contains:

| Element | Position | Style |
|---------|----------|-------|
| **Name** | Left, bold | Semibold text |
| **Severity** | Inline after name | Muted text: `· error` or `· warning` |
| **Description** | Below name | Small muted text |
| **Target summary** | Below description, inline | Monospace, muted (e.g., `regex: .*`, `list: 3 repos`) |
| **Result summary** | Inline after target | Green ✓ / red ✗ counts + last run time |
| **Schedule** | Right-aligned | Small muted text: `⏱ Scheduled: cron` or `⏱ Scheduled: default` |
| **Enabled toggle** | Right side | Swap toggle (● / ○) |
| **Run button** | Far right | Primary button with play icon |

Disabled checks are visually dimmed (reduced opacity) and show `· disabled` inline.

**Expandable sections** below each row:
- **Script** — toggle button reveals a collapsible `<pre><code class="language-bash">` block with highlight.js syntax highlighting (loaded from CDN on the checks page only).
- **Results** — toggle button loads per-check results inline via HTMX (`GET /partials/check-results/{slug}`). Results are grouped by **run** (most recent first), each shown as a collapsible `<details>` element displaying: trigger type badge (manual/cron/refresh), timestamp, pass/fail summary. Expanding a run reveals the per-repo result table with status icons and per-row output expansion. Up to 10 recent completed runs are shown.

```html
<button hx-post="/api/checks/{slug}/toggle" hx-swap="none" ...>Toggle</button>
<button hx-post="/api/checks/{slug}/run" hx-swap="none" ...>Run</button>
<button hx-get="/partials/check-results/{slug}" hx-target="#check-results-{slug}" ...>Results</button>
```

## 6.7 — Backlog Page

**Route:** `GET /backlog`
**Template:** `templates/backlog.html`
**Engine:** `src/grimoire/web/backlog.py`

The backlog page flattens every problem across all repos into a single prioritized list, answering: "What is the most important thing I should fix right now?"

### Data Model

**`BacklogCategory`** — enum of item types: `FAILING_WORKFLOW`, `FAILING_CHECK_ERROR`, `FAILING_CHECK_WARNING`, `STALE_PR`, `STALE_ISSUE`.

**`BacklogItem`** — dataclass with: `category`, `repo_full_name`, `description`, `url`, `age_days` (optional), `score` (computed), `repo_weight`, `workflow_multiplier`.

**`RepoGroup`** — dataclass for the grouped-by-repository view: `repo_full_name`, `items: list[BacklogItem]`, `total_score: float` (sum of item scores). Properties: `tier` and `tier_class` derived from `total_score` using `PRIORITY_TIERS`.

**`TypeGroup`** — dataclass for the grouped-by-type view: `key` (e.g. `"stale_issues"`, `"workflow:CI"`), `label`, `icon`, `items: list[BacklogItem]`, `total_score: float`. Properties: `tier` and `tier_class` derived from `total_score` using `PRIORITY_TIERS`.

### Priority Scoring

Each item gets a priority score: `score = category_weight × repo_weight × workflow_multiplier × age_factor`.

- **`category_weight`** — base weight from `backlog.category_weights` config.
- **`repo_weight`** — resolved by `resolve_repo_weight(repo_full_name, config)` against `backlog.repository_weights`. Rules are evaluated top-to-bottom, the last match wins, and the default is `1.0` if nothing matches. Set a matching rule to `0.0` to hide a repo from the backlog.
- **`workflow_multiplier`** — per-workflow-name weight from `backlog.workflow_weights` (regex patterns via `re.search()`, default 1.0). Only applies to workflow/check items. First matching pattern wins. Example patterns: `"Release .*"`, `"^(CI|Build)$"`, `"Deploy"`.
- **`age_factor`** — automatic boost for older problems. Stale items use excess age over threshold: `1.0 + log2(1 + max(0, age_days - threshold))`. Workflows/checks use flat factor (1.0) since failure start time is not tracked.
- **`compute_score(category, repo_weight, age_days, reference_days, config, workflow_name="")`** — scoring helper that accepts the resolved repository weight instead of reading per-repo metadata.

**Priority tiers:** score ≥80 = Critical (red), ≥50 = High (orange), ≥20 = Medium (yellow), <20 = Low (gray).

### Layout

**Header:**
- Title: "Backlog"
- Summary: "N items across M repos — X critical, Y high, Z medium, W low"
- Search input: always-visible text box with magnifying-glass icon. Filters items via server-side substring match (case-insensitive) against repo name, description, category label, and branch name. Input is debounced (300 ms) and triggers HTMX partial reload.
- View toggle: flat list / group by repository (DaisyUI `join` button group)
- Export dropdown: "Export All as Markdown" / "Copy to Clipboard"
- Filters toggle

**Filters panel** (collapsible):
- Category toggles (checkboxes per item type)
- Repo filter (multi-select)
- Advanced: category weight sliders (collapsed by default). Changes re-sort live via HTMX partial reload.
  - "Save to config" button persists current weights to `config.yaml` via `POST /api/backlog/save-weights`.
  - `localStorage` saves slider positions automatically.

**Item list** (HTMX partial: `GET /partials/backlog-items`):

- **Flat view** (default): Each row shows: priority badge (color-coded), category icon, repo name (link to detail page), description, age, GitHub link, copy-as-markdown button.
- **Grouped by repo** (`group_by=repo`): Items grouped under collapsible `<details>` sections per repository. Each group header shows: cumulative score badge (Σ), repo name (link to detail page), item count. Items within each group are listed without the repo name column. Groups sorted by cumulative score descending.
- **Grouped by type** (`group_by=type`): Items grouped by category type under collapsible `<details>` sections. Groups appear in this order:
  - **Stale Issues** — all `STALE_ISSUE` items
  - **Stale PRs** — all `STALE_PR` items
  - **Workflows** — all `FAILING_WORKFLOW` items
  - **Checks** — all `FAILING_CHECK_ERROR` and `FAILING_CHECK_WARNING` items
  - **Others** — any items that don't fit the above (future-proofing)

  Each group header shows: cumulative score badge, type icon + label, item count. Items within each group show the repo name since grouping is by type, not repo.

**`group_by_repo(items) -> list[RepoGroup]`** — groups items by `repo_full_name`, computes `total_score` as sum of item scores, sorts groups by `total_score` descending. Items within each group retain their original score-descending order.

**`group_by_type(items) -> list[TypeGroup]`** — groups items by category type (stale issues, stale PRs, workflows, checks, others). Each `TypeGroup` has: `key` (e.g. `"stale_issues"`, `"workflows"`), `label`, `icon`, `items`, `total_score`. Groups appear in a fixed order (not by score). Items within each group retain their original score-descending order.

### Markdown Export

**`GET /api/backlog/export`** — returns full backlog as Markdown (`text/plain`).

Format:
```markdown
# Grimoire Backlog — YYYY-MM-DD

## Critical (N items)
- [ ] **[owner/repo]** Failing workflow: Name on `branch` (Xd) — [View](url)

## High (N items)
- [ ] **[owner/repo]** Stale PR #42: "Title" (Xd) — [View](url)
```

Individual items can be copied via the per-row clipboard button.

### Routes

| Endpoint | Method | Returns |
|----------|--------|---------|
| `GET /backlog` | GET | Full page. Accepts `?group_by=repo` or `?group_by=type` for grouped views. |
| `GET /partials/backlog-items` | GET | HTMX partial (item list). Accepts query params: `category`, `repo`, `group_by` (`repo` or `type`), `search`, weight overrides. |
| `GET /api/backlog/export` | GET | Full backlog as Markdown text |
| `POST /api/backlog/save-weights` | POST | Persists category weight changes to `config.yaml` and reloads in-memory config |

### Item Sources

| Type | Source | Individually listed? |
|------|--------|---------------------|
| Failing workflow | `WorkflowStatus` with `status == "failure"` | ✅ per workflow×branch |
| Failing check (error) | `CheckResultRecord` with `passed == False`, error severity | ✅ per check×repo×branch |
| Failing check (warning) | Same, with `severity == "warning"` | ✅ |
| Stale PR | `PullRequestDetail` from cache | ✅ individual items |
| Stale issue | `IssueDetail` from cache | ✅ individual items |

## 6.8 — Styling

**Framework:** Tailwind CSS (via CDN for development, standalone CLI for production build) + DaisyUI for component classes + FontAwesome 6 for status icons (via CDN).

**Design principles:**
- **Compact and data-dense** — dashboard is a control panel, not a marketing page.
- **Accessible status indicators** — FontAwesome icons (✓ check, ✗ xmark, ◷ clock, — minus) with semantic colors; status is conveyed by both shape and color.
- **Monospace for counts** — numbers in the table use monospace font for alignment.
- **Dark mode** — DaisyUI theme support. Default to system preference.
- **Responsive** — table scrolls horizontally on small screens; key info visible on mobile.

- `backlog.html` extends `base.html`.

**Template hierarchy:**
- `base.html` — HTML shell, nav bar, Tailwind/DaisyUI CDN links, HTMX script tag.
- `dashboard.html` extends `base.html`.
- `repository.html` extends `base.html`.
- `actions.html` extends `base.html`.
- `checks.html` extends `base.html`.
- `maintenance.html` extends `base.html`.

## 6.9 — HTMX Partial Endpoints

These endpoints return HTML fragments (not full pages) for HTMX to swap in:

| Endpoint | Returns |
|----------|---------|
| `GET /partials/dashboard-cards?sort=...&dir=...` | Grid view: card layout |
| `GET /partials/dashboard-list?sort=...&dir=...` | List view: compact rows |
| `GET /partials/dashboard-table?sort=...&dir=...` | Table view: data table |
| `GET /partials/dashboard-stats?include_checks=...&include_stale=...` | Stats bar, respecting the health status display toggle |
| `GET /partials/action-run/{run_id}` | Expanded action run details |
| `GET /partials/action-results/{slug}?sort=...&dir=...` | Per-action results grouped by run (collapsible) |
| `GET /partials/action-output/{result_id}` | Expanded action output text |
| `GET /partials/check-output/{result_id}` | Expanded check output text |
| `GET /partials/check-results/{slug}?sort=...&dir=...` | Per-check results grouped by run (collapsible) |
| `GET /partials/backlog-items` | Filtered/sorted backlog item list |
| `GET /partials/refresh-status` | Refresh button (idle/running with progress) |
| `POST /partials/refresh-trigger` | Starts refresh, returns running-state button |
| `GET /partials/check-run-status/{slug}` | Check run button with progress counter |
| `POST /partials/check-run/{slug}` | Starts check run, returns running-state button |
| `GET /partials/action-run-status/{slug}` | Action run button with progress counter |
| `GET /partials/loading-status` | Loading progress bar; redirects (`HX-Redirect: /`) when done |

## 6.10 — Empty States

Every section must render a helpful message when there's no data:

| Situation | Message |
|-----------|---------|
| No repos configured | "No repositories configured — edit `config.yaml` to get started." |
| First startup, no cache | Dashboard shows repos with "Loading..." spinners; data populates as the first refresh completes. |
| No checks defined | Checks column on dashboard is hidden. Repo detail shows "No checks defined." Checks page shows "No checks configured — add YAML files to `data/checks/`." |
| No check results yet | Checks page result history shows "No check results yet." |
| No actions exist | Actions page shows "No actions defined — add YAML files to `data/actions/`." |
| No action results yet | Per-action "Results" button hidden; inline text shows "no runs yet". |
| Repo has warnings | Amber ⚠ with hover text; never an empty row. |
| No backlog items | Backlog page shows "No issues found — all clear! 🎉" |
| No maintenance groups | Maintenance page shows "No maintenance groups configured" and how to add `maintenance.groups` to `config.yaml`. |
| Group with no tracked repo in scope | The group card says "No tracked repository is in scope for this group yet." and has no link. |

## 6.11 — Maintenance Page and Groups API

**Files:** `src/grimoire/maintenance.py` (builder, state, API router), `src/grimoire/web/router.py` (page), `templates/maintenance.html`
**Config:** `maintenance.groups` (spec 1.2). **Nav entry:** "Maintenance" after "Actions".

Maintenance groups answer "which PRs of kind X are open across my repositories?" (Renovate PRs, PRs by one user, …). Grimoire does **not** store or fetch those PRs: it holds the tracked repository list (including team-resolved repos), so it builds the GitHub search query and a `https://github.com/pulls?q=…` link. No GitHub request is made, no table is added, and the refresh is unchanged.

### Query builder

```python
def build_group_searches(group: MaintenanceGroup, tracked: list[str]) -> list[MaintenanceSearch]
# MaintenanceSearch(name, query, url, part=1, parts=1)
```

A pure function of the group and the tracked repository full names.

| Part of the query | Rule |
|---|---|
| Base | Always `is:pr is:open`. |
| Repositories | The tracked names, sorted; narrowed by the group's `repos` globs. Always emitted as `repo:` qualifiers (`(repo:a OR repo:b …)` for several). No repository in scope → **no search** (a query without `repo:` would search all of GitHub). |
| `authors` | `author:<value>` (`app/<name>` is used as is). |
| `labels` | `label:<value>`. |
| `title_words` | `<word> in:title`; each word carries its own `in:title`. |
| `draft` / `base` / `status` | `draft:true\|false`, `base:<branch>`, `status:<success\|failure\|pending>`. |
| Several values of one matcher | Alternatives: `(x OR y)`, always parenthesised. |
| Several matchers | ANDed (space-separated). |
| Quoting | A value is quoted (`"help wanted"`) unless it only contains word characters, `.`, `/`, `@`, `-`. A value that is `OR`, `AND` or `NOT` in any letter case, or that starts with `-`, is always quoted: bare, it would be a search operator or a negation (see row 12 in the limits table). A value such as `repo:x` contains `:`, so it is quoted and stays a literal term. Values never contain `"` (rejected by config validation). |
| URL | `https://github.com/pulls?q=` + the query URL-encoded (`quote_plus`). |
| Splitting | When the URL would exceed `MAX_URL_LENGTH` (7000), the repositories are split into several searches of the same group (`part`/`parts`); every part keeps all matchers. |

Example, group `renovate` (`authors: [app/renovate, app/dependabot]`, `draft: false`) over `acme/api`, `acme/web`:

```
is:pr is:open (repo:acme/api OR repo:acme/web) (author:app/renovate OR author:app/dependabot) draft:false
```

### Syntax engine

The queries use GitHub's **advanced** issue search syntax (explicit `OR` and parentheses). The API evaluates it when asked: REST `search/issues` with `advanced_search=true` and GraphQL `search(type: ISSUE_ADVANCED)`. The REST default (legacy) engine rejects the parentheses. Single-valued matchers produce plain qualifiers that both engines understand; only a multi-valued matcher (or several repositories) needs the advanced engine.

Whether `https://github.com/pulls?q=…` evaluates the advanced syntax is **unverified**: the page needs a login and could not be exercised, and GitHub's documentation only names the repository issues page and the issues dashboard (row 11). A link with a parenthesised multi-author, multi-label, multi-title-word or multi-repository query must be opened once by the maintainer to confirm the result before relying on it; only a group with single values over one repository has a query without `OR`. The Maintenance page says so next to the links.

### Running a query with the `gh` CLI

Verified on 2026-11-06 with `gh` 2.46.0 (the version installed here) against `canonical/operator` and `canonical/pebble`, read-only. Statements about `gh` 2.103.3 come from a separate check that was not reproduced here.

| Command | Observed |
|---|---|
| `gh api -X GET search/issues -f advanced_search=true -f q="$query"` | Works on 2.46.0 (`(repo:canonical/operator OR repo:canonical/pebble)` = 54 open PRs). Recommended. With `--paginate` (and `-f per_page=100`) it returned the same 54 items. GitHub's documentation limits search to 1000 results and 100 per page (not tested here). |
| `gh search prs "$query"` (whole query as one argument) | Silently returns no results on 2.46.0 (observed) and on 2.103.3 (separate check): the query is sent as one quoted term. |
| `gh search prs -- $query` (query split into words) | 422 "Invalid search query" on 2.46.0 (observed) for a query with `OR`: there is no way to select the advanced engine. Works only on a recent `gh` (2.103.3 by the separate check). |
| `gh pr list -R owner/name --search "$query"` | Per repository only. On 2.46.0 (observed, `canonical/operator`) a parenthesised `OR` of qualifiers returned 0: `(draft:true OR draft:false)` = 0 where `draft:false` = 19 and `draft:true` = 14, and `(author:dimaqq OR author:tonyandrewmeyer)` = 0 where `author:tonyandrewmeyer` = 21. `(fix in:title OR docs in:title)` did return the union (20 = 13 + 7). The parenthesised `OR` is evaluated on 2.103.3 (separate check). Do not rely on it with an older `gh`. |

### Verified GitHub search limits

Checked on 2026-11-06 with the authenticated `gh` CLI (2.46.0) against `search/issues` (read-only, `per_page=1`) and with the GitHub docs. Counts are `total_count`.

| # | Question | Observed |
|---|---|---|
| 1 | Query length / operators, legacy engine | Docs: query text at most 256 characters (operators and qualifiers excluded) and at most five `AND`/`OR`/`NOT` operators; violations are a 422 "Validation failed". |
| 2 | Repeated qualifiers, legacy engine | `repo:A repo:B` = 73 = 37 + 36 and `author:x author:y` = 26 = 23 + 3, so repeated qualifiers are alternatives. `(author:x OR author:y)` is a 422 ("listed users cannot be searched"), and `author:x OR author:y` is a 422 ("logical operators only apply to text, not to qualifiers"). |
| 3 | Repeated qualifiers, advanced engine | `repo:A repo:B` = 0 and `author:x author:y` = 6 (explicit `(author:x OR author:y)` = 20): repeated qualifiers are **not** alternatives, OR must be explicit. |
| 4 | Explicit OR, advanced engine | `(repo:A OR repo:B)` = 73 and `(repo:A OR repo:B) (author:x OR author:y)` = 25: works. |
| 5 | OR without parentheses | `repo:A docs in:title OR spread in:title` = 18811: the `repo:` scope is silently dropped. The builder always parenthesises. |
| 6 | Comma lists | `repo:A,B` and `author:x,y` are a 422. `label:a,b` works (alternatives) but the builder uses `(label:a OR label:b)`. |
| 7 | `in:title` with OR | `(docs OR spread) in:title` = 35 (also matches bodies); `(docs in:title OR spread in:title)` = 11, equal to the count obtained from the PR list. |
| 8 | Many repositories | 67 repositories as `(repo:… OR …)` (66 `OR`s, 2543 characters) is accepted: 713, equal to the sum of its two halves (313 + 400), `incomplete_results: false`. 150 repositories (149 `OR`s, 5536 characters) is accepted. No operator or length limit was hit below the URL limit. |
| 9 | URL length | A request line above ~8.1 KB is a 414 on both `api.github.com` ("Request-URL that is too long", 300 repositories, 11 KB) and `github.com/pulls` (8187 bytes accepted, 8196 refused). 150 repositories already take 6.5 KB; 67 take 2.9 KB. Hence `MAX_URL_LENGTH = 7000` and the split into `parts`. |
| 10 | `org:`/`user:` | Both are accepted (`org:canonical` = `user:canonical` = 10326). They are not used: they would include untracked repositories. |
| 11 | github.com UI | An unauthenticated request to `github.com/pulls?q=…` answers 302 to the login page, so the UI was not exercised. GitHub's [Filtering and searching issues and pull requests](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/filtering-and-searching-issues-and-pull-requests) page says boolean and nested queries work "on your repository's issues page and the issues dashboard"; it does not mention the pull requests page. The `gh` source has TODOs saying support for advanced issue search on the PR tab was not yet confirmed. Behaviour of `/pulls?q=` for parenthesised queries is therefore **unverified**. |
| 12 | Search keywords and `-` in values (advanced engine, `canonical/operator`, 33 open PRs) | `repo:canonical/operator OR in:title` (title word `OR`, bare) = 199,210,519: the top-level `OR` drops the repo scope. Quoted, `repo:canonical/operator "OR" in:title` = 0 and `("OR" in:title OR fix in:title)` = 13 (equal to `fix in:title` alone), so the scope holds. Bare `-docs in:title` = 7, the same as `docs in:title` (not a literal word, not a clean negation); `"-docs" in:title` = 7 as a quoted term. |

Everything the group fields express is expressible: OR within a matcher, AND across matchers, any number of tracked repositories (via splitting).

### API

`GET /api/maintenance/groups` → `list[MaintenanceSearch]`, in configured group order, one item per search:

```json
[{"name": "renovate", "query": "is:pr is:open (repo:acme/api OR repo:acme/web) author:app/renovate", "url": "https://github.com/pulls?q=is%3Apr+is%3Aopen+...", "part": 1, "parts": 1}]
```

Reads the registered config (`set_maintenance_config`) and the tracked repositories (`github.router._repos`) only. A group with no repository in scope has no item. Consumers that run the query themselves must use the advanced engine and run every part of a group; the recommended command is `gh api -X GET search/issues -f advanced_search=true -f q="$query"` (see "Running a query with the `gh` CLI" for what does not work).

### Page

**Route:** `GET /maintenance`. One card per configured group: name, the matchers (list values joined with "or"), the repository scope ("all tracked repositories" or the `repos` globs), and per search the query text plus an "Open on GitHub" button (`target="_blank" rel="noopener noreferrer"`). A split group shows "Part i of n" per search. One line near the links warns that GitHub may not apply the parenthesised syntax on this page and that a multi-value link should be checked once. Empty states: see 6.10.

## Acceptance Criteria

- [ ] Dashboard renders all tracked repos with correct stats
- [ ] Sorting works for all columns (name, issues, stale issues, PRs, stale PRs, workflow failures, check failures)
- [ ] Multi-branch repos show per-branch workflow/check status in a single row
- [ ] Per-repo warnings display as amber indicators with hover text
- [ ] Global warning banner appears when in degraded mode or rate-limited
- [ ] Repository detail page shows stale issues/PRs, workflow table, check results
- [ ] Check output is expandable on the detail page
- [ ] Actions page lists all available actions with run buttons
- [ ] Run history shows all runs with correct metadata
- [ ] Action run details expand inline with per-repo results and logs
- [ ] Backlog page lists all problems sorted by priority score
- [ ] Backlog category weight sliders re-sort items live via HTMX
- [ ] Backlog "Save to config" persists weight changes to config.yaml
- [ ] Backlog export renders correct Markdown with tier grouping
- [ ] Backlog per-item copy-to-clipboard works
- [ ] Backlog filters by category and repo work correctly
- [ ] Maintenance page lists each configured group with matchers, query and a GitHub link opening in a new tab; empty state explains how to configure
- [ ] `GET /api/maintenance/groups` returns name, query and url per search without GitHub calls
- [ ] Generated queries are accepted by GitHub's advanced search (see 6.11 for the verified limits)
- [ ] HTMX partial updates work without full page reloads
- [ ] Pages render correctly in both light and dark mode
- [ ] Pages are responsive (usable on tablet/mobile, scrollable tables)
