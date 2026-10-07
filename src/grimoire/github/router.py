"""REST API routers for repository data and refresh."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException

from grimoire.github.schemas import (
    RefreshResponse,
    RefreshStatusResponse,
    RepoDetailResponse,
    RepoListResponse,
    RepoSummary,
    WorkflowStatusResponse,
)
from grimoire.models import RepositoryStats, TrackedRepository

# ---------------------------------------------------------------------------
# In-memory cache — populated by the service layer
# ---------------------------------------------------------------------------

_cache: dict[str, RepositoryStats] = {}
_repos: dict[str, TrackedRepository] = {}
_last_refresh: datetime | None = None


def update_cache(
    repos: list[TrackedRepository],
    stats: list[RepositoryStats],
    *,
    timestamp: datetime | None = None,
) -> None:
    """Replace the in-memory cache with fresh data.

    Args:
        timestamp: When the data was actually fetched. Defaults to now (UTC).
                   Pass a real fetched_at when loading from DB cache.
    """
    global _last_refresh  # noqa: PLW0603
    _cache.clear()
    _repos.clear()
    for s in stats:
        _cache[s.full_name] = s
    for r in repos:
        _repos[r.full_name] = r
    _last_refresh = timestamp or datetime.now(tz=__import__("datetime").timezone.utc)


def _build_summary(repo: TrackedRepository, stats: RepositoryStats) -> RepoSummary:
    workflow_failures = sum(1 for w in stats.workflows if w.status == "failure")
    return RepoSummary(
        full_name=stats.full_name,
        default_branch=stats.default_branch,
        branches=repo.branches,
        sources=repo.sources,
        source=repo.source,
        open_issues=stats.open_issues,
        stale_issues=stats.stale_issues,
        open_pull_requests=stats.open_pull_requests,
        stale_pull_requests=stats.stale_pull_requests,
        workflow_failures=workflow_failures,
        last_commit_at=stats.last_commit_at,
        total_branches=stats.total_branches,
        warnings=stats.warnings,
        fetched_at=stats.fetched_at,
    )


# ---------------------------------------------------------------------------
# Repos router — mounted at /api/repos
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/repos", tags=["repositories"])


@router.get("/", response_model=RepoListResponse)
async def list_repos() -> RepoListResponse:
    """List all tracked repositories with summary stats."""
    summaries: list[RepoSummary] = []
    for full_name, stats in _cache.items():
        repo = _repos.get(full_name)
        if repo is None:
            continue
        summaries.append(_build_summary(repo, stats))
    return RepoListResponse(repositories=summaries, last_refresh=_last_refresh)


@router.get("/{owner}/{name}", response_model=RepoDetailResponse)
async def get_repo_detail(owner: str, name: str) -> RepoDetailResponse:
    """Get detailed stats for a single repository."""
    full_name = f"{owner}/{name}"
    stats = _cache.get(full_name)
    repo = _repos.get(full_name)
    if stats is None or repo is None:
        raise HTTPException(status_code=404, detail=f"Repository {full_name} not found in cache")

    return RepoDetailResponse(
        full_name=stats.full_name,
        default_branch=stats.default_branch,
        branches=repo.branches,
        sources=repo.sources,
        source=repo.source,
        open_issues=stats.open_issues,
        stale_issues=stats.stale_issues,
        open_pull_requests=stats.open_pull_requests,
        stale_pull_requests=stats.stale_pull_requests,
        last_commit_at=stats.last_commit_at,
        total_branches=stats.total_branches,
        workflows=[
            WorkflowStatusResponse(
                name=w.name,
                branch=w.branch,
                status=w.status,
                url=w.url,
                run_url=w.run_url,
            )
            for w in stats.workflows
        ],
        warnings=stats.warnings,
        fetched_at=stats.fetched_at,
    )


# ---------------------------------------------------------------------------
# Refresh router — mounted at /api
# ---------------------------------------------------------------------------

refresh_router = APIRouter(tags=["repositories"])

# This will be set from outside (e.g., by the app factory or lifespan)
_refresh_callback: object | None = None


def set_refresh_callback(callback: object) -> None:
    """Register the async callable that performs a full refresh."""
    global _refresh_callback  # noqa: PLW0603
    _refresh_callback = callback


@refresh_router.post("/refresh", response_model=RefreshResponse, status_code=202)
async def trigger_refresh(background_tasks: BackgroundTasks) -> RefreshResponse:
    """Trigger a manual data refresh (runs in background)."""
    from grimoire.github.service import is_refresh_running

    if _refresh_callback is None:
        return RefreshResponse(
            status="error", message="Refresh not configured — no callback registered"
        )

    if is_refresh_running():
        return RefreshResponse(status="ok", message="Refresh already in progress")

    from collections.abc import Awaitable, Callable

    callback: Callable[[], Awaitable[None]] = _refresh_callback  # type: ignore[assignment]
    background_tasks.add_task(callback)
    return RefreshResponse(status="ok", message="Refresh started")


@refresh_router.get("/refresh/status", response_model=RefreshStatusResponse)
async def refresh_status() -> RefreshStatusResponse:
    """Return current refresh progress."""
    from grimoire.github.service import get_refresh_progress, is_refresh_running

    running = is_refresh_running()
    progress = get_refresh_progress()
    return RefreshStatusResponse(
        running=running,
        completed=progress.completed if progress else 0,
        total=progress.total if progress else 0,
    )
