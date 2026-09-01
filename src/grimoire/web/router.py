"""Web page routes and HTMX partials for the Grimoire dashboard."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from grimoire.config import StalenessConfig
from grimoire.database import ActionRunRecord
from grimoire.models import TrackedRepository, WorkflowStatus
from grimoire.targeting import TargetSpec
from grimoire.web.backlog import (
    BacklogItem,
    build_backlog_items,
    export_markdown,
    group_by_repo,
    group_by_type,
)

router = APIRouter(tags=["web"])

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["VERSION"] = version("grimoire-dashboard")

# Module-level staleness config — set from app lifespan
_staleness_config: StalenessConfig = StalenessConfig()


def set_staleness_config(config: StalenessConfig) -> None:
    """Register the staleness thresholds from the app config."""
    global _staleness_config  # noqa: PLW0603
    _staleness_config = config


# Module-level backlog config — set from app lifespan
from grimoire.config import BacklogConfig  # noqa: E402

_backlog_config: BacklogConfig = BacklogConfig()
_config_path: Path | None = None


def set_backlog_config(config: BacklogConfig, config_path: Path | None = None) -> None:
    """Register the backlog configuration from the app config."""
    global _backlog_config, _config_path  # noqa: PLW0603
    _backlog_config = config
    _config_path = config_path


# Module-level refresh schedule — set from app lifespan
_refresh_schedule: str = "*/5 * * * *"


def set_refresh_schedule(schedule: str) -> None:
    """Register the refresh cron schedule from the app config."""
    global _refresh_schedule  # noqa: PLW0603
    _refresh_schedule = schedule


# ---------------------------------------------------------------------------
# View-model dataclass for template rendering
# ---------------------------------------------------------------------------


@dataclass
class RepoViewModel:
    """Prepared data for rendering a single repo card in the dashboard."""

    full_name: str
    branches: list[str]
    source: str
    open_issues: int
    stale_issues: int
    open_prs: int
    stale_prs: int
    workflow_failures: int
    check_failures: int
    check_warnings: int
    warnings: list[str]
    workflows_by_branch: dict[str, list[WorkflowStatus]]
    checks_by_branch: dict[str, list[dict[str, Any]]]
    fetched_at: datetime | None = None
    last_commit_at: datetime | None = None
    total_branches: int = 0
    include_checks: bool = True
    include_stale: bool = True

    @property
    def has_problems(self) -> bool:
        return bool(self.workflow_failures or self.check_failures or self.warnings)

    @property
    def health_status(self) -> str:
        """Three-tier health: 'error', 'warning', or 'ok'.

        Only error-severity check failures affect health, and only when
        `include_checks` is enabled. Warning-severity failures never
        influence status. Staleness only affects health when `include_stale`
        is enabled. Workflow failures always count.
        """
        if self.workflow_failures or (self.include_checks and self.check_failures):
            return "error"
        if self.include_stale and (self.stale_issues or self.stale_prs):
            return "warning"
        return "ok"

    @property
    def total_workflows(self) -> int:
        return sum(len(wfs) for wfs in self.workflows_by_branch.values())

    @property
    def total_checks(self) -> int:
        return sum(len(chks) for chks in self.checks_by_branch.values())

    @property
    def owner(self) -> str:
        return self.full_name.split("/")[0]

    @property
    def name(self) -> str:
        return self.full_name.split("/", 1)[1]


@dataclass
class DashboardTotals:
    """Aggregate stats for the dashboard stats bar."""

    repos: int = 0
    repos_ok: int = 0
    repos_warning: int = 0
    repos_error: int = 0
    open_issues: int = 0
    open_prs: int = 0
    stale_issues: int = 0
    stale_prs: int = 0
    workflow_failures: int = 0
    total_workflows: int = 0
    check_failures: int = 0
    check_warnings: int = 0
    total_checks: int = 0


@dataclass
class ActionViewModel:
    """Prepared data for rendering an action on the actions page."""

    name: str
    slug: str
    description: str
    schedule: str | None
    enabled: bool = True
    target_summary: str = ""
    script: str = ""
    pass_count: int = 0
    fail_count: int = 0
    last_run: datetime | str | None = None


@dataclass
class ActionRunViewModel:
    """Prepared data for rendering an action run row."""

    id: int
    action_slug: str
    action_name: str
    triggered_by: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    total_repos: int
    passed_repos: int
    results: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CheckViewModel:
    """Prepared data for rendering a check definition on the checks page."""

    name: str
    slug: str
    description: str
    schedule: str | None
    enabled: bool
    target_summary: str
    script: str
    severity: str = "error"
    pass_count: int = 0
    fail_count: int = 0
    warn_count: int = 0
    last_run: datetime | str | None = None


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------

SORT_KEYS: dict[str, Any] = {
    "name": lambda r: r.full_name.lower(),
    "health": lambda r: {"error": 0, "warning": 1, "ok": 2}.get(r.health_status, 2),
    "issues": lambda r: r.open_issues,
    "stale_issues": lambda r: r.stale_issues,
    "prs": lambda r: r.open_prs,
    "stale_prs": lambda r: r.stale_prs,
    "branches": lambda r: r.total_branches,
    "workflow_failures": lambda r: r.workflow_failures,
    "check_failures": lambda r: r.check_failures,
    "last_activity": lambda r: r.last_commit_at or datetime.min.replace(tzinfo=timezone.utc),
}

SORT_LABELS: dict[str, str] = {
    "name": "Name",
    "health": "Health",
    "issues": "Open Issues",
    "stale_issues": "Stale Issues",
    "prs": "Open PRs",
    "stale_prs": "Stale PRs",
    "branches": "Branches",
    "workflow_failures": "Failing Workflows",
    "check_failures": "Failing Checks",
    "last_activity": "Last Activity",
}


def _sort_repos(repos: list[RepoViewModel], sort: str, direction: str) -> list[RepoViewModel]:
    key_fn = SORT_KEYS.get(sort, SORT_KEYS["name"])
    return sorted(repos, key=key_fn, reverse=(direction == "desc"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _time_ago(dt: datetime | str) -> str:
    """Return a human-readable 'X ago' string."""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    now = datetime.now(tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _compute_totals(repos: list[RepoViewModel]) -> DashboardTotals:
    totals = DashboardTotals(repos=len(repos))
    for r in repos:
        # Health distribution
        status = r.health_status
        if status == "error":
            totals.repos_error += 1
        elif status == "warning":
            totals.repos_warning += 1
        else:
            totals.repos_ok += 1
        totals.open_issues += r.open_issues
        totals.open_prs += r.open_prs
        totals.stale_issues += r.stale_issues
        totals.stale_prs += r.stale_prs
        totals.workflow_failures += r.workflow_failures
        totals.total_workflows += r.total_workflows
        totals.check_failures += r.check_failures
        totals.check_warnings += r.check_warnings
        totals.total_checks += r.total_checks
    return totals


def _resolve_targets_sync(
    targets: TargetSpec,
    repos: dict[str, TrackedRepository],
) -> set[tuple[str, str]]:
    """Resolve list/regex targeting to a set of ``(repo, branch)`` pairs.

    Script-based targeting is skipped (it depends on workdir state); those
    checks only surface for a branch once a result has been recorded for it.
    """
    if targets.list is not None:
        allowed = set(targets.list)
        return {
            (r.full_name, branch)
            for r in repos.values()
            if r.full_name in allowed
            for branch in (r.branches or [r.default_branch])
        }
    if targets.regex is not None:
        pattern = re.compile(targets.regex)
        return {
            (r.full_name, branch)
            for r in repos.values()
            if pattern.search(r.full_name)
            for branch in (r.branches or [r.default_branch])
        }
    return set()


_LATEST_RESULTS_SQL = text(
    "SELECT cr.id, cr.check_name, cr.check_slug, cr.repo_full_name, cr.branch, "
    "cr.passed, cr.timestamp "
    "FROM check_result cr "
    "INNER JOIN ("
    "  SELECT check_slug, repo_full_name, branch, MAX(timestamp) AS max_ts "
    "  FROM check_result "
    "  GROUP BY check_slug, repo_full_name, branch"
    ") latest ON cr.check_slug = latest.check_slug "
    "AND cr.repo_full_name = latest.repo_full_name "
    "AND cr.branch = latest.branch "
    "AND cr.timestamp = latest.max_ts"
)


async def _load_check_context(
    repos: dict[str, TrackedRepository],
) -> tuple[dict[str, set[tuple[str, str]]], dict[tuple[str, str, str], dict[str, Any]]]:
    """Load check targeting and latest DB results.

    Returns (check_targets, results_by_key) where:
    - check_targets: check_slug → set of (repo_full_name, branch) pairs the check applies to
    - results_by_key: (check_slug, repo_full_name, branch) → result dict
    """
    from grimoire.checks.router import _checks
    from grimoire.checks.router import _engine as _checks_engine

    check_targets: dict[str, set[tuple[str, str]]] = {}
    for check_def in _checks:
        if not check_def.enabled:
            continue
        check_targets[check_def.slug] = _resolve_targets_sync(check_def.targets, repos)

    results_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    if _checks_engine is not None and _checks:
        async with AsyncSession(_checks_engine) as session:
            result = await session.execute(_LATEST_RESULTS_SQL)
            rows = result.all()
        for row in rows:
            key = (row[2], row[3], row[4])  # check_slug, repo_full_name, branch
            results_by_key[key] = {
                "id": row[0],
                "check_name": row[1],
                "check_slug": row[2],
                "passed": bool(row[5]),
                "timestamp": row[6],
            }

    return check_targets, results_by_key


def _build_checks_for_repo(
    full_name: str,
    branches: list[str],
    check_targets: dict[str, set[tuple[str, str]]],
    results_by_key: dict[tuple[str, str, str], dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], int, int]:
    """Build checks_by_branch and count failures/warnings for a single repo.

    Returns (checks_by_branch, check_failures, check_warnings) where
    check_failures counts error-severity failures and check_warnings counts
    warning-severity failures.
    """
    from grimoire.checks.router import _checks

    checks_by_branch: dict[str, list[dict[str, Any]]] = {}
    check_failures = 0
    check_warnings = 0
    for check_def in _checks:
        if not check_def.enabled:
            continue
        targets = check_targets.get(check_def.slug, set())
        for branch in branches:
            applies = (full_name, branch) in targets
            key = (check_def.slug, full_name, branch)
            result = results_by_key.get(key)
            if result:
                entry = {
                    "check_name": check_def.name,
                    "check_slug": check_def.slug,
                    "passed": result["passed"],
                    "status": "pass" if result["passed"] else "fail",
                    "severity": check_def.severity,
                    "id": result["id"],
                    "timestamp": result["timestamp"],
                }
                if not result["passed"]:
                    if check_def.severity == "error":
                        check_failures += 1
                    else:
                        check_warnings += 1
            elif applies:
                entry = {
                    "check_name": check_def.name,
                    "check_slug": check_def.slug,
                    "passed": None,
                    "status": "not-run",
                    "severity": check_def.severity,
                    "id": None,
                    "timestamp": None,
                }
            else:
                continue
            checks_by_branch.setdefault(branch, []).append(entry)
    return checks_by_branch, check_failures, check_warnings


async def _build_repo_viewmodels(
    include_checks: bool = True, include_stale: bool = True
) -> list[RepoViewModel]:
    """Build view models from the in-memory GitHub cache + checks state."""
    from grimoire.github.router import _cache, _repos

    check_targets, results_by_key = await _load_check_context(_repos)

    viewmodels: list[RepoViewModel] = []
    for full_name, stats in _cache.items():
        repo = _repos.get(full_name)
        if repo is None:
            continue

        branches = repo.branches or [stats.default_branch]

        # Group workflows by branch
        workflows_by_branch: dict[str, list[WorkflowStatus]] = {}
        for wf in stats.workflows:
            workflows_by_branch.setdefault(wf.branch, []).append(wf)

        # Count workflow failures
        workflow_failures = sum(1 for w in stats.workflows if w.status == "failure")

        # Build check results
        checks_by_branch, check_failures, check_warnings = _build_checks_for_repo(
            full_name, branches, check_targets, results_by_key
        )

        viewmodels.append(
            RepoViewModel(
                full_name=full_name,
                branches=branches,
                source=repo.source,
                open_issues=stats.open_issues,
                stale_issues=stats.stale_issues,
                open_prs=stats.open_pull_requests,
                stale_prs=stats.stale_pull_requests,
                workflow_failures=workflow_failures,
                check_failures=check_failures,
                check_warnings=check_warnings,
                warnings=stats.warnings,
                workflows_by_branch=workflows_by_branch,
                checks_by_branch=checks_by_branch,
                fetched_at=stats.fetched_at,
                last_commit_at=stats.last_commit_at,
                total_branches=stats.total_branches,
                include_checks=include_checks,
                include_stale=include_stale,
            )
        )
    return viewmodels


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    sort: str = "name",
    dir: str = "asc",
    include_checks: bool = True,
    include_stale: bool = True,
) -> HTMLResponse:
    """Dashboard page — lists all tracked repositories."""
    from grimoire.github.router import _last_refresh
    from grimoire.github.service import get_refresh_progress, is_refresh_running

    refresh_running = is_refresh_running()
    refresh_progress = get_refresh_progress()

    repos = await _build_repo_viewmodels(include_checks, include_stale)

    # Initial loading state: no repos yet and a refresh is in progress
    if not repos and refresh_running:
        return templates.TemplateResponse(
            request,
            "loading.html",
            context={
                "progress_completed": refresh_progress.completed if refresh_progress else 0,
                "progress_total": refresh_progress.total if refresh_progress else 0,
            },
        )

    repos = _sort_repos(repos, sort, dir)
    totals = _compute_totals(repos)

    # Collect global warnings
    warnings: list[str] = []
    for r in repos:
        for w in r.warnings:
            if w not in warnings:
                warnings.append(w)

    last_refresh_ago = _time_ago(_last_refresh) if _last_refresh else None
    refresh_running = is_refresh_running()
    refresh_progress = get_refresh_progress()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        context={
            "repos": repos,
            "totals": totals,
            "warnings": warnings,
            "last_refresh": _last_refresh,
            "last_refresh_ago": last_refresh_ago,
            "sort": sort,
            "dir": dir,
            "include_checks": include_checks,
            "include_stale": include_stale,
            "sort_labels": SORT_LABELS,
            "staleness": _staleness_config,
            "time_ago": _time_ago,
            "running": refresh_running,
            "progress_completed": refresh_progress.completed if refresh_progress else 0,
            "progress_total": refresh_progress.total if refresh_progress else 0,
            "refresh_schedule": _refresh_schedule,
        },
    )


@router.get("/repo/{owner}/{name}", response_class=HTMLResponse)
async def repository_detail(request: Request, owner: str, name: str) -> HTMLResponse:
    """Repository detail page."""
    from grimoire.github.router import _cache, _repos

    full_name = f"{owner}/{name}"
    stats = _cache.get(full_name)
    repo = _repos.get(full_name)

    if stats is None or repo is None:
        return templates.TemplateResponse(
            request,
            "not_found.html",
            context={"entity": f"Repository {full_name}"},
            status_code=404,
        )

    branches = repo.branches or [stats.default_branch]

    workflows_by_branch: dict[str, list[WorkflowStatus]] = {}
    for wf in stats.workflows:
        workflows_by_branch.setdefault(wf.branch, []).append(wf)

    workflow_failures = sum(1 for w in stats.workflows if w.status == "failure")
    workflow_pending = sum(1 for w in stats.workflows if w.status == "pending")

    check_targets, results_by_key = await _load_check_context({full_name: repo})
    checks_by_branch, check_failures, check_warnings = _build_checks_for_repo(
        full_name, branches, check_targets, results_by_key
    )

    return templates.TemplateResponse(
        request,
        "repository.html",
        context={
            "repo_name": full_name,
            "stats": stats,
            "repo": repo,
            "branches": branches,
            "workflows_by_branch": workflows_by_branch,
            "checks_by_branch": checks_by_branch,
            "workflow_failures": workflow_failures,
            "workflow_pending": workflow_pending,
            "check_failures": check_failures,
            "check_warnings": check_warnings,
            "staleness": _staleness_config,
            "time_ago": _time_ago,
        },
    )


@router.get("/actions", response_class=HTMLResponse)
async def actions_page(request: Request) -> HTMLResponse:
    """Actions page — list available actions with per-action results."""
    from grimoire.actions.router import _actions
    from grimoire.actions.router import _engine as _actions_engine

    # Build per-action aggregate stats from DB (latest run's repo results)
    action_stats: dict[str, dict[str, Any]] = {}
    if _actions_engine is not None:
        async with AsyncSession(_actions_engine) as session:
            # For each action, find the latest completed run and aggregate its repo results
            run_rows = (
                await session.execute(
                    text(
                        "SELECT ar.id, ar.action_slug, ar.started_at, arrr.passed "
                        "FROM action_run ar "
                        "JOIN action_run_repo arrr ON arrr.run_id = ar.id "
                        "WHERE ar.id IN ("
                        "  SELECT id FROM action_run sub "
                        "  WHERE sub.action_slug = ar.action_slug "
                        "  ORDER BY sub.started_at DESC LIMIT 1"
                        ")"
                    )
                )
            ).all()

            for row in run_rows:
                slug = row[1]
                started_at = row[2]
                passed = bool(row[3])
                stats = action_stats.setdefault(slug, {"pass": 0, "fail": 0, "last_run": None})
                if passed:
                    stats["pass"] += 1
                else:
                    stats["fail"] += 1
                if stats["last_run"] is None:
                    stats["last_run"] = started_at

    action_vms = []
    for a in _actions:
        targets = a.targets
        if targets is None:
            target_summary = "global"
        elif targets.regex is not None:
            target_summary = f"regex: {targets.regex}"
        elif targets.list is not None:
            target_summary = f"list: {len(targets.list)} repos"
        else:
            target_summary = "script"

        stats = action_stats.get(a.slug, {})
        action_vms.append(
            ActionViewModel(
                name=a.name,
                slug=a.slug,
                description=a.description,
                schedule=a.schedule,
                enabled=a.enabled,
                target_summary=target_summary,
                script=a.script,
                pass_count=stats.get("pass", 0),
                fail_count=stats.get("fail", 0),
                last_run=stats.get("last_run"),
            )
        )

    return templates.TemplateResponse(
        request,
        "actions.html",
        context={
            "actions": action_vms,
            "time_ago": _time_ago,
        },
    )


@router.get("/checks", response_class=HTMLResponse)
async def checks_page(request: Request) -> HTMLResponse:
    """Checks page — list defined checks, toggle, run, view results."""
    from grimoire.checks.router import _checks
    from grimoire.checks.router import _engine as _checks_engine

    # Build per-check aggregate stats from DB
    check_stats: dict[str, dict[str, Any]] = {}
    if _checks_engine is not None and _checks:
        async with AsyncSession(_checks_engine) as session:
            result = await session.execute(_LATEST_RESULTS_SQL)
            rows = result.all()
        for row in rows:
            slug = row[2]
            passed = bool(row[5])
            ts = row[6]
            stats = check_stats.setdefault(slug, {"pass": 0, "fail": 0, "last_run": None})
            if passed:
                stats["pass"] += 1
            else:
                stats["fail"] += 1
            if stats["last_run"] is None or (ts is not None and ts > stats["last_run"]):
                stats["last_run"] = ts

    check_vms = []
    for c in _checks:
        targets = c.targets
        if targets.regex is not None:
            target_summary = f"regex: {targets.regex}"
        elif targets.list is not None:
            target_summary = f"list: {len(targets.list)} repos"
        else:
            target_summary = "script"

        stats = check_stats.get(c.slug, {})
        raw_fail = stats.get("fail", 0)
        check_vms.append(
            CheckViewModel(
                name=c.name,
                slug=c.slug,
                description=c.description,
                schedule=c.schedule,
                enabled=c.enabled,
                target_summary=target_summary,
                script=c.script,
                severity=c.severity,
                pass_count=stats.get("pass", 0),
                fail_count=raw_fail if c.severity == "error" else 0,
                warn_count=raw_fail if c.severity == "warning" else 0,
                last_run=stats.get("last_run"),
            )
        )

    return templates.TemplateResponse(
        request,
        "checks.html",
        context={
            "checks": check_vms,
            "time_ago": _time_ago,
        },
    )


# ---------------------------------------------------------------------------
# HTMX Partial Routes
# ---------------------------------------------------------------------------


async def _build_sorted_repos(
    sort: str, direction: str, include_checks: bool = True, include_stale: bool = True
) -> list[RepoViewModel]:
    """Build and sort repo view models (shared by all dashboard partials)."""
    repos = await _build_repo_viewmodels(include_checks, include_stale)
    return _sort_repos(repos, sort, direction)


@router.get("/partials/dashboard-matrix", response_class=HTMLResponse)
async def dashboard_matrix_partial(
    request: Request,
    sort: str = "name",
    dir: str = "asc",
    include_checks: bool = True,
    include_stale: bool = True,
) -> HTMLResponse:
    """Return the compact matrix view for HTMX swap."""
    repos = await _build_sorted_repos(sort, dir, include_checks, include_stale)
    return templates.TemplateResponse(
        request,
        "partials/dashboard_matrix.html",
        context={
            "repos": repos,
            "sort": sort,
            "dir": dir,
            "include_checks": include_checks,
            "include_stale": include_stale,
            "staleness": _staleness_config,
            "time_ago": _time_ago,
        },
    )


@router.get("/partials/dashboard-list", response_class=HTMLResponse)
async def dashboard_list_partial(
    request: Request,
    sort: str = "name",
    dir: str = "asc",
    include_checks: bool = True,
    include_stale: bool = True,
) -> HTMLResponse:
    """Return the compact list view for HTMX swap."""
    repos = await _build_sorted_repos(sort, dir, include_checks, include_stale)
    return templates.TemplateResponse(
        request,
        "partials/dashboard_list.html",
        context={
            "repos": repos,
            "staleness": _staleness_config,
            "time_ago": _time_ago,
        },
    )


@router.get("/partials/dashboard-stats", response_class=HTMLResponse)
async def dashboard_stats_partial(
    request: Request,
    include_checks: bool = True,
    include_stale: bool = True,
) -> HTMLResponse:
    """Return the stats bar for HTMX swap, respecting the health-status toggle."""
    repos = await _build_repo_viewmodels(include_checks, include_stale)
    totals = _compute_totals(repos)
    return templates.TemplateResponse(
        request,
        "partials/dashboard_stats.html",
        context={"totals": totals},
    )


@router.get("/partials/action-run/{run_id}", response_class=HTMLResponse)
async def action_run_partial(request: Request, run_id: int) -> HTMLResponse:
    """Return expanded action run details for inline expansion."""
    from grimoire.actions.router import _engine as _actions_engine

    results_list: list[dict[str, Any]] = []
    if _actions_engine is not None:
        async with AsyncSession(_actions_engine) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, repo_full_name, branch, passed, output "
                        "FROM action_run_repo WHERE run_id = :run_id"
                    ),
                    params={"run_id": run_id},
                )
            ).all()
        for row in rows:
            results_list.append(
                {
                    "id": row[0],
                    "repo_full_name": row[1],
                    "branch": row[2],
                    "passed": bool(row[3]),
                    "output": row[4],
                }
            )

    return templates.TemplateResponse(
        request,
        "partials/action_run_detail.html",
        context={
            "run_id": run_id,
            "results": results_list,
        },
    )


@router.get("/partials/action-results/{slug}", response_class=HTMLResponse)
async def action_results_partial(
    request: Request, slug: str, sort: str = "repo", dir: str = "asc"
) -> HTMLResponse:
    """Return per-action results grouped by run for inline expansion."""
    from grimoire.actions.router import _engine as _actions_engine

    runs: list[dict[str, Any]] = []
    if _actions_engine is not None:
        # Fetch recent completed runs for this action (limit to last 10)
        run_query = text(
            "SELECT id, triggered_by, started_at "
            "FROM action_run "
            "WHERE action_slug = :slug AND status = 'completed' "
            "ORDER BY started_at DESC LIMIT 10"
        )
        async with AsyncSession(_actions_engine) as session:
            run_rows = (await session.execute(run_query, params={"slug": slug})).all()

        # For each run, fetch its per-repo results
        result_query = text(
            "SELECT id, repo_full_name, branch, passed "
            "FROM action_run_repo WHERE run_id = :run_id "
            "ORDER BY repo_full_name, branch"
        )
        for run_row in run_rows:
            run_id = run_row[0]
            async with AsyncSession(_actions_engine) as session:
                result_rows = (
                    await session.execute(result_query, params={"run_id": run_id})
                ).all()

            results_list = [
                {
                    "id": r[0],
                    "repo_full_name": r[1],
                    "branch": r[2],
                    "passed": bool(r[3]),
                }
                for r in result_rows
            ]

            # Sort results within the run
            sort_keys: dict[str, Any] = {
                "repo": lambda r: r["repo_full_name"].lower(),
                "branch": lambda r: r["branch"].lower(),
                "status": lambda r: 0 if r["passed"] else 1,
            }
            key_fn = sort_keys.get(sort, sort_keys["repo"])
            results_list.sort(key=key_fn, reverse=(dir == "desc"))

            pass_count = sum(1 for r in results_list if r["passed"])
            fail_count = len(results_list) - pass_count
            runs.append(
                {
                    "run_id": run_id,
                    "triggered_by": run_row[1],
                    "started_at": run_row[2],
                    "pass_count": pass_count,
                    "fail_count": fail_count,
                    "results": results_list,
                }
            )

    return templates.TemplateResponse(
        request,
        "partials/action_results.html",
        context={
            "runs": runs,
            "slug": slug,
            "sort": sort,
            "dir": dir,
            "time_ago": _time_ago,
        },
    )


@router.get("/partials/action-output/{result_id}", response_class=HTMLResponse)
async def action_output_partial(request: Request, result_id: int) -> HTMLResponse:
    """Return expanded action output for a single repo result."""
    from grimoire.actions.router import _engine as _actions_engine

    output = ""
    if _actions_engine is not None:
        async with AsyncSession(_actions_engine) as session:
            result = await session.execute(
                text("SELECT output FROM action_run_repo WHERE id = :id"),
                params={"id": result_id},
            )
            row = result.first()
            if row:
                output = row[0]

    return templates.TemplateResponse(
        request,
        "partials/check_output.html",
        context={
            "result_id": result_id,
            "output": output,
        },
    )


@router.get("/partials/check-output/{result_id}", response_class=HTMLResponse)
async def check_output_partial(request: Request, result_id: int) -> HTMLResponse:
    """Return expanded check output."""
    from grimoire.checks.router import _engine as _checks_engine

    output = ""
    if _checks_engine is not None:
        async with AsyncSession(_checks_engine) as session:
            result = await session.execute(
                text("SELECT output FROM check_result WHERE id = :id"),
                params={"id": result_id},
            )
            row = result.first()
            if row:
                output = row[0]

    return templates.TemplateResponse(
        request,
        "partials/check_output.html",
        context={
            "result_id": result_id,
            "output": output,
        },
    )


@router.get("/partials/check-results/{slug}", response_class=HTMLResponse)
async def check_results_partial(
    request: Request, slug: str, sort: str = "repo", dir: str = "asc"
) -> HTMLResponse:
    """Return per-check results grouped by run for inline expansion."""
    from grimoire.checks.router import _checks
    from grimoire.checks.router import _engine as _checks_engine

    # Determine severity for this check
    severity = "error"
    for c in _checks:
        if c.slug == slug:
            severity = c.severity
            break

    runs: list[dict[str, Any]] = []
    if _checks_engine is not None:
        # Fetch recent runs for this check (limit to last 10)
        run_query = text(
            "SELECT id, triggered_by, started_at "
            "FROM check_run "
            "WHERE check_slug = :slug AND status = 'completed' "
            "ORDER BY started_at DESC LIMIT 10"
        )
        async with AsyncSession(_checks_engine) as session:
            run_rows = (await session.execute(run_query, params={"slug": slug})).all()

        # For each run, fetch its per-repo results
        result_query = text(
            "SELECT id, repo_full_name, branch, passed, timestamp "
            "FROM check_result WHERE run_id = :run_id "
            "ORDER BY repo_full_name, branch"
        )
        for run_row in run_rows:
            run_id = run_row[0]
            async with AsyncSession(_checks_engine) as session:
                result_rows = (
                    await session.execute(result_query, params={"run_id": run_id})
                ).all()

            results_list = [
                {
                    "id": r[0],
                    "repo_full_name": r[1],
                    "branch": r[2],
                    "passed": bool(r[3]),
                    "timestamp": r[4],
                }
                for r in result_rows
            ]

            # Sort results within the run
            sort_keys: dict[str, Any] = {
                "repo": lambda r: r["repo_full_name"].lower(),
                "branch": lambda r: r["branch"].lower(),
                "status": lambda r: 0 if r["passed"] else 1,
                "time": lambda r: r["timestamp"] or "",
            }
            key_fn = sort_keys.get(sort, sort_keys["repo"])
            results_list.sort(key=key_fn, reverse=(dir == "desc"))

            pass_count = sum(1 for r in results_list if r["passed"])
            fail_count = len(results_list) - pass_count
            runs.append(
                {
                    "run_id": run_id,
                    "triggered_by": run_row[1],
                    "started_at": run_row[2],
                    "pass_count": pass_count,
                    "fail_count": fail_count,
                    "results": results_list,
                }
            )

        # Fallback: if no runs found, check for orphan results (pre-migration data)
        if not runs:
            orphan_query = text(
                "SELECT cr.id, cr.repo_full_name, cr.branch, cr.passed, cr.timestamp "
                "FROM check_result cr "
                "INNER JOIN ("
                "  SELECT repo_full_name, branch, MAX(timestamp) AS max_ts "
                "  FROM check_result WHERE check_slug = :slug "
                "  GROUP BY repo_full_name, branch"
                ") latest ON cr.repo_full_name = latest.repo_full_name "
                "AND cr.branch = latest.branch "
                "AND cr.timestamp = latest.max_ts "
                "AND cr.check_slug = :slug "
                "ORDER BY cr.timestamp DESC"
            )
            async with AsyncSession(_checks_engine) as session:
                orphan_rows = (await session.execute(orphan_query, params={"slug": slug})).all()
            if orphan_rows:
                results_list = [
                    {
                        "id": r[0],
                        "repo_full_name": r[1],
                        "branch": r[2],
                        "passed": bool(r[3]),
                        "timestamp": r[4],
                    }
                    for r in orphan_rows
                ]
                pass_count = sum(1 for r in results_list if r["passed"])
                fail_count = len(results_list) - pass_count
                runs.append(
                    {
                        "run_id": None,
                        "triggered_by": "unknown",
                        "started_at": results_list[0]["timestamp"] if results_list else None,
                        "pass_count": pass_count,
                        "fail_count": fail_count,
                        "results": results_list,
                    }
                )

    return templates.TemplateResponse(
        request,
        "partials/check_results.html",
        context={
            "runs": runs,
            "slug": slug,
            "severity": severity,
            "sort": sort,
            "dir": dir,
            "time_ago": _time_ago,
        },
    )


# ---------------------------------------------------------------------------
# Run-status polling partials
# ---------------------------------------------------------------------------


@router.get("/partials/refresh-status", response_class=HTMLResponse)
async def refresh_status_partial(request: Request, was_running: bool = False) -> HTMLResponse:
    """Return the refresh-button partial (idle or running state).

    When *was_running* is True but the refresh has finished, an
    ``HX-Trigger: refreshCompleted`` header is sent so the dashboard can
    auto-refresh its data.
    """
    from grimoire.github.service import get_refresh_progress, is_refresh_running

    running = is_refresh_running()
    progress = get_refresh_progress()
    resp = templates.TemplateResponse(
        request,
        "partials/refresh_button.html",
        context={
            "running": running,
            "progress_completed": progress.completed if progress else 0,
            "progress_total": progress.total if progress else 0,
        },
    )
    if was_running and not running:
        resp.headers["HX-Trigger"] = "refreshCompleted"
    return resp


@router.get("/partials/loading-status", response_class=HTMLResponse)
async def loading_status_partial(request: Request) -> HTMLResponse:
    """Return the loading progress partial.

    When the refresh is no longer running, sends an HX-Redirect to reload
    the dashboard (which will now show the real content).
    """
    from grimoire.github.service import get_refresh_progress, is_refresh_running

    running = is_refresh_running()
    progress = get_refresh_progress()

    if not running:
        resp = HTMLResponse("")
        resp.headers["HX-Redirect"] = "/"
        return resp

    return templates.TemplateResponse(
        request,
        "partials/loading_progress.html",
        context={
            "progress_completed": progress.completed if progress else 0,
            "progress_total": progress.total if progress else 0,
        },
    )


@router.post("/partials/refresh-trigger", response_class=HTMLResponse)
async def refresh_trigger(request: Request, background_tasks: BackgroundTasks) -> HTMLResponse:
    """Trigger a data refresh and return the 'Refreshing...' button partial.

    The refresh is started as a background task — the response is immediate.
    The returned partial includes polling to detect completion.
    """
    from grimoire.github.router import _refresh_callback
    from grimoire.github.service import is_refresh_running

    if _refresh_callback is None:
        raise HTTPException(status_code=500, detail="Refresh not configured")

    if not is_refresh_running():
        from collections.abc import Awaitable, Callable

        callback: Callable[[], Awaitable[None]] = _refresh_callback  # type: ignore[assignment]
        background_tasks.add_task(callback)

    return templates.TemplateResponse(
        request,
        "partials/refresh_button.html",
        context={"running": True, "progress_completed": 0, "progress_total": 0},
    )


@router.get("/partials/check-run-status/{slug}", response_class=HTMLResponse)
async def check_run_status_partial(
    request: Request, slug: str, was_running: bool = False
) -> HTMLResponse:
    """Return the run-button partial for a check (idle or running state).

    When *was_running* is True but the check has finished, an
    ``HX-Trigger: checkRunCompleted`` header is sent so the page can
    auto-refresh results.
    """
    from grimoire.checks.engine import get_check_progress, is_check_running

    running = is_check_running(slug)
    progress = get_check_progress(slug)
    resp = templates.TemplateResponse(
        request,
        "partials/check_run_button.html",
        context={
            "slug": slug,
            "running": running,
            "progress_completed": progress.completed if progress else 0,
            "progress_total": progress.total if progress else 0,
        },
    )
    if was_running and not running:
        resp.headers["HX-Trigger"] = "checkRunCompleted"
    return resp


@router.post("/partials/check-run/{slug}", response_class=HTMLResponse)
async def check_run_trigger(
    request: Request, slug: str, background_tasks: BackgroundTasks
) -> HTMLResponse:
    """Trigger a check run and return the 'Running...' button partial.

    The check is started as a background task — the response is immediate.
    The returned partial includes polling to detect completion.
    """
    from grimoire.checks.engine import is_check_running, run_check_for_all_targets
    from grimoire.checks.router import (
        _checks,
    )
    from grimoire.checks.router import (
        _engine as checks_engine,
    )
    from grimoire.checks.router import (
        _repos as checks_repos,
    )
    from grimoire.checks.router import (
        _workspace as checks_workspace,
    )

    # Find the check definition
    check = None
    for c in _checks:
        if c.slug == slug:
            check = c
            break
    if check is None:
        raise HTTPException(status_code=404, detail=f"Check '{slug}' not found")

    if is_check_running(slug):
        raise HTTPException(status_code=409, detail="Check is already running")

    assert checks_workspace is not None
    assert checks_engine is not None

    async def _run_in_background() -> None:
        await run_check_for_all_targets(
            check, checks_repos, checks_workspace, checks_engine, triggered_by="manual"
        )

    background_tasks.add_task(_run_in_background)

    return templates.TemplateResponse(
        request,
        "partials/check_run_button.html",
        context={"slug": slug, "running": True, "progress_completed": 0, "progress_total": 0},
    )


@router.post("/partials/action-run/{slug}", response_class=HTMLResponse)
async def action_run_trigger(
    request: Request, slug: str, background_tasks: BackgroundTasks
) -> HTMLResponse:
    """Trigger an action run and return the 'Running...' button partial.

    The action is started as a background task — the response is immediate.
    The returned partial includes polling to detect completion.
    """
    from grimoire.actions.engine import ActionConflictError, is_action_running, run_action
    from grimoire.actions.router import _actions
    from grimoire.actions.router import _engine as actions_engine
    from grimoire.actions.router import _repos as actions_repos
    from grimoire.actions.router import _workspace as actions_workspace

    # Find the action definition
    action = None
    for a in _actions:
        if a.slug == slug:
            action = a
            break
    if action is None:
        raise HTTPException(status_code=404, detail=f"Action '{slug}' not found")

    if is_action_running(slug):
        raise HTTPException(status_code=409, detail="Action is already running")

    assert actions_workspace is not None
    assert actions_engine is not None

    async def _run_in_background() -> None:
        try:
            await run_action(
                action, actions_repos, actions_workspace, actions_engine, triggered_by="manual"
            )
        except ActionConflictError:
            pass

    background_tasks.add_task(_run_in_background)

    return templates.TemplateResponse(
        request,
        "partials/action_run_button.html",
        context={"slug": slug, "running": True, "progress_completed": 0, "progress_total": 0},
    )


@router.get("/partials/action-run-status/{slug}", response_class=HTMLResponse)
async def action_run_status_partial(
    request: Request, slug: str, was_running: bool = False
) -> HTMLResponse:
    """Return the run-button partial for an action (idle or running state)."""
    from grimoire.actions.engine import get_action_progress, is_action_running
    from grimoire.actions.router import _engine as _actions_engine

    # Prefer in-memory tracker; fall back to DB for crash-recovery scenarios
    running = is_action_running(slug)
    if not running and _actions_engine is not None:
        async with AsyncSession(_actions_engine) as session:
            stmt = select(ActionRunRecord).where(
                ActionRunRecord.action_slug == slug,
                ActionRunRecord.status == "running",
            )
            running = (await session.exec(stmt)).first() is not None

    progress = get_action_progress(slug)
    resp = templates.TemplateResponse(
        request,
        "partials/action_run_button.html",
        context={
            "slug": slug,
            "running": running,
            "progress_completed": progress.completed if progress else 0,
            "progress_total": progress.total if progress else 0,
        },
    )
    if was_running and not running:
        resp.headers["HX-Trigger"] = "actionRunCompleted"
    return resp


# ---------------------------------------------------------------------------
# Backlog page
# ---------------------------------------------------------------------------


async def _build_backlog_items(
    categories: list[str] | None = None,
    repos_filter: list[str] | None = None,
    min_score: float = 0.0,
    config_override: BacklogConfig | None = None,
    search: str = "",
) -> list[BacklogItem]:
    """Collect and optionally filter backlog items."""
    from grimoire.github.router import _cache, _repos

    check_targets, results_by_key = await _load_check_context(_repos)

    from grimoire.checks.router import _checks

    config = config_override if config_override is not None else _backlog_config

    items = build_backlog_items(
        cache=_cache,
        repos=_repos,
        config=config,
        staleness=_staleness_config,
        check_targets=check_targets,
        results_by_key=results_by_key,
        check_defs=_checks,
    )

    # Apply filters
    if categories:
        cat_set = set(categories)
        items = [i for i in items if i.category.value in cat_set]

    if repos_filter:
        repo_set = set(repos_filter)
        items = [i for i in items if i.repo_full_name in repo_set]

    if min_score > 0:
        items = [i for i in items if i.score >= min_score]

    if search:
        q = search.lower()
        items = [
            i
            for i in items
            if q in i.repo_full_name.lower()
            or q in i.description.lower()
            or q in i.category_label.lower()
            or q in i.branch.lower()
        ]

    return items


@router.get("/backlog", response_class=HTMLResponse)
async def backlog_page(request: Request) -> HTMLResponse:
    """Backlog page — prioritised problem list across all repos."""
    from grimoire.github.router import _last_refresh

    items = await _build_backlog_items()

    # Build summary counts per tier
    tier_counts: dict[str, int] = {}
    for item in items:
        tier_counts[item.tier] = tier_counts.get(item.tier, 0) + 1

    # Count unique repos with problems
    repos_with_items = len({i.repo_full_name for i in items})

    # Check if grouped view was requested (via query param)
    group_by = request.query_params.get("group_by", "")
    groups = None
    type_groups = None
    if group_by == "repo":
        groups = group_by_repo(items)
    elif group_by == "type":
        type_groups = group_by_type(items)

    return templates.TemplateResponse(
        request,
        "backlog.html",
        context={
            "items": items,
            "groups": groups,
            "type_groups": type_groups,
            "group_by": group_by,
            "tier_counts": tier_counts,
            "total_items": len(items),
            "repos_with_items": repos_with_items,
            "backlog_config": _backlog_config,
            "time_ago": _time_ago,
            "last_refresh": _last_refresh,
        },
    )


@router.get("/partials/backlog-items", response_class=HTMLResponse)
async def backlog_items_partial(
    request: Request,
    categories: str = "",
    repos: str = "",
    min_score: float = 0.0,
    group_by: str = "",
    search: str = "",
    w_failing_workflow: float = -1,
    w_failing_check_error: float = -1,
    w_failing_check_warning: float = -1,
    w_stale_pr: float = -1,
    w_stale_issue: float = -1,
) -> HTMLResponse:
    """Return filtered backlog items as an HTMX partial."""
    cat_list = [c for c in categories.split(",") if c] or None
    repo_list = [r for r in repos.split(",") if r] or None

    # Build a temporary config override if any weight params were provided
    config_override: BacklogConfig | None = None
    weight_params = {
        "failing_workflow": w_failing_workflow,
        "failing_check_error": w_failing_check_error,
        "failing_check_warning": w_failing_check_warning,
        "stale_pr": w_stale_pr,
        "stale_issue": w_stale_issue,
    }
    if any(v >= 0 for v in weight_params.values()):
        from grimoire.config import BacklogCategoryWeights

        base = _backlog_config.category_weights
        overrides = {k: v if v >= 0 else getattr(base, k) for k, v in weight_params.items()}
        config_override = BacklogConfig(
            category_weights=BacklogCategoryWeights(**overrides),
            workflow_weights=_backlog_config.workflow_weights,
        )

    items = await _build_backlog_items(
        categories=cat_list,
        repos_filter=repo_list,
        min_score=min_score,
        config_override=config_override,
        search=search,
    )

    groups = None
    type_groups = None
    if group_by == "repo":
        groups = group_by_repo(items)
    elif group_by == "type":
        type_groups = group_by_type(items)

    return templates.TemplateResponse(
        request,
        "partials/backlog_items.html",
        context={
            "items": items,
            "groups": groups,
            "type_groups": type_groups,
            "time_ago": _time_ago,
        },
    )


@router.get("/api/backlog/export")
async def backlog_export_markdown(
    request: Request,  # noqa: ARG001
    categories: str = "",
    repos: str = "",
    min_score: float = 0.0,
) -> PlainTextResponse:
    """Export the backlog as Markdown text."""
    cat_list = [c for c in categories.split(",") if c] or None
    repo_list = [r for r in repos.split(",") if r] or None

    items = await _build_backlog_items(
        categories=cat_list,
        repos_filter=repo_list,
        min_score=min_score,
    )
    md = export_markdown(items)
    return PlainTextResponse(md, media_type="text/markdown")


@router.post("/api/backlog/save-weights")
async def backlog_save_weights(request: Request) -> dict[str, str]:
    """Persist category weights and workflow weights from the UI back to config.yaml."""
    import yaml

    if _config_path is None or not _config_path.exists():
        return {"status": "error", "message": "Config file path not available"}

    body = await request.json()

    with open(_config_path) as f:
        raw = yaml.safe_load(f) or {}

    if "backlog" not in raw:
        raw["backlog"] = {}
    if "category_weights" in body:
        raw["backlog"]["category_weights"] = body["category_weights"]
    if "workflow_weights" in body:
        raw["backlog"]["workflow_weights"] = body["workflow_weights"]
    if "repository_weights" in body:
        raw["backlog"]["repository_weights"] = body["repository_weights"]

    with open(_config_path, "w") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

    # Reload in-memory config
    from grimoire.config import BacklogConfig

    new_backlog = BacklogConfig.model_validate(raw.get("backlog", {}))
    global _backlog_config  # noqa: PLW0603
    _backlog_config = new_backlog

    return {"status": "ok"}
