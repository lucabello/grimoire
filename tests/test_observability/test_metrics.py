"""Tests for Prometheus metrics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from prometheus_client import REGISTRY

from grimoire.models import RepoMembership, RepositoryStats, TrackedRepository, WorkflowStatus
from grimoire.observability.metrics import (
    DATA_FETCHED_TIMESTAMP,
    LAST_COMMIT_TIMESTAMP,
    OLDEST_ISSUE_AGE,
    OLDEST_PR_AGE,
    OPEN_ISSUES,
    OPEN_PRS,
    REPOS_TOTAL,
    STALE_ISSUES,
    STALE_PRS,
    TOTAL_BRANCHES,
    WORKFLOW_FAILURES,
    WORKFLOW_STATUS,
    update_check_metrics,
    update_repo_metrics,
    update_space_metrics,
)


async def test_metrics_endpoint_returns_200(async_client: AsyncClient) -> None:
    """GET /metrics returns 200 with prometheus content type."""
    resp = await async_client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")
    # Should contain at least one of our custom metrics
    assert b"grimoire_" in resp.content


async def test_update_repo_metrics_sets_gauges() -> None:
    """update_repo_metrics correctly sets all per-repo gauges."""
    last_commit = datetime(2025, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    fetched = datetime(2025, 4, 10, 8, 0, 0, tzinfo=timezone.utc)
    stats_list = [
        RepositoryStats(
            full_name="org/alpha",
            default_branch="main",
            open_issues=5,
            stale_issues=2,
            open_pull_requests=3,
            stale_pull_requests=1,
            total_branches=8,
            last_commit_at=last_commit,
            fetched_at=fetched,
            workflows=[
                WorkflowStatus(
                    name="CI",
                    branch="main",
                    status="success",
                    url="https://example.com",
                ),
                WorkflowStatus(
                    name="Deploy",
                    branch="main",
                    status="failure",
                    url="https://example.com",
                ),
            ],
        ),
        RepositoryStats(
            full_name="org/beta",
            default_branch="main",
            open_issues=10,
            stale_issues=0,
            open_pull_requests=0,
            stale_pull_requests=0,
        ),
    ]

    update_repo_metrics(stats_list)

    assert REPOS_TOTAL._value.get() == 2  # type: ignore[union-attr]
    assert OPEN_ISSUES.labels(repo="org/alpha")._value.get() == 5  # type: ignore[union-attr]
    assert STALE_ISSUES.labels(repo="org/alpha")._value.get() == 2  # type: ignore[union-attr]
    assert OPEN_PRS.labels(repo="org/alpha")._value.get() == 3  # type: ignore[union-attr]
    assert STALE_PRS.labels(repo="org/alpha")._value.get() == 1  # type: ignore[union-attr]
    assert OPEN_ISSUES.labels(repo="org/beta")._value.get() == 10  # type: ignore[union-attr]

    # Branch gauges
    assert TOTAL_BRANCHES.labels(repo="org/alpha")._value.get() == 8  # type: ignore[union-attr]

    # Workflow gauges
    assert (
        WORKFLOW_STATUS.labels(repo="org/alpha", workflow="CI", branch="main")._value.get() == 1  # type: ignore[union-attr]
    )
    assert (
        WORKFLOW_STATUS.labels(repo="org/alpha", workflow="Deploy", branch="main")._value.get()
        == 0  # type: ignore[union-attr]
    )
    assert WORKFLOW_FAILURES.labels(repo="org/alpha")._value.get() == 1  # type: ignore[union-attr]
    assert WORKFLOW_FAILURES.labels(repo="org/beta")._value.get() == 0  # type: ignore[union-attr]

    # Timestamp gauges
    assert LAST_COMMIT_TIMESTAMP.labels(repo="org/alpha")._value.get() == last_commit.timestamp()  # type: ignore[union-attr]
    assert DATA_FETCHED_TIMESTAMP.labels(repo="org/alpha")._value.get() == fetched.timestamp()  # type: ignore[union-attr]


async def test_update_check_metrics() -> None:
    """update_check_metrics sets status gauge and records histogram."""
    update_check_metrics(
        check_slug="lint",
        repo="org/alpha",
        branch="main",
        passed=True,
        duration_seconds=1.5,
    )

    # Verify the check status gauge was set
    from grimoire.observability.metrics import CHECK_STATUS

    val = CHECK_STATUS.labels(repo="org/alpha", check="lint", branch="main")._value.get()  # type: ignore[union-attr]
    assert val == 1

    # Verify histogram recorded a sample
    sample = REGISTRY.get_sample_value(
        "grimoire_check_run_duration_seconds_count", {"check": "lint"}
    )
    assert sample is not None
    assert sample >= 1


async def test_update_repo_metrics_sets_age_gauges_and_histograms() -> None:
    """update_repo_metrics correctly sets oldest age gauges and observes histograms."""
    now = datetime.now(timezone.utc)
    oldest_issue = now - timedelta(days=100)
    recent_issue = now - timedelta(days=5)
    oldest_pr = now - timedelta(days=30)
    recent_pr = now - timedelta(hours=12)

    stats_list = [
        RepositoryStats(
            full_name="org/gamma",
            default_branch="main",
            open_issues=2,
            open_pull_requests=2,
            oldest_issue_created_at=oldest_issue,
            oldest_pr_created_at=oldest_pr,
            issue_created_dates=[oldest_issue, recent_issue],
            pr_created_dates=[oldest_pr, recent_pr],
        ),
    ]

    update_repo_metrics(stats_list)

    oldest_issue_age = OLDEST_ISSUE_AGE.labels(repo="org/gamma")._value.get()  # type: ignore[union-attr]
    oldest_pr_age = OLDEST_PR_AGE.labels(repo="org/gamma")._value.get()  # type: ignore[union-attr]

    assert oldest_issue_age > 0
    assert oldest_issue_age >= timedelta(days=99).total_seconds()
    assert oldest_pr_age > 0
    assert oldest_pr_age >= timedelta(days=29).total_seconds()

    issue_hist_count = REGISTRY.get_sample_value(
        "grimoire_issue_age_seconds_count", {"repo": "org/gamma"}
    )
    pr_hist_count = REGISTRY.get_sample_value(
        "grimoire_pr_age_seconds_count", {"repo": "org/gamma"}
    )
    assert issue_hist_count is not None
    assert issue_hist_count >= 2
    assert pr_hist_count is not None
    assert pr_hist_count >= 2


def _space_series() -> set[tuple[str, str]]:
    series = set()
    for family in REGISTRY.collect():
        if family.name == "grimoire_repository_space_info":
            for sample in family.samples:
                assert sample.value == 1
                series.add((sample.labels["repo"], sample.labels["space"]))
    return series


def test_space_info_has_one_series_per_membership() -> None:
    repos = [
        TrackedRepository(
            full_name="org/shared",
            memberships=[RepoMembership(), RepoMembership(space="a"), RepoMembership(space="b")],
        ),
        TrackedRepository(full_name="org/solo", memberships=[RepoMembership(space="a")]),
        TrackedRepository(full_name="org/loose", memberships=[RepoMembership()]),
    ]

    update_space_metrics(repos)

    # The overlapping repo yields two series; the unassigned repo none.
    assert _space_series() == {("org/shared", "a"), ("org/shared", "b"), ("org/solo", "a")}


def test_space_info_series_of_removed_memberships_disappear() -> None:
    shared = TrackedRepository(
        full_name="org/shared",
        memberships=[RepoMembership(space="a"), RepoMembership(space="b")],
    )
    gone = TrackedRepository(full_name="org/gone", memberships=[RepoMembership(space="a")])
    update_space_metrics([shared, gone])
    assert ("org/gone", "a") in _space_series()

    shared_after = shared.model_copy(update={"memberships": [RepoMembership(space="a")]})
    update_space_metrics([shared_after])

    assert _space_series() == {("org/shared", "a")}


def test_space_info_is_exposed_on_the_metrics_endpoint() -> None:
    update_space_metrics(
        [TrackedRepository(full_name="org/x", memberships=[RepoMembership(space="a")])]
    )
    from prometheus_client import generate_latest

    assert b'grimoire_repository_space_info{repo="org/x",space="a"} 1.0' in generate_latest()
    update_space_metrics([])
