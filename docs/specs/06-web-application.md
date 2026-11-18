# Module 6: Web Application (Dashboard, Repo Detail, Actions, Checks, Backlog, PRs Pages)

Build the HTMX-powered web frontend with six pages: dashboard overview, individual repository detail, actions management, checks management, backlog prioritization, and the PRs of configured groups.

**Dependencies:** Modules 1–5 (all backend services).

## 6.1 — Application Wiring

**File:** `src/grimoire/app.py`

Update `create_app()` to:

- Mount all API routers (checks, actions, repos, prs, spaces) and the space cookie middleware (6.12).
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
    await upgrade_database(engine)  # Alembic: create or migrate the schema
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
    app.include_router(prs_router, prefix="/api")
    app.include_router(spaces_router, prefix="/api")
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory="..."), name="static")

    return app
```

### URL prefix (root_path)

The app may be served under a path prefix (ASGI `root_path`, e.g. behind an ingress). Templates must never hard-code absolute URLs:

- Jinja: a context processor on `templates` injects `base_path` (`root_path` without trailing slash). Write `href="{{ base_path }}/repo/..."`, `hx-get="{{ base_path }}/partials/..."`.
- JavaScript: `base.html` defines `const BASE_PATH = {{ base_path | tojson }};` — build URLs as `BASE_PATH + '/partials/...'`.
- Server redirects (e.g. `HX-Redirect`) use `_base_path(request)`.

### Cross-site request protection

**File:** `src/grimoire/security.py` (`is_cross_site`, `cross_site_guard`), registered with `app.middleware("http")` in `create_app()`.

State-changing routes are unauthenticated POSTs (check/action run and toggle, `/partials/*-run*`, `/partials/refresh-trigger`, `/api/backlog/save-weights`, `/api/refresh`). Without a guard, any website that a user of a private (VPN) instance visits can send them a simple cross-site POST and, for example, trigger an action that pushes with the instance token. The middleware answers `403 {"detail": "Cross-site requests are not allowed"}` to `POST`, `PUT`, `PATCH` and `DELETE` requests when:

- `Sec-Fetch-Site` is present and is not `same-origin` or `none` (so `same-site` and `cross-site` are rejected); it decides on its own and does not depend on `Host`; or
- `Sec-Fetch-Site` is absent, `Origin` is present and its host (`netloc`) differs from the request's `Host` header (an opaque `Origin: null` never matches). `X-Forwarded-Host` is not consulted: the app does not trust proxy-supplied hosts. A reverse proxy must therefore pass the original `Host` (the nginx example in the deploy guide does).

Requests with neither header (curl, scripts, other services) and all `GET`/`HEAD`/`OPTIONS` requests are allowed. The app's own HTMX requests are same-origin and pass.

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

### Space filter

The page, `GET /partials/dashboard-matrix`, `/partials/dashboard-list` and `/partials/dashboard-stats` read the HTML space selection (6.12) and `_build_repo_viewmodels(include_checks, include_stale, selection)` applies it:

- Only repos with `selection.includes(repo)` are shown; the stats bar counts only those repos.
- Workflow cells use `selection.workflow_visible(repo, workflow_name)`, so an overlapping repo shows the union of the filters of the selected spaces (only that space's workflows for a single space; every membership for All). Workflow group columns stay global and are not hidden when empty.
- Rows carry no per-row space markers: the space selector and the active-filter bar say what is filtered, and the repository page (6.4) names a repo's spaces.

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

### Spaces line

When spaces are configured, a plain text line under the repository name (`#repo-spaces`) names the repo's spaces in config order (`Spaces: Alpha, Beta`), or says `Not in any space`. It is plain text, not badges, and absent when no spaces are configured.

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

### Space filter (Checks and Actions pages)

The Checks page, the Actions page and their partials (`/partials/check-results/{slug}`, `/partials/action-results/{slug}`, `/partials/action-run/{run_id}`) use the HTML selection (6.12):

- **Listed:** definitions with empty `spaces` (global) or `spaces` intersecting the selection. All lists everything; `_unassigned` alone lists only the global ones.
- **Results:** per-repo results are shown only for repos in the selection (`SpaceSelection.repo_name_filter()`, a predicate on the repo full name). Results of global actions (`GLOBAL_REPO == "(global)"`) are always shown. The pass/fail summary on each card and in each run header is computed over the shown results; a run none of whose repos is in the selection is not listed.
- **Running is unchanged:** a manual or scheduled run uses the definition's `targets` whatever the selection. The run-status partials and run/toggle endpoints do not read the selection.
- The list APIs (`GET /api/checks`, `GET /api/actions`) and `GET /api/checks/{slug}/results` accept `?spaces=` via `api_space_selection` and ignore the cookie.

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
| `GET /api/backlog/export` | GET | Full backlog as Markdown text. Accepts `?categories=`, `?repos=`, `?min_score=` and `?spaces=` (query only, never the cookie) |
| `POST /api/backlog/save-weights` | POST | Persists category weight changes to `config.yaml` and reloads in-memory config |

### Space filter

`build_backlog_items(..., selection)` only collects items of repos with `selection.includes(repo)`; failing-workflow items additionally require `selection.workflow_visible(repo, workflow_name)`. Scoring, weights and ranking are unaffected: the selection only changes which items exist. Rows and repo-group headers carry no per-row space markers.

`GET /backlog` and `GET /partials/backlog-items` use the HTML selection (6.12). The export is called from the page JavaScript and must export what the page shows, but `/api/backlog/export` is an API endpoint that reads `?spaces=` only: the page therefore adds `?spaces=<ids>` (empty for All, so no parameter) to the "Copy as Markdown" request and to the "Download .md file" link, which keeps the export URL reproducible. `POST /api/backlog/save-weights` is unchanged (it rewrites the main config file; `space_files` are never touched).

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
- `prs.html` extends `base.html`; it includes `partials/prs_groups.html`, which the partial endpoint also renders.

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
| `GET /partials/prs?sort=...&ci=...&search=...&group=...` | PR summary chips and group sections; also swaps the "last updated" line and the form's hidden `ci` input out of band |
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
| No PR groups | PRs page shows "No PR groups configured" and a `prs.groups` snippet. |
| PR groups but no data yet | PRs page shows "Waiting for the first refresh" (the refresh button shows the progress; the list loads when it completes). |
| Group with no matching PR | The group section says "No open PRs match this group 🎉". |
| Filters match nothing | The group section says "No PRs match the current filters." |
| CI status could not be fetched | One amber line with the warning count; the rows keep their last known state. |

## 6.11 — PRs Page and API

**Files:** `src/grimoire/prs.py` (matcher, grouping, page filters, state, API router), `src/grimoire/web/router.py` (page and partial), `templates/prs.html`, `templates/partials/prs_groups.html`
**Config:** `prs.groups` (spec 1.2). **Nav entry:** "PRs" after "Actions".

The PRs page answers "which PRs of kind X are open across my repositories, and are they green?" (Renovate and Dependabot PRs, a bot's PRs, one person's PRs, …). Grimoire already fetches the open pull requests of every tracked repository to count the stale ones; it now keeps them (spec 2.2) and applies the groups to them when a page or the API reads the cache. Changing a group needs a restart but no refetch.

### Matching

`matches(group, repo_full_name, pr) -> bool` is a pure function. Values of one list matcher are alternatives, different matchers are ANDed, and a group matches only the PRs of the repositories in its scope.

| Matcher | Rule |
|---|---|
| `repos` | `fnmatchcase` on the tracked full name; empty = every tracked repository. |
| `authors` | Both sides are normalised: lower-case, a leading `app/` removed, a trailing `[bot]` removed. `app/renovate` therefore matches the login `renovate[bot]` that the REST pulls list reports, and `observability-noctua-bot` (a regular user account) matches itself. |
| `labels` | Case-insensitive exact label name. |
| `title_words` | Case-insensitive substring of the title (GitHub's search matched words; this is a deliberate change). |
| `draft` | Boolean equality. |
| `base` | Equality with the PR's base branch. |

A PR that matches several groups appears in each of them (workflow groups on the dashboard, where the first match wins, are a different feature).

Real payloads checked on 2026-11-06 against `canonical/*` repositories: the pulls list reports `dependabot[bot]` and `renovate[bot]` (`user.type` is `Bot`) and `observability-noctua-bot` as `user.type` `User`.

### CI, review and merge state

The pulls list has no CI state. For the PRs that match a group, `refresh_all_stats` asks GraphQL after the per-repository fetches (spec 2.1: `get_pull_request_rollups`, batches of 20, sequential). With no groups configured no GraphQL request is made. A GraphQL failure leaves the list intact: the PRs keep their previous state when their head commit is unchanged, have none otherwise, and the repository gets a `Could not fetch CI status for …` warning. The CI badge shows the GitHub rollup, so a Renovate PR waiting out `renovate/stability-days` shows as pending until the delay passes.

### Page

**Route:** `GET /prs?sort=&ci=&search=&group=` (unknown values fall back to the defaults). Query values only filter what is displayed.

- **Header:** "PRs", "Last updated X ago" and the refresh button (`partials/refresh_button.html`); the list reloads on `refreshCompleted`.
- **Controls:** a debounced search box (repository, title or author substring), a sort select and, with several groups, a group select. They form one HTMX form that swaps `#prs-content` from `GET /partials/prs`.
- **Summary chips:** total, failing, pending, passing, no checks (and unknown, when some PRs have no CI data), counted over the distinct PRs of the shown groups after search and group selection. A chip sets the `ci` filter; clicking the active chip clears it.
- **Group sections:** one `<details open>` per group in config order. The summary shows the name and "N PRs in M repos · X failing · Y pending".
- **Row:** CI badge (check, xmark, clock or minus icon linking to `{pr}/checks`, tooltip "CI … (as of …)"; a failing row has a left `border-error` accent) · repository (links to `/repo/{owner}/{name}`) · `#number title` opening GitHub in a new tab (`rel="noopener noreferrer"`) with draft, auto-merge and label badges, the head branch and the base branch when it differs from the repository default · author (normalised, robot icon when the login ends in `[bot]`) · review and merge badges · age (created, absolute time in a tooltip) · last update.
- **Review and merge badges:** `approved`, `changes requested`, `review required`; merge `conflicts` (`dirty`), `behind`, `blocked` (hidden when the review state already explains it) and `ready` (`clean`). A merge state of `unknown` shows nothing.
- **Sorting:** `attention` (default: failure, pending, no checks or unknown, then success; oldest first within each), `repo`, `age` (oldest first), `updated` (most recent first).
- **Tables** scroll horizontally on small screens. Empty states: see 6.10.

### Space filter

`GET /prs` and `GET /partials/prs` use the HTML selection (6.12). `stats_in_selection(stats, repos, selection)` (`prs.py`) keeps the stats of repos with `selection.includes(repo)` before `build_group_results`, so a PR is shown only if its repo is in the selection and the group matchers (including a group's `repos` scope) still apply on top. PR groups themselves stay global.

- Under a narrower selection, groups left with no PRs are hidden, including from the group select; when none is left the page says "No open PRs in the selected spaces match any group". Under All, an empty group still says "No open PRs match this group 🎉". The "No PR groups configured" screen depends on the configuration only (`has_groups`).
- Rows carry no per-row space markers.

### API

`GET /api/prs` reads the in-memory cache only (no GitHub calls) and returns the groups in config order. The optional `?spaces=a,b` (`api_space_selection`, never the cookie) keeps only the PRs of repos in those spaces; every group is still returned, with `total: 0` and an empty `pull_requests` list when nothing is left:

```json
{
  "last_refresh": "2026-11-06T21:05:00Z",
  "groups": [{
    "name": "Maintenance",
    "total": 12, "failing": 3, "pending": 1, "passing": 7, "no_checks": 1,
    "pull_requests": [{
      "repo": "canonical/operator", "number": 12, "title": "…", "url": "https://github.com/canonical/operator/pull/12",
      "author": "renovate[bot]", "created_at": "…", "updated_at": "…", "draft": false, "labels": ["dependencies"],
      "base": "main", "head_ref": "renovate/…", "head_sha": "…", "auto_merge": false,
      "ci": "failure", "ci_url": "https://github.com/canonical/operator/pull/12/checks",
      "review_decision": "review_required", "merge_state": "blocked", "ci_checked_at": "…"
    }]
  }]
}
```

`author` is the raw login. `ci` (`success`, `failure`, `pending`, `none`), `review_decision`, `merge_state` and `ci_checked_at` are `null` when GraphQL has not reported them. A PR matching several groups appears in each. There are no query filters: consumers filter the list themselves.

## 6.12 — Space selection (HTML)

**Files:** `src/grimoire/web/spaces.py`, `src/grimoire/spaces.py` (selection model, spec 1.3), context processor in `web/router.py`, middleware registered in `app.py`.

HTML pages and HTMX partials filter by the user's space selection; the JSON API does not read it (`?spaces=` only, spec 1.3).

- `html_space_selection(request) -> SpaceSelection` (dependency used by page routes): `?spaces=a,b` beats the cookie `grimoire_spaces` (comma-separated ids). Unknown ids are dropped; an empty selection or every space selected is All.
- A request with a `?spaces=` parameter also saves the normalised selection in the cookie `grimoire_spaces` (`Max-Age` one year, `SameSite=Lax`, `Path` = the app `root_path` or `/`; not `HttpOnly`, the navbar script writes it too). All is saved as an empty value. FastAPI does not merge cookies set through a dependency into a directly returned `TemplateResponse`, so the dependency only records the value on `request.state` and `space_cookie_middleware` appends the `Set-Cookie` header to the response. The selection is resolved once per request.
- The context processor on `templates` gives every template `spaces` (the shape of `GET /api/spaces`: `id`, `name`, `description`, `repo_count`; empty when no spaces are configured), `space_selection` (a `SpaceSelection`; `.ids` is `None` for All) and `space_filter` (data of the active-filter bar, `None` under All).
### Navbar selector and badges

**Files:** `templates/base.html` (includes the selector inside the left navbar block, next to the logo), `templates/partials/space_selector.html`, `templates/partials/_space_macros.html`, `templates/repository.html`.

- Hidden entirely when `spaces` is empty (installs without spaces see no change).
- **Markup.** A DaisyUI `<details class="dropdown">` whose `<summary>` is the button. This replaced a focus-based `div[tabindex][role=button]` wrapper (with `shrink-0` and a `data-cookie-path` attribute) that one user's browser did not render at all; the wrapper must not carry `shrink-0` or a `data-cookie-path` attribute. The cookie path comes from the `BASE_PATH` constant defined in `base.html`.
- **Button.** Outlined (not ghost), with a layer icon, never wrapped (`whitespace-nowrap`); the navbar wraps (`flex-wrap`) so on narrow screens the nav links move to a second row. The label is **"Spaces"** under All, the **space's name** when exactly one is selected and **"Spaces (N)"** for several; the tooltip lists the selected names. It follows the checkboxes live, before the page reloads; server rendering and script use the same rules.
- **No colour.** Spaces are told apart by their name only (colour is not an accessible channel on its own), so there is no `color` setting and no colour dots anywhere. The Unassigned row is set apart by italics. Secondary text (counts, hints) keeps at least `opacity-80` so it stays legible.
- **Dropdown.** A search box ("Search spaces", filters the rows by name; All / None and the selection still apply to every space; "No matching spaces" when nothing matches); header "Spaces" with **All** / **None**; one row per space with checkbox, name, the repo count spelled out ("3 repos", "1 repo") and an always-visible **only** action. The name is part of the checkbox's `<label>`: clicking it toggles the box exactly like clicking the box. Only **only** selects a single space; the Unassigned row (dashed dot, muted) only when `GET /api/spaces` offers it; footer hint "saved in this browser". It closes on a click outside or Escape.
- **Applying.** **All**, **None** and **only** are decisive: they write the cookie and reload at once. Ticking boxes accumulates and applies when the dropdown closes. None selects nothing, which means All. The cookie is written from JavaScript with the attributes the server uses (`Path` = `BASE_PATH` or `/`, `Max-Age` one year, `SameSite=Lax`; nothing or every row checked is an empty value), and the page reloads so every page and HTMX partial picks it up. The reload drops `?spaces=` from the URL because it would override the cookie.
- **No badges or chips per row.** The dashboard, backlog and PRs rows carry no space markers (they were noise). The repository page (6.4) shows a plain text line under the title instead: `Spaces: Alpha, Beta` (config order), or `Not in any space` for an unassigned repo; it is absent when no spaces are configured.

### Active-filter bar

**File:** `templates/partials/space_filter_bar.html`, included by `base.html` under the navbar; data from `space_filter_bar()` in `web/spaces.py`.

Shown only while a filter is active (nothing is shown under All). It says **what** is filtered and how to undo it: "Showing" followed by one removable chip per selected space, "· N of M repositories" (the repos in the selection out of all tracked repos, from `count_repos`), and a "Show all spaces" link. Every control is a plain link to `?spaces=`: a chip's link lists the other selected ids (so removing the last one leaves an empty value, which means All) and "Show all spaces" links to `?spaces=`. The server applies and saves the new selection through the usual `?spaces=` handling, so the bar needs no script and works by keyboard; each chip link has an `aria-label` ("Remove <name> from the filter"). The links are relative to the current page, so other query parameters of the page are dropped.

- Page tasks filter with `space_selection.includes(repo)` and `space_selection.workflow_visible(repo, workflow_name)`.

## Acceptance Criteria

- [ ] Dashboard renders all tracked repos with correct stats
- [ ] The dashboard, its partials and the stats bar show only the repos of the space selection; an overlapping repo shows the union of the selected spaces' workflows
- [ ] Sorting works for all columns (name, issues, stale issues, PRs, stale PRs, workflow failures, check failures)
- [ ] Multi-branch repos show per-branch workflow/check status in a single row
- [ ] Per-repo warnings display as amber indicators with hover text
- [ ] Global warning banner appears when in degraded mode or rate-limited
- [ ] Repository detail page shows stale issues/PRs, workflow table, check results
- [ ] Check output is expandable on the detail page
- [ ] Actions page lists all available actions with run buttons
- [ ] Checks and Actions pages list global definitions plus those of the selected spaces, show results and summary counts only for the selected repos, and a manual run under a narrow selection still runs on all targets; the APIs ignore the cookie
- [ ] Run history shows all runs with correct metadata
- [ ] Action run details expand inline with per-repo results and logs
- [ ] Backlog page lists all problems sorted by priority score
- [ ] Backlog category weight sliders re-sort items live via HTMX
- [ ] Backlog "Save to config" persists weight changes to config.yaml
- [ ] Backlog export renders correct Markdown with tier grouping
- [ ] Backlog per-item copy-to-clipboard works
- [ ] Backlog filters by category and repo work correctly
- [ ] The backlog page, partial and export only list items of the selected spaces' repos (and only the workflows the selected spaces allow); scores are unchanged; the export honours `?spaces=` and ignores the cookie
- [ ] A group with `authors: ["app/renovate", "app/dependabot", "observability-noctua-bot"]` matches the PRs of `renovate[bot]`, `dependabot[bot]` and `observability-noctua-bot` and no others
- [ ] PRs page shows one section per group with CI badge, repository, linked title, labels, draft and auto-merge badges, author, review/merge badges, age and last update; chips, search, `ci` filter, sort and group selection work through HTMX; all empty states render
- [ ] The PRs page and partial show only the PRs of the selected spaces' repos and hide groups left empty; `GET /api/prs?spaces=` filters the PRs, keeps every group and ignores the cookie
- [ ] `GET /api/prs` returns the groups with their PRs without GitHub calls; `/maintenance` and `/api/maintenance/groups` are gone
- [ ] `?spaces=` beats the `grimoire_spaces` cookie on HTML pages and is saved to it (Max-Age 1 year, SameSite=Lax, Path = root_path); unknown ids are dropped; `/api/*` ignores the cookie; `GET /api/spaces` lists the spaces, plus Unassigned only when unassigned repos exist
- [ ] The navbar selector is absent without spaces; the button reads "Spaces" / the name / "Spaces (N)"; no colour is used for spaces; the active-filter bar appears only while filtering, lists the selected spaces with removable chips and "N of M repositories", and its links apply the new selection; only, All and None work; the selection survives reload and browser restart; it works under `--root-path`
- [ ] A cross-site `POST` (foreign `Origin`, or `Sec-Fetch-Site: cross-site`/`same-site`) to any state-changing route is rejected with 403 and changes nothing; same-origin, `Sec-Fetch-Site: none` and header-less requests are allowed; `GET` is unaffected
- [ ] HTMX partial updates work without full page reloads
- [ ] Pages render correctly in both light and dark mode
- [ ] Pages are responsive (usable on tablet/mobile, scrollable tables)
