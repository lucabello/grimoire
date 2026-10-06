"""Pydantic response models for the GitHub-related REST API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class IssueResponse(BaseModel):
    """Summary of an open issue."""

    number: int
    title: str
    url: str
    created_at: datetime | None = None
    last_comment_at: datetime | None = None
    stale: bool = False


class PullRequestResponse(BaseModel):
    """Summary of an open pull request."""

    number: int
    title: str
    url: str
    author: str = ""
    created_at: datetime | None = None
    last_push_at: datetime | None = None
    last_comment_at: datetime | None = None
    stale: bool = False


class WorkflowStatusResponse(BaseModel):
    """Workflow status information."""

    name: str
    branch: str
    status: str
    url: str
    run_url: str = ""


class RepoSummary(BaseModel):
    """Condensed view of a tracked repository for list responses."""

    full_name: str
    default_branch: str
    branches: list[str] = []
    sources: list[str] = ["static"]
    source: str = "static"  # primary (first) source — backward compatible
    open_issues: int = 0
    stale_issues: int = 0
    open_pull_requests: int = 0
    stale_pull_requests: int = 0
    workflow_failures: int = 0
    last_commit_at: datetime | None = None
    total_branches: int = 0
    warnings: list[str] = []
    fetched_at: datetime | None = None


class RepoListResponse(BaseModel):
    """Response for GET /api/repos."""

    repositories: list[RepoSummary]
    last_refresh: datetime | None = None


class RepoDetailResponse(BaseModel):
    """Response for GET /api/repos/{owner}/{name}."""

    full_name: str
    default_branch: str
    branches: list[str] = []
    sources: list[str] = ["static"]
    source: str = "static"  # primary (first) source — backward compatible
    open_issues: int = 0
    stale_issues: int = 0
    open_pull_requests: int = 0
    stale_pull_requests: int = 0
    last_commit_at: datetime | None = None
    total_branches: int = 0
    workflows: list[WorkflowStatusResponse] = []
    warnings: list[str] = []
    fetched_at: datetime | None = None


class RefreshResponse(BaseModel):
    """Response for POST /api/refresh."""

    status: str
    message: str


class RefreshStatusResponse(BaseModel):
    """Response for GET /api/refresh/status."""

    running: bool
    completed: int = 0
    total: int = 0
