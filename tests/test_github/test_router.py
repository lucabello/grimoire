"""Tests for grimoire.github.router — cache and refresh logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from httpx import AsyncClient

from grimoire.github.router import update_cache
from grimoire.models import RepositoryStats, TrackedRepository, WorkflowStatus


def _make_repo(name: str = "owner/repo") -> TrackedRepository:
    return TrackedRepository(full_name=name, branches=["main"], source=name)


def _make_stats(name: str = "owner/repo", fetched_at: datetime | None = None) -> RepositoryStats:
    return RepositoryStats(
        full_name=name,
        default_branch="main",
        open_issues=0,
        stale_issues=0,
        open_pull_requests=0,
        stale_pull_requests=0,
        workflows=[],
        total_branches=1,
        fetched_at=fetched_at,
    )


async def test_update_cache_uses_current_time_by_default():
    """When no timestamp is given, _last_refresh is set to approximately now."""
    before = datetime.now(tz=timezone.utc)
    update_cache([_make_repo()], [_make_stats()])

    from grimoire.github.router import _last_refresh as lr

    assert lr is not None
    assert lr >= before
    assert (lr - before).total_seconds() < 2


async def test_update_cache_uses_explicit_timestamp():
    """When a timestamp is provided, _last_refresh uses that value."""
    explicit_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    update_cache([_make_repo()], [_make_stats()], timestamp=explicit_time)

    from grimoire.github.router import _last_refresh as lr

    assert lr == explicit_time


async def test_update_cache_preserves_real_fetched_at_from_db():
    """Simulates startup: loading old cached data should show the real data age."""
    old_time = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    update_cache(
        [_make_repo()],
        [_make_stats(fetched_at=old_time)],
        timestamp=old_time,
    )

    from grimoire.github.router import _last_refresh as lr

    assert lr == old_time
    # Verify it's actually in the past
    assert (datetime.now(tz=timezone.utc) - lr).total_seconds() > 3600


async def test_repo_detail_exposes_workflow_run_details(async_client: AsyncClient):
    """GET /api/repos/{owner}/{name} returns the last-run fields, ISO 8601 timestamps and raw conclusion."""
    stats = _make_stats()
    stats.workflows = [
        WorkflowStatus(
            name="CI",
            branch="main",
            status="failure",
            url="https://github.com/owner/repo/actions/workflows/ci.yml",
            run_url="https://github.com/owner/repo/actions/runs/42",
            run_id=42,
            run_attempt=2,
            event="push",
            head_sha="d" * 40,
            conclusion="timed_out",
            created_at=datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc),
            run_started_at=datetime(2026, 3, 1, 10, 0, 5, tzinfo=timezone.utc),
            updated_at=datetime(2026, 3, 1, 10, 5, tzinfo=timezone.utc),
        ),
        WorkflowStatus(name="Build", branch="main", status="pending", url="u", run_id=43),
    ]
    update_cache([_make_repo()], [stats])

    resp = await async_client.get("/api/repos/owner/repo")

    assert resp.status_code == 200
    ci, build = resp.json()["workflows"]
    assert ci == {
        "name": "CI",
        "branch": "main",
        "status": "failure",
        "url": "https://github.com/owner/repo/actions/workflows/ci.yml",
        "run_url": "https://github.com/owner/repo/actions/runs/42",
        "run_id": 42,
        "run_attempt": 2,
        "event": "push",
        "head_sha": "d" * 40,
        "conclusion": "timed_out",
        "created_at": "2026-03-01T10:00:00Z",
        "run_started_at": "2026-03-01T10:00:05Z",
        "updated_at": "2026-03-01T10:05:00Z",
    }
    assert build["conclusion"] is None
    assert build["run_id"] == 43
