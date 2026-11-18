"""Core domain models for Grimoire.

These are *not* database table models — they are Pydantic models used throughout
the application as the shared domain vocabulary. Database models live in ``database.py``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class TrackedRepository(BaseModel):
    """A repository that Grimoire is monitoring."""

    full_name: str  # "owner/repo"
    default_branch: str = "main"
    branches: list[str] = []  # branches to observe; empty → default branch only
    source: str = "static"  # "static" | "team:org/team-name"
    workflow_include: list[str] = []  # glob patterns; empty → include all
    workflow_exclude: list[str] = []  # glob patterns; empty → exclude none


class WorkflowStatus(BaseModel):
    """Status of a single GitHub Actions workflow on a specific branch."""

    name: str
    branch: str
    status: str  # "success" | "failure" | "pending" | "unknown"
    url: str
    run_url: str = ""
    run_id: int | None = None
    run_attempt: int | None = None
    event: str | None = None
    head_sha: str | None = None
    conclusion: str | None = None  # raw GitHub conclusion; None while in progress
    created_at: datetime | None = None
    run_started_at: datetime | None = None
    updated_at: datetime | None = None


class IssueDetail(BaseModel):
    """Summary of an individual issue for display."""

    number: int
    title: str
    url: str
    created_at: datetime
    last_activity_at: datetime | None = None
    author: str = ""


class PullRequestDetail(BaseModel):
    """Summary of an individual pull request for display."""

    number: int
    title: str
    url: str
    created_at: datetime
    last_activity_at: datetime | None = None
    author: str = ""


class OpenPullRequest(BaseModel):
    """An open pull request of a tracked repository, as listed on the PRs page.

    Everything but the CI fields comes from the pulls list. ``ci_state``,
    ``review_decision`` and ``merge_state`` are ``None`` until GraphQL has reported them.
    """

    number: int
    title: str
    url: str
    author: str = ""
    created_at: datetime
    updated_at: datetime | None = None
    draft: bool = False
    labels: list[str] = []
    base_ref: str = ""
    head_ref: str = ""
    head_sha: str = ""
    auto_merge: bool = False
    ci_state: str | None = None  # "success" | "failure" | "pending" | "none"
    review_decision: str | None = None  # "approved" | "changes_requested" | "review_required"
    merge_state: str | None = None  # lower-cased GraphQL MergeStateStatus
    ci_fetched_at: datetime | None = None


class RepositoryStats(BaseModel):
    """Aggregated stats for a tracked repository."""

    full_name: str
    default_branch: str
    open_issues: int = 0
    stale_issues: int = 0
    open_pull_requests: int = 0
    stale_pull_requests: int = 0
    workflows: list[WorkflowStatus] = []
    stale_issue_items: list[IssueDetail] = []
    stale_pr_items: list[PullRequestDetail] = []
    warnings: list[str] = []
    open_pr_items: list[OpenPullRequest] = []
    fetched_at: datetime | None = None
    last_commit_at: datetime | None = None
    total_branches: int = 0
    oldest_issue_created_at: datetime | None = None
    oldest_pr_created_at: datetime | None = None
    issue_created_dates: list[datetime] = []
    pr_created_dates: list[datetime] = []


class CheckResult(BaseModel):
    """Result of running a single check against a repo+branch."""

    check_name: str
    check_slug: str
    repo_full_name: str
    branch: str
    passed: bool
    output: str
    timestamp: datetime


class ActionRepoResult(BaseModel):
    """Result of running an action against a single repo+branch."""

    repo_full_name: str
    branch: str
    passed: bool
    output: str


class ActionRun(BaseModel):
    """Summary of a complete action run across all target repos."""

    action_name: str
    action_slug: str
    triggered_by: str  # "manual" | "cron" | "api"
    started_at: datetime
    finished_at: datetime | None = None
    results: list[ActionRepoResult] = []
