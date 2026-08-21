"""Tests for the GitHub service layer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from grimoire.config import (
    GitHubConfig,
    GrimoireConfig,
    StalenessConfig,
    StaticRepoSource,
    TeamRepoSource,
)
from grimoire.database import (
    CachedIssue,
    CachedPullRequest,
    CachedRepository,
    CachedWorkflowStatus,
    CheckResultRecord,
    create_tables,
    get_engine,
)
from grimoire.github.client import GitHubClient
from grimoire.github.service import (
    fetch_repository_stats,
    load_stats_from_db,
    prune_stale_data,
    refresh_all_stats,
    resolve_repositories,
    save_stats_to_db,
)
from grimoire.models import RepositoryStats, TrackedRepository, WorkflowStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine(tmp_path) -> AsyncEngine:
    db_path = str(tmp_path / "test.db")
    eng = await get_engine(db_path)
    await create_tables(eng)
    return eng


@pytest.fixture
async def client(engine: AsyncEngine) -> GitHubClient:
    c = GitHubClient(token="ghp_test", engine=engine, backoff_factors=(0.0, 0.0, 0.0))
    yield c  # type: ignore[misc]
    await c.close()


def _make_config(
    repos: list[StaticRepoSource | TeamRepoSource] | None = None,
    staleness: StalenessConfig | None = None,
) -> GrimoireConfig:
    return GrimoireConfig(
        github=GitHubConfig(token="ghp_test"),
        repositories=repos or [StaticRepoSource(repo="owner/repo1")],
        staleness=staleness or StalenessConfig(),
    )


# ---------------------------------------------------------------------------
# resolve_repositories — static
# ---------------------------------------------------------------------------


@respx.mock
async def test_resolve_static_repos(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/owner/repo1").mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "owner/repo1",
                "default_branch": "main",
                "archived": False,
            },
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    config = _make_config(
        repos=[StaticRepoSource(repo="owner/repo1", branches=["main", "develop"])]
    )
    repos = await resolve_repositories(config, client)
    assert len(repos) == 1
    assert repos[0].full_name == "owner/repo1"
    assert repos[0].branches == ["main", "develop"]
    assert repos[0].source == "static"


@respx.mock
async def test_resolve_static_uses_default_branch(client: GitHubClient) -> None:
    """When no branches specified, use the repo's default_branch."""
    respx.get("https://api.github.com/repos/owner/repo1").mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "owner/repo1",
                "default_branch": "develop",
                "archived": False,
            },
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    config = _make_config(repos=[StaticRepoSource(repo="owner/repo1")])
    repos = await resolve_repositories(config, client)
    assert len(repos) == 1
    assert repos[0].branches == ["develop"]


@respx.mock
async def test_resolve_skips_archived(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/owner/archived").mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "owner/archived",
                "default_branch": "main",
                "archived": True,
            },
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    config = _make_config(repos=[StaticRepoSource(repo="owner/archived")])
    repos = await resolve_repositories(config, client)
    assert len(repos) == 0


# ---------------------------------------------------------------------------
# resolve_repositories — team
# ---------------------------------------------------------------------------


@respx.mock
async def test_resolve_team_repos(client: GitHubClient) -> None:
    respx.get("https://api.github.com/orgs/myorg/teams/backend/repos").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "full_name": "myorg/service-a",
                    "default_branch": "main",
                    "archived": False,
                },
                {
                    "full_name": "myorg/service-b",
                    "default_branch": "main",
                    "archived": False,
                },
                {
                    "full_name": "myorg/old-repo",
                    "default_branch": "main",
                    "archived": True,
                },
            ],
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    config = _make_config(
        repos=[TeamRepoSource(team="myorg/backend", exclude=["myorg/service-b"])]
    )
    repos = await resolve_repositories(config, client)
    assert len(repos) == 1
    assert repos[0].full_name == "myorg/service-a"
    assert repos[0].source == "team:myorg/backend"


# ---------------------------------------------------------------------------
# fetch_repository_stats — staleness
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_stats_stale_detection(client: GitHubClient) -> None:
    now = datetime.now(UTC)
    old_date = (now - timedelta(days=400)).isoformat()
    fresh_date = (now - timedelta(days=10)).isoformat()

    respx.get("https://api.github.com/repos/owner/repo1/issues").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 1,
                    "title": "Old issue",
                    "updated_at": old_date,
                    "created_at": old_date,
                },
                {
                    "number": 2,
                    "title": "Fresh issue",
                    "updated_at": fresh_date,
                    "created_at": fresh_date,
                },
            ],
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    respx.get("https://api.github.com/repos/owner/repo1/pulls").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"number": 10, "title": "Old PR", "updated_at": old_date, "created_at": old_date},
                {
                    "number": 11,
                    "title": "Fresh PR",
                    "updated_at": fresh_date,
                    "created_at": fresh_date,
                },
            ],
            headers={"X-RateLimit-Remaining": "4998", "X-RateLimit-Limit": "5000"},
        )
    )
    respx.get("https://api.github.com/repos/owner/repo1/actions/workflows").mock(
        return_value=httpx.Response(
            200,
            json={"total_count": 0, "workflows": []},
            headers={"X-RateLimit-Remaining": "4997", "X-RateLimit-Limit": "5000"},
        )
    )
    respx.get("https://api.github.com/repos/owner/repo1/branches/main").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "main",
                "commit": {
                    "sha": "abc",
                    "commit": {"committer": {"date": fresh_date}},
                },
            },
            headers={"X-RateLimit-Remaining": "4996", "X-RateLimit-Limit": "5000"},
        )
    )
    respx.get("https://api.github.com/repos/owner/repo1/branches").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "main",
                    "commit": {"sha": "abc"},
                },
                {
                    "name": "old-feature",
                    "commit": {"sha": "def"},
                },
            ],
            headers={"X-RateLimit-Remaining": "4995", "X-RateLimit-Limit": "5000"},
        )
    )
    respx.get("https://api.github.com/repos/owner/repo1/git/commits/abc").mock(
        return_value=httpx.Response(
            200,
            json={"sha": "abc", "committer": {"date": fresh_date}},
            headers={"X-RateLimit-Remaining": "4994", "X-RateLimit-Limit": "5000"},
        )
    )
    respx.get("https://api.github.com/repos/owner/repo1/git/commits/def").mock(
        return_value=httpx.Response(
            200,
            json={"sha": "def", "committer": {"date": old_date}},
            headers={"X-RateLimit-Remaining": "4993", "X-RateLimit-Limit": "5000"},
        )
    )

    repo = TrackedRepository(full_name="owner/repo1", default_branch="main", branches=["main"])
    staleness = StalenessConfig(issues_days=365, pull_requests_days=30)
    stats = await fetch_repository_stats(repo, client, staleness)

    assert stats.open_issues == 2
    assert stats.stale_issues == 1  # only the 400-day-old one
    assert stats.open_pull_requests == 2
    assert stats.stale_pull_requests == 1  # only the 400-day-old one
    assert stats.last_commit_at is not None
    assert stats.total_branches == 2


# ---------------------------------------------------------------------------
# DB round-trip: save + load
# ---------------------------------------------------------------------------


async def test_save_and_load_stats(engine: AsyncEngine) -> None:
    repos = [
        TrackedRepository(
            full_name="owner/repo1",
            default_branch="main",
            branches=["main", "develop"],
            source="static",
        )
    ]
    now = datetime.now(UTC)
    stats_list = [
        RepositoryStats(
            full_name="owner/repo1",
            default_branch="main",
            open_issues=5,
            stale_issues=2,
            open_pull_requests=3,
            stale_pull_requests=1,
            workflows=[
                WorkflowStatus(
                    name="CI",
                    branch="main",
                    status="success",
                    url="https://github.com/owner/repo1/actions/workflows/ci.yml",
                    run_url="https://github.com/owner/repo1/actions/runs/123",
                )
            ],
            fetched_at=now,
            last_commit_at=now,
            total_branches=5,
        )
    ]

    await save_stats_to_db(engine, stats_list, repos)

    # Verify records in DB
    async with AsyncSession(engine) as session:
        cached = (await session.exec(select(CachedRepository))).all()
        assert len(cached) == 1
        assert cached[0].full_name == "owner/repo1"
        assert json.loads(cached[0].branches_json) == ["main", "develop"]
        assert cached[0].total_branches == 5
        assert cached[0].last_commit_at is not None

        wf_rows = (await session.exec(select(CachedWorkflowStatus))).all()
        assert len(wf_rows) == 1
        assert wf_rows[0].workflow_name == "CI"
        assert wf_rows[0].status == "success"

    # Load back
    loaded_repos, loaded_stats = await load_stats_from_db(engine)
    assert len(loaded_repos) == 1
    assert loaded_repos[0].full_name == "owner/repo1"
    assert loaded_repos[0].branches == ["main", "develop"]
    assert len(loaded_stats) == 1
    assert loaded_stats[0].workflows[0].name == "CI"
    assert loaded_stats[0].total_branches == 5
    assert loaded_stats[0].last_commit_at is not None


async def test_save_replaces_old_data(engine: AsyncEngine) -> None:
    """Saving twice should replace, not duplicate, cached records."""
    repos = [TrackedRepository(full_name="owner/repo1", default_branch="main", branches=["main"])]
    now = datetime.now(UTC)
    stats_v1 = [RepositoryStats(full_name="owner/repo1", default_branch="main", fetched_at=now)]
    stats_v2 = [
        RepositoryStats(
            full_name="owner/repo1",
            default_branch="main",
            open_issues=10,
            fetched_at=now,
        )
    ]

    await save_stats_to_db(engine, stats_v1, repos)
    await save_stats_to_db(engine, stats_v2, repos)

    async with AsyncSession(engine) as session:
        cached = (await session.exec(select(CachedRepository))).all()
        assert len(cached) == 1


# ---------------------------------------------------------------------------
# refresh_all_stats — 304 fallback
# ---------------------------------------------------------------------------


@respx.mock
async def test_refresh_falls_back_to_cache_on_304(
    client: GitHubClient, engine: AsyncEngine
) -> None:
    """When repo resolution returns nothing (all 304s), cached repos are used."""
    # Pre-populate the DB with cached data
    repos = [TrackedRepository(full_name="myorg/svc", default_branch="main", branches=["main"])]
    now = datetime.now(UTC)
    stats_list = [
        RepositoryStats(
            full_name="myorg/svc",
            default_branch="main",
            open_issues=3,
            fetched_at=now,
        )
    ]
    await save_stats_to_db(engine, stats_list, repos)

    # Mock team endpoint returning 304 (ETag hit)
    respx.get("https://api.github.com/orgs/myorg/teams/backend/repos").mock(
        return_value=httpx.Response(
            304,
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )

    # Mock the per-repo endpoints (also 304)
    for pattern in [
        "/repos/myorg/svc/issues",
        "/repos/myorg/svc/pulls",
        "/repos/myorg/svc/actions/workflows",
        "/repos/myorg/svc/branches/main",
        "/repos/myorg/svc/branches",
    ]:
        respx.get(f"https://api.github.com{pattern}").mock(
            return_value=httpx.Response(
                304,
                headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
            )
        )

    config = _make_config(repos=[TeamRepoSource(team="myorg/backend")])
    result_repos, result_stats = await refresh_all_stats(config, client)

    # Should fall back to cached repos rather than returning empty
    assert len(result_repos) == 1
    assert result_repos[0].full_name == "myorg/svc"
    assert len(result_stats) == 1


@respx.mock
async def test_workflow_runs_304_preserves_previous_status(client: GitHubClient) -> None:
    """When get_workflow_runs returns 304, preserve previous workflow status."""
    # Mock endpoints for a repo with one workflow
    respx.get("https://api.github.com/repos/owner/repo1/issues").mock(
        return_value=httpx.Response(200, json=[], headers=_rl_headers())
    )
    respx.get("https://api.github.com/repos/owner/repo1/pulls").mock(
        return_value=httpx.Response(200, json=[], headers=_rl_headers())
    )
    respx.get("https://api.github.com/repos/owner/repo1/actions/workflows").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflows": [{"id": 42, "name": "CI", "html_url": "https://github.com/wf"}],
            },
            headers=_rl_headers(),
        )
    )
    # Workflow runs returns 304 (cache hit)
    respx.get("https://api.github.com/repos/owner/repo1/actions/workflows/42/runs").mock(
        return_value=httpx.Response(304, headers=_rl_headers())
    )
    respx.get("https://api.github.com/repos/owner/repo1/branches/main").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "main",
                "commit": {
                    "sha": "abc",
                    "commit": {"committer": {"date": datetime.now(UTC).isoformat()}},
                },
            },
            headers=_rl_headers(),
        )
    )
    respx.get("https://api.github.com/repos/owner/repo1/branches").mock(
        return_value=httpx.Response(200, json=[], headers=_rl_headers())
    )

    repo = TrackedRepository(full_name="owner/repo1", default_branch="main", branches=["main"])
    previous = RepositoryStats(
        full_name="owner/repo1",
        default_branch="main",
        workflows=[
            WorkflowStatus(
                name="CI",
                branch="main",
                status="success",
                url="https://github.com/wf",
                run_url="https://github.com/run/1",
            )
        ],
    )
    staleness = StalenessConfig()
    stats = await fetch_repository_stats(repo, client, staleness, previous=previous)

    # The previous workflow status should be preserved (not dropped)
    assert len(stats.workflows) == 1
    assert stats.workflows[0].name == "CI"
    assert stats.workflows[0].status == "success"


def _rl_headers() -> dict[str, str]:
    return {"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"}


@respx.mock
async def test_workflow_runs_falls_back_when_branch_filter_empty(client: GitHubClient) -> None:
    """Non-branch-triggered workflows (e.g. `release`) have no runs matching a
    tracked branch's head_branch. Grimoire should fall back to the latest run
    overall and report it under the configured branch."""
    respx.get("https://api.github.com/repos/owner/repo1/issues").mock(
        return_value=httpx.Response(200, json=[], headers=_rl_headers())
    )
    respx.get("https://api.github.com/repos/owner/repo1/pulls").mock(
        return_value=httpx.Response(200, json=[], headers=_rl_headers())
    )
    respx.get("https://api.github.com/repos/owner/repo1/actions/workflows").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflows": [{"id": 42, "name": "Release", "html_url": "https://github.com/wf"}],
            },
            headers=_rl_headers(),
        )
    )

    route = respx.get("https://api.github.com/repos/owner/repo1/actions/workflows/42/runs")
    route.side_effect = [
        # First call (branch=main filter) returns no runs
        httpx.Response(200, json={"total_count": 0, "workflow_runs": []}, headers=_rl_headers()),
        # Fallback call (no branch filter) returns the latest release run
        httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": 999,
                        "conclusion": "success",
                        "html_url": "https://github.com/run/999",
                        "head_branch": "v1.2.3",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
            },
            headers=_rl_headers(),
        ),
    ]

    respx.get("https://api.github.com/repos/owner/repo1/branches/main").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "main",
                "commit": {
                    "sha": "abc",
                    "commit": {"committer": {"date": datetime.now(UTC).isoformat()}},
                },
            },
            headers=_rl_headers(),
        )
    )
    respx.get("https://api.github.com/repos/owner/repo1/branches").mock(
        return_value=httpx.Response(200, json=[], headers=_rl_headers())
    )

    repo = TrackedRepository(full_name="owner/repo1", default_branch="main", branches=["main"])
    staleness = StalenessConfig()
    stats = await fetch_repository_stats(repo, client, staleness)

    assert len(stats.workflows) == 1
    assert stats.workflows[0].name == "Release"
    assert stats.workflows[0].branch == "main"
    assert stats.workflows[0].status == "success"
    assert stats.workflows[0].run_url == "https://github.com/run/999"

    calls = route.calls
    assert len(calls) == 2
    assert "branch" in calls[0].request.url.params
    assert "branch" not in calls[1].request.url.params


@respx.mock
async def test_workflow_runs_prefers_newer_unscoped_run_over_stale_branch_match(
    client: GitHubClient,
) -> None:
    """A workflow can have an old run whose head_branch happens to equal the
    default branch (e.g. an old failed release run), while newer runs are
    invisible to the branch filter because they were triggered from a tag.
    Grimoire must report the newer run, not the stale branch-matched one."""
    respx.get("https://api.github.com/repos/owner/repo1/issues").mock(
        return_value=httpx.Response(200, json=[], headers=_rl_headers())
    )
    respx.get("https://api.github.com/repos/owner/repo1/pulls").mock(
        return_value=httpx.Response(200, json=[], headers=_rl_headers())
    )
    respx.get("https://api.github.com/repos/owner/repo1/actions/workflows").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflows": [{"id": 42, "name": "Release", "html_url": "https://github.com/wf"}],
            },
            headers=_rl_headers(),
        )
    )

    route = respx.get("https://api.github.com/repos/owner/repo1/actions/workflows/42/runs")
    route.side_effect = [
        # branch=main filter: an old failed run whose head_branch is genuinely "main"
        httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": 100,
                        "conclusion": "failure",
                        "html_url": "https://github.com/run/100",
                        "head_branch": "main",
                        "created_at": "2025-10-13T08:29:41Z",
                    }
                ],
            },
            headers=_rl_headers(),
        ),
        # unscoped fallback: the actual latest run, triggered from a tag
        httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": 999,
                        "conclusion": "success",
                        "html_url": "https://github.com/run/999",
                        "head_branch": "v4.1.3",
                        "created_at": "2026-08-12T13:47:32Z",
                    }
                ],
            },
            headers=_rl_headers(),
        ),
    ]

    respx.get("https://api.github.com/repos/owner/repo1/branches/main").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "main",
                "commit": {
                    "sha": "abc",
                    "commit": {"committer": {"date": datetime.now(UTC).isoformat()}},
                },
            },
            headers=_rl_headers(),
        )
    )
    respx.get("https://api.github.com/repos/owner/repo1/branches").mock(
        return_value=httpx.Response(200, json=[], headers=_rl_headers())
    )

    repo = TrackedRepository(full_name="owner/repo1", default_branch="main", branches=["main"])
    staleness = StalenessConfig()
    stats = await fetch_repository_stats(repo, client, staleness)

    assert len(stats.workflows) == 1
    assert stats.workflows[0].status == "success"
    assert stats.workflows[0].run_url == "https://github.com/run/999"


@respx.mock
async def test_workflow_runs_no_fallback_for_non_default_branch(client: GitHubClient) -> None:
    """Periodic/release-only workflows must not appear under non-default
    tracked branches, since they never actually ran there."""
    respx.get("https://api.github.com/repos/owner/repo1/issues").mock(
        return_value=httpx.Response(200, json=[], headers=_rl_headers())
    )
    respx.get("https://api.github.com/repos/owner/repo1/pulls").mock(
        return_value=httpx.Response(200, json=[], headers=_rl_headers())
    )
    respx.get("https://api.github.com/repos/owner/repo1/actions/workflows").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflows": [{"id": 42, "name": "Update", "html_url": "https://github.com/wf"}],
            },
            headers=_rl_headers(),
        )
    )

    route = respx.get("https://api.github.com/repos/owner/repo1/actions/workflows/42/runs")
    route.mock(
        return_value=httpx.Response(
            200, json={"total_count": 0, "workflow_runs": []}, headers=_rl_headers()
        )
    )

    respx.get("https://api.github.com/repos/owner/repo1/branches/feature").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "feature",
                "commit": {
                    "sha": "abc",
                    "commit": {"committer": {"date": datetime.now(UTC).isoformat()}},
                },
            },
            headers=_rl_headers(),
        )
    )
    respx.get("https://api.github.com/repos/owner/repo1/branches").mock(
        return_value=httpx.Response(200, json=[], headers=_rl_headers())
    )

    repo = TrackedRepository(full_name="owner/repo1", default_branch="main", branches=["feature"])
    staleness = StalenessConfig()
    stats = await fetch_repository_stats(repo, client, staleness)

    # No workflow status should be recorded for the non-default branch
    assert len(stats.workflows) == 0

    calls = route.calls
    assert len(calls) == 1
    assert "branch" in calls[0].request.url.params


# ---------------------------------------------------------------------------
# prune_removed_repos
# ---------------------------------------------------------------------------


async def test_prune_removes_repos_not_in_config(engine: AsyncEngine) -> None:
    """Repos in DB but not in config should be deleted."""
    from grimoire.github.service import prune_removed_repos

    # Seed DB with two repos
    repo_a = TrackedRepository(full_name="org/keep", default_branch="main", branches=["main"])
    repo_b = TrackedRepository(full_name="org/remove", default_branch="main", branches=["main"])
    stats = [
        RepositoryStats(full_name="org/keep", default_branch="main"),
        RepositoryStats(full_name="org/remove", default_branch="main"),
    ]
    await save_stats_to_db(engine, stats, [repo_a, repo_b])

    # Config only has org/keep
    config = _make_config(repos=[StaticRepoSource(repo="org/keep")])
    pruned = await prune_removed_repos(engine, config)
    assert pruned == 1

    # Verify only org/keep remains
    repos, _ = await load_stats_from_db(engine)
    assert [r.full_name for r in repos] == ["org/keep"]


async def test_prune_skipped_with_team_sources(engine: AsyncEngine) -> None:
    """Pruning should be skipped when team sources are present."""
    from grimoire.github.service import prune_removed_repos

    # Seed DB with a repo
    repo = TrackedRepository(full_name="org/repo", default_branch="main", branches=["main"])
    await save_stats_to_db(
        engine, [RepositoryStats(full_name="org/repo", default_branch="main")], [repo]
    )

    # Config has a team source — can't know full set, so skip pruning
    config = _make_config(repos=[TeamRepoSource(team="org/myteam")])
    pruned = await prune_removed_repos(engine, config)
    assert pruned == 0

    # Repo should still be there
    repos, _ = await load_stats_from_db(engine)
    assert len(repos) == 1


# ---------------------------------------------------------------------------
# prune_stale_data
# ---------------------------------------------------------------------------


async def test_prune_stale_data_removes_repos(engine: AsyncEngine) -> None:
    """Repos not in the active set are fully removed from the DB."""
    # Seed two repos
    for name in ("org/keep", "org/stale"):
        repo = TrackedRepository(full_name=name, default_branch="main", branches=["main"])
        await save_stats_to_db(
            engine,
            [RepositoryStats(full_name=name, default_branch="main")],
            [repo],
        )

    # Only org/keep is active
    active = [TrackedRepository(full_name="org/keep", default_branch="main", branches=["main"])]
    pruned = await prune_stale_data(engine, active)
    assert pruned == 1

    repos, _ = await load_stats_from_db(engine)
    assert [r.full_name for r in repos] == ["org/keep"]


async def test_prune_stale_data_cleans_related_rows(engine: AsyncEngine) -> None:
    """Issues, PRs, workflows, and check results for stale repos are cleaned."""
    repo_name = "org/stale"
    repo = TrackedRepository(full_name=repo_name, default_branch="main", branches=["main"])
    await save_stats_to_db(
        engine,
        [RepositoryStats(full_name=repo_name, default_branch="main")],
        [repo],
    )

    # Insert related rows
    now = datetime(2025, 1, 1, tzinfo=UTC)
    async with AsyncSession(engine) as session:
        session.add(
            CachedIssue(
                repo_full_name=repo_name,
                number=1,
                title="Issue",
                url="https://example.com/1",
                created_at=now,
            )
        )
        session.add(
            CachedPullRequest(
                repo_full_name=repo_name,
                number=2,
                title="PR",
                url="https://example.com/2",
                created_at=now,
            )
        )
        session.add(
            CachedWorkflowStatus(
                repo_full_name=repo_name,
                branch="main",
                workflow_name="CI",
                status="success",
            )
        )
        session.add(
            CheckResultRecord(
                repo_full_name=repo_name,
                check_slug="test-check",
                check_name="Test Check",
                branch="main",
                passed=True,
            )
        )
        await session.commit()

    # Prune with empty active list
    pruned = await prune_stale_data(engine, [])
    assert pruned == 1

    # All related rows should be gone
    async with AsyncSession(engine) as session:
        assert (await session.exec(select(CachedIssue))).all() == []
        assert (await session.exec(select(CachedPullRequest))).all() == []
        assert (await session.exec(select(CachedWorkflowStatus))).all() == []
        assert (await session.exec(select(CheckResultRecord))).all() == []
        assert (await session.exec(select(CachedRepository))).all() == []


async def test_prune_stale_data_removes_excluded_workflows(engine: AsyncEngine) -> None:
    """Workflow statuses excluded by filters are pruned even for active repos."""
    repo_name = "org/repo"
    repo = TrackedRepository(
        full_name=repo_name,
        default_branch="main",
        branches=["main"],
        workflow_exclude=["Nightly *"],
    )
    await save_stats_to_db(
        engine,
        [RepositoryStats(full_name=repo_name, default_branch="main")],
        [repo],
    )

    # Insert workflow rows — one matching exclude, one not
    async with AsyncSession(engine) as session:
        session.add(
            CachedWorkflowStatus(
                repo_full_name=repo_name,
                branch="main",
                workflow_name="CI",
                status="success",
            )
        )
        session.add(
            CachedWorkflowStatus(
                repo_full_name=repo_name,
                branch="main",
                workflow_name="Nightly Tests",
                status="failure",
            )
        )
        await session.commit()

    await prune_stale_data(engine, [repo])

    async with AsyncSession(engine) as session:
        wfs = (await session.exec(select(CachedWorkflowStatus))).all()
        assert len(wfs) == 1
        assert wfs[0].workflow_name == "CI"


async def test_prune_workspace_dirs(engine: AsyncEngine, tmp_path) -> None:
    """Workspace directories for pruned repos are removed from disk."""
    workspace = tmp_path / "workspace"
    # Create dirs for two repos
    (workspace / "org" / "keep").mkdir(parents=True)
    (workspace / "org" / "stale").mkdir(parents=True)
    (workspace / "other" / "gone").mkdir(parents=True)

    active = [TrackedRepository(full_name="org/keep", default_branch="main", branches=["main"])]

    # Seed the stale repos in DB so prune counts them
    for name in ("org/stale", "other/gone"):
        repo = TrackedRepository(full_name=name, default_branch="main", branches=["main"])
        await save_stats_to_db(
            engine,
            [RepositoryStats(full_name=name, default_branch="main")],
            [repo],
        )

    pruned = await prune_stale_data(engine, active, workspace_dir=workspace)
    assert pruned == 2

    # Stale dirs should be gone
    assert (workspace / "org" / "keep").is_dir()
    assert not (workspace / "org" / "stale").exists()
    assert not (workspace / "other").exists()  # empty owner dir removed


# ---------------------------------------------------------------------------
# RefreshProgress tracking
# ---------------------------------------------------------------------------


def test_refresh_progress_initial_state() -> None:
    """Before any refresh, progress should be None and not running."""
    from grimoire.github.service import get_refresh_progress, is_refresh_running

    assert not is_refresh_running()
    assert get_refresh_progress() is None


@respx.mock
async def test_refresh_tracks_progress(client: GitHubClient, engine: AsyncEngine) -> None:
    """Progress should be set during refresh and cleared after."""
    from grimoire.github.service import (
        get_refresh_progress,
        is_refresh_running,
    )

    # Pre-populate cached data
    repos = [TrackedRepository(full_name="myorg/svc", default_branch="main", branches=["main"])]
    now = datetime.now(UTC)
    stats_list = [
        RepositoryStats(
            full_name="myorg/svc", default_branch="main", open_issues=0, fetched_at=now
        )
    ]
    await save_stats_to_db(engine, stats_list, repos)

    # Mock team endpoint returning 304
    respx.get("https://api.github.com/orgs/myorg/teams/backend/repos").mock(
        return_value=httpx.Response(
            304, headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"}
        )
    )
    for pattern in [
        "/repos/myorg/svc/issues",
        "/repos/myorg/svc/pulls",
        "/repos/myorg/svc/actions/workflows",
        "/repos/myorg/svc/branches/main",
        "/repos/myorg/svc/branches",
    ]:
        respx.get(f"https://api.github.com{pattern}").mock(
            return_value=httpx.Response(
                304, headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"}
            )
        )

    config = _make_config(repos=[TeamRepoSource(team="myorg/backend")])
    result_repos, result_stats = await refresh_all_stats(config, client)

    # After refresh completes, progress should be cleared
    assert not is_refresh_running()
    assert get_refresh_progress() is None
    assert len(result_repos) == 1
