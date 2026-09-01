# Module 6: Web Application (Dashboard, Repo Detail, Actions, Checks, Backlog Pages)

Build the HTMX-powered web frontend with five pages: dashboard overview, individual repository detail, actions management, checks management, and backlog prioritization.

**Dependencies:** Modules 1–5 (all backend services).

## 6.1 — Application Wiring

**File:** `src/grimoire/app.py`

Update `create_app()` to:

- Mount all API routers (checks, actions, repos).
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
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory="..."), name="static")

    return app
```

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
- [ ] HTMX partial updates work without full page reloads
- [ ] Pages render correctly in both light and dark mode
- [ ] Pages are responsive (usable on tablet/mobile, scrollable tables)
