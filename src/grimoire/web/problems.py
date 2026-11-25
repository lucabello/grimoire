"""Problems: a structured list of what is currently wrong, read from the cache and the DB."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from grimoire.spaces import ALL_SPACES, SpaceSelection, api_space_selection
from grimoire.web.router import _LATEST_RESULTS_FROM, _base_path

if TYPE_CHECKING:
    from grimoire.checks.loader import CheckDefinition
    from grimoire.models import RepositoryStats, TrackedRepository

# Failures show at the end of a script's output, so the tail is kept.
OUTPUT_LIMIT = 2000


class ProblemKind(str, Enum):
    """The kinds of problem; fields of a detail are only ever added, never renamed."""

    WORKFLOW_FAILURE = "workflow_failure"
    PR_CI_FAILURE = "pr_ci_failure"
    CHECK_FAILURE = "check_failure"
    REPO_WARNING = "repo_warning"


class WorkflowFailureDetail(BaseModel):
    kind: Literal["workflow_failure"] = "workflow_failure"
    run_id: int | None
    run_attempt: int | None
    event: str | None
    conclusion: str | None
    head_sha: str | None
    run_url: str


class PRCIFailureDetail(BaseModel):
    kind: Literal["pr_ci_failure"] = "pr_ci_failure"
    number: int
    author: str
    draft: bool
    labels: list[str]
    review_decision: str | None
    merge_state: str | None
    auto_merge: bool
    head_sha: str
    ci_url: str
    ci_checked_at: datetime | None


class CheckFailureDetail(BaseModel):
    kind: Literal["check_failure"] = "check_failure"
    check_slug: str
    output: str
    output_truncated: bool
    results_path: str
    timestamp: datetime | None


class RepoWarningDetail(BaseModel):
    kind: Literal["repo_warning"] = "repo_warning"
    warning: str


ProblemDetail = Annotated[
    WorkflowFailureDetail | PRCIFailureDetail | CheckFailureDetail | RepoWarningDetail,
    Field(discriminator="kind"),
]


class Problem(BaseModel):
    kind: ProblemKind
    severity: Literal["error", "warning"]
    repo: str
    branch: str | None
    subject: str
    summary: str
    url: str | None
    observed_at: datetime | None
    detail: ProblemDetail


class ProblemCounts(BaseModel):
    """Problems per kind (after the space filter, before the kind filter); every kind is listed."""

    workflow_failure: int = 0
    pr_ci_failure: int = 0
    check_failure: int = 0
    repo_warning: int = 0


class ProblemsResponse(BaseModel):
    last_refresh: datetime | None
    counts: ProblemCounts
    problems: list[Problem]


@dataclass(frozen=True)
class FailedCheck:
    """The latest result of a check on a repo+branch, when it failed."""

    output: str  # at most the last OUTPUT_LIMIT characters
    output_length: int
    timestamp: datetime | None


def _as_utc(value: datetime | str | None) -> datetime | None:
    """A DB timestamp (naive UTC, a string through raw SQL) as an aware datetime."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _problem(
    detail: WorkflowFailureDetail | PRCIFailureDetail | CheckFailureDetail | RepoWarningDetail,
    *,
    severity: Literal["error", "warning"],
    repo: str,
    branch: str | None,
    subject: str,
    summary: str,
    url: str | None,
    observed_at: datetime | None,
) -> Problem:
    return Problem(
        kind=ProblemKind(detail.kind),
        severity=severity,
        repo=repo,
        branch=branch,
        subject=subject,
        summary=summary,
        url=url,
        observed_at=observed_at,
        detail=detail,
    )


def _workflow_problems(
    stats: RepositoryStats, repo: TrackedRepository, selection: SpaceSelection
) -> list[Problem]:
    return [
        _problem(
            WorkflowFailureDetail(
                run_id=wf.run_id,
                run_attempt=wf.run_attempt,
                event=wf.event,
                conclusion=wf.conclusion,
                head_sha=wf.head_sha,
                run_url=wf.run_url,
            ),
            severity="error",
            repo=repo.full_name,
            branch=wf.branch,
            subject=wf.name,
            summary=f"Workflow '{wf.name}' failing on {wf.branch}",
            url=wf.run_url or wf.url,
            observed_at=wf.run_started_at or wf.created_at,
        )
        for wf in stats.workflows
        if wf.status == "failure" and selection.workflow_visible(repo, wf.name)
    ]


def _pr_problems(stats: RepositoryStats, repo: TrackedRepository) -> list[Problem]:
    return [
        _problem(
            PRCIFailureDetail(
                number=pr.number,
                author=pr.author,
                draft=pr.draft,
                labels=pr.labels,
                review_decision=pr.review_decision,
                merge_state=pr.merge_state,
                auto_merge=pr.auto_merge,
                head_sha=pr.head_sha,
                ci_url=f"{pr.url}/checks",
                ci_checked_at=pr.ci_fetched_at,
            ),
            severity="error",
            repo=repo.full_name,
            branch=pr.head_ref or None,
            subject=f"#{pr.number} {pr.title}",
            summary=f"CI failing on PR #{pr.number}: {pr.title}",
            url=pr.url,
            observed_at=pr.ci_fetched_at,
        )
        for pr in stats.open_pr_items
        if pr.ci_state == "failure"
    ]


def _check_problems(
    stats: RepositoryStats,
    repo: TrackedRepository,
    check_defs: Iterable[CheckDefinition],
    failing_checks: Mapping[tuple[str, str, str], FailedCheck],
    base_path: str,
) -> list[Problem]:
    problems: list[Problem] = []
    for check_def in check_defs:
        if not check_def.enabled:
            continue
        for branch in repo.branches or [stats.default_branch]:
            failed = failing_checks.get((check_def.slug, repo.full_name, branch))
            if failed is None:
                continue
            problems.append(
                _problem(
                    CheckFailureDetail(
                        check_slug=check_def.slug,
                        output=failed.output,
                        output_truncated=failed.output_length > OUTPUT_LIMIT,
                        results_path=f"{base_path}/api/checks/{check_def.slug}/results",
                        timestamp=failed.timestamp,
                    ),
                    severity=check_def.severity,
                    repo=repo.full_name,
                    branch=branch,
                    subject=check_def.name,
                    summary=f"Check '{check_def.name}' failing on {branch}",
                    url=None,
                    observed_at=failed.timestamp,
                )
            )
    return problems


def _warning_problems(stats: RepositoryStats, repo: TrackedRepository) -> list[Problem]:
    return [
        _problem(
            RepoWarningDetail(warning=warning),
            severity="warning",
            repo=repo.full_name,
            branch=None,
            subject="Repository warning",
            summary=warning,
            url=None,
            observed_at=stats.fetched_at,
        )
        for warning in stats.warnings
    ]


def collect_problems(
    cache: Mapping[str, RepositoryStats],
    repos: Mapping[str, TrackedRepository],
    check_defs: Iterable[CheckDefinition],
    failing_checks: Mapping[tuple[str, str, str], FailedCheck],
    selection: SpaceSelection = ALL_SPACES,
    base_path: str = "",
) -> list[Problem]:
    """Every current problem of the repos in the space *selection*, by repository name.

    *failing_checks* maps ``(check_slug, repo, branch)`` to the latest result when it failed.
    Like the backlog, checks count only when enabled and only on the tracked branches.
    """
    check_defs = list(check_defs)
    problems: list[Problem] = []
    for full_name in sorted(cache):
        repo = repos.get(full_name)
        if repo is None or not selection.includes(repo):
            continue
        stats = cache[full_name]
        problems += _workflow_problems(stats, repo, selection)
        problems += _pr_problems(stats, repo)
        problems += _check_problems(stats, repo, check_defs, failing_checks, base_path)
        problems += _warning_problems(stats, repo)
    return problems


async def load_failing_checks() -> dict[tuple[str, str, str], FailedCheck]:
    """The latest result of each (check, repo, branch) whose latest result failed.

    The output is cut to its last ``OUTPUT_LIMIT`` characters in SQL, so large outputs are
    never loaded.
    """
    from grimoire.checks.router import _checks, _engine

    if _engine is None or not _checks:
        return {}
    query = text(
        "SELECT cr.check_slug, cr.repo_full_name, cr.branch, "
        f"substr(cr.output, -{OUTPUT_LIMIT}), length(cr.output), cr.timestamp "
        + _LATEST_RESULTS_FROM
        + " WHERE cr.passed = 0"
    )
    async with AsyncSession(_engine) as session:
        rows = (await session.exec(query)).all()  # type: ignore[call-overload]
    return {
        (slug, repo, branch): FailedCheck(output or "", length or 0, _as_utc(timestamp))
        for slug, repo, branch, output, length, timestamp in rows
    }


def _parse_kinds(raw: str) -> set[str]:
    kinds = {part for part in (p.strip() for p in raw.split(",")) if part}
    unknown = kinds - {kind.value for kind in ProblemKind}
    if unknown:
        known = ", ".join(kind.value for kind in ProblemKind)
        raise HTTPException(
            status_code=422,
            detail=f"Unknown kind: {', '.join(sorted(unknown))} (known: {known})",
        )
    return kinds


router = APIRouter(tags=["problems"])


@router.get("/problems", response_model=ProblemsResponse)
async def list_problems(
    request: Request,
    kind: str = Query(
        "",
        description="Comma-separated kinds to keep (workflow_failure, pr_ci_failure, "
        "check_failure, repo_warning); omit for all. An unknown kind is a 422.",
    ),
    selection: SpaceSelection = Depends(api_space_selection),
) -> ProblemsResponse:
    """What is currently wrong, from the cache and the latest check results (no GitHub calls).

    ``counts`` covers the selected spaces and ignores ``?kind=``. ``last_refresh`` is ``null``
    until the first refresh: an empty list then means no data yet, not healthy. The selection
    cookie is never read.
    """
    from grimoire.checks.router import _checks
    from grimoire.github.router import _cache, _last_refresh, _repos

    kinds = _parse_kinds(kind)
    problems = collect_problems(
        _cache,
        _repos,
        _checks,
        await load_failing_checks(),
        selection,
        _base_path(request),
    )
    counts = Counter(problem.kind.value for problem in problems)
    return ProblemsResponse(
        last_refresh=_last_refresh,
        counts=ProblemCounts(**counts),
        problems=[p for p in problems if not kinds or p.kind.value in kinds],
    )
