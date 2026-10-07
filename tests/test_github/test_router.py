"""Tests for grimoire.github.router — cache and refresh logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from grimoire.github.router import update_cache
from grimoire.models import RepositoryStats, TrackedRepository


def _make_repo(name: str = "owner/repo") -> TrackedRepository:
    return TrackedRepository(full_name=name, branches=["main"], sources=[name])


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
