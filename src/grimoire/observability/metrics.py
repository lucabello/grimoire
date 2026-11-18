"""Prometheus metrics for Grimoire.

All metrics are documented in METRICS.md at the repository root.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from grimoire.models import RepositoryStats, TrackedRepository

# ---------------------------------------------------------------------------
# Repository health gauges
# ---------------------------------------------------------------------------

REPOS_TOTAL = Gauge("grimoire_repositories_total", "Number of tracked repos")
OPEN_ISSUES = Gauge("grimoire_open_issues_total", "Open issues", ["repo"])
STALE_ISSUES = Gauge("grimoire_stale_issues_total", "Stale issues", ["repo"])
OPEN_PRS = Gauge("grimoire_open_pull_requests_total", "Open PRs", ["repo"])
STALE_PRS = Gauge("grimoire_stale_pull_requests_total", "Stale PRs", ["repo"])
TOTAL_BRANCHES = Gauge("grimoire_total_branches", "Total branches", ["repo"])
WORKFLOW_STATUS = Gauge(
    "grimoire_workflow_status",
    "Workflow status (1=success, 0=failure)",
    ["repo", "workflow", "branch"],
)
WORKFLOW_FAILURES = Gauge(
    "grimoire_workflow_failures_total",
    "Number of failing workflows",
    ["repo"],
)
SPACE_INFO = Gauge(
    "grimoire_repository_space_info",
    "Space membership of a repository (always 1; join on repo to label other series)",
    ["repo", "space"],
)
CHECK_STATUS = Gauge(
    "grimoire_check_status",
    "Check status (1=pass, 0=fail)",
    ["repo", "check", "branch"],
)
LAST_COMMIT_TIMESTAMP = Gauge(
    "grimoire_last_commit_timestamp_seconds",
    "Unix timestamp of the most recent commit",
    ["repo"],
)
DATA_FETCHED_TIMESTAMP = Gauge(
    "grimoire_data_fetched_timestamp_seconds",
    "Unix timestamp of the last data fetch",
    ["repo"],
)
OLDEST_ISSUE_AGE = Gauge(
    "grimoire_oldest_issue_age_seconds",
    "Age of the oldest open issue in seconds",
    ["repo"],
)
OLDEST_PR_AGE = Gauge(
    "grimoire_oldest_pr_age_seconds",
    "Age of the oldest open pull request in seconds",
    ["repo"],
)

# ---------------------------------------------------------------------------
# Age distribution histograms
# ---------------------------------------------------------------------------

ISSUE_AGE_DISTRIBUTION = Histogram(
    "grimoire_issue_age_seconds",
    "Age distribution of open issues",
    ["repo"],
    buckets=[86400, 604800, 2592000, 7776000, 15552000, 31536000, float("inf")],
    #        1d     7d      30d      90d      180d      365d      inf
)
PR_AGE_DISTRIBUTION = Histogram(
    "grimoire_pr_age_seconds",
    "Age distribution of open pull requests",
    ["repo"],
    buckets=[3600, 86400, 604800, 1209600, 2592000, 7776000, float("inf")],
    #        1h    1d     7d      14d      30d      90d      inf
)

# ---------------------------------------------------------------------------
# Performance histograms
# ---------------------------------------------------------------------------

CHECK_DURATION = Histogram(
    "grimoire_check_run_duration_seconds", "Check execution time", ["check"]
)
ACTION_DURATION = Histogram(
    "grimoire_action_run_duration_seconds", "Action execution time", ["action"]
)
DATA_REFRESH_DURATION = Histogram(
    "grimoire_data_refresh_duration_seconds", "Full data refresh cycle time"
)

# ---------------------------------------------------------------------------
# GitHub API metrics
# ---------------------------------------------------------------------------

API_REQUESTS = Counter(
    "grimoire_github_api_requests_total", "Total API calls", ["endpoint", "status"]
)
RATE_LIMIT_REMAINING = Gauge("grimoire_github_api_rate_limit_remaining", "Rate limit remaining")
RATE_LIMIT_RESET = Gauge("grimoire_github_api_rate_limit_reset", "Rate limit reset timestamp")

# ---------------------------------------------------------------------------
# /metrics endpoint
# ---------------------------------------------------------------------------

router = APIRouter(tags=["observability"])


@router.get("/metrics")
async def metrics() -> Response:
    """Expose Prometheus metrics."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def update_repo_metrics(stats_list: list[RepositoryStats]) -> None:
    """Update all per-repo gauge metrics after a data refresh."""
    now = datetime.now(UTC)
    REPOS_TOTAL.set(len(stats_list))
    for stats in stats_list:
        repo = stats.full_name
        OPEN_ISSUES.labels(repo=repo).set(stats.open_issues)
        STALE_ISSUES.labels(repo=repo).set(stats.stale_issues)
        OPEN_PRS.labels(repo=repo).set(stats.open_pull_requests)
        STALE_PRS.labels(repo=repo).set(stats.stale_pull_requests)
        TOTAL_BRANCHES.labels(repo=repo).set(stats.total_branches)
        for wf in stats.workflows:
            val = 1 if wf.status == "success" else 0
            WORKFLOW_STATUS.labels(repo=repo, workflow=wf.name, branch=wf.branch).set(val)
        workflow_failures = sum(1 for wf in stats.workflows if wf.status == "failure")
        WORKFLOW_FAILURES.labels(repo=repo).set(workflow_failures)
        if stats.last_commit_at is not None:
            LAST_COMMIT_TIMESTAMP.labels(repo=repo).set(stats.last_commit_at.timestamp())
        if stats.fetched_at is not None:
            DATA_FETCHED_TIMESTAMP.labels(repo=repo).set(stats.fetched_at.timestamp())

        if stats.oldest_issue_created_at is not None:
            age = (now - stats.oldest_issue_created_at).total_seconds()
            OLDEST_ISSUE_AGE.labels(repo=repo).set(age)

        if stats.oldest_pr_created_at is not None:
            age = (now - stats.oldest_pr_created_at).total_seconds()
            OLDEST_PR_AGE.labels(repo=repo).set(age)

        for created_at in stats.issue_created_dates:
            age = (now - created_at).total_seconds()
            ISSUE_AGE_DISTRIBUTION.labels(repo=repo).observe(age)

        for created_at in stats.pr_created_dates:
            age = (now - created_at).total_seconds()
            PR_AGE_DISTRIBUTION.labels(repo=repo).observe(age)


def update_space_metrics(repos: list[TrackedRepository]) -> None:
    """Publish one ``grimoire_repository_space_info`` series per (repo, space) membership.

    Repos without a space get no series. The previous label sets are cleared first so that repos
    removed from a space disappear.
    """
    SPACE_INFO.clear()
    for repo in repos:
        for space in repo.spaces:
            SPACE_INFO.labels(repo=repo.full_name, space=space).set(1)


def update_check_metrics(
    check_slug: str, repo: str, branch: str, passed: bool, duration_seconds: float
) -> None:
    """Update check metrics after execution."""
    CHECK_STATUS.labels(repo=repo, check=check_slug, branch=branch).set(1 if passed else 0)
    CHECK_DURATION.labels(check=check_slug).observe(duration_seconds)


def update_action_metrics(action_slug: str, duration_seconds: float) -> None:
    """Update action metrics after execution."""
    ACTION_DURATION.labels(action=action_slug).observe(duration_seconds)


def update_rate_limit_metrics(remaining: int, reset_timestamp: int) -> None:
    """Update GitHub rate limit metrics."""
    RATE_LIMIT_REMAINING.set(remaining)
    RATE_LIMIT_RESET.set(reset_timestamp)
