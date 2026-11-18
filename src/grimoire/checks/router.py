"""REST API router for checks."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from grimoire.checks.engine import is_check_running, run_check_for_all_targets
from grimoire.database import CheckResultRecord, CheckRunRecord, CheckToggleRecord
from grimoire.spaces import SpaceSelection, api_space_selection

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from grimoire.checks.loader import CheckDefinition
    from grimoire.models import TrackedRepository
    from grimoire.workspace.manager import WorkspaceManager

# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class CheckListItem(BaseModel):
    name: str
    slug: str
    description: str
    schedule: str | None
    enabled: bool
    target_count: int
    spaces: list[str] = []


class CheckResultResponse(BaseModel):
    check_name: str
    check_slug: str
    repo_full_name: str
    branch: str
    passed: bool
    output: str
    timestamp: datetime


class CheckRepoResultResponse(BaseModel):
    repo_full_name: str
    branch: str
    passed: bool
    output: str


class CheckRunSummary(BaseModel):
    id: int
    check_slug: str
    check_name: str
    triggered_by: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    total_repos: int
    passed_repos: int


class CheckRunDetail(BaseModel):
    id: int
    check_slug: str
    check_name: str
    triggered_by: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    results: list[CheckRepoResultResponse]


# ---------------------------------------------------------------------------
# Module-level state — set from outside
# ---------------------------------------------------------------------------

_checks: list[CheckDefinition] = []
_repos: list[TrackedRepository] = []
_workspace: WorkspaceManager | None = None
_engine: AsyncEngine | None = None


def set_checks_state(
    checks: list[CheckDefinition],
    repos: list[TrackedRepository],
    workspace: WorkspaceManager,
    engine: AsyncEngine,
) -> None:
    """Inject dependencies into the checks router."""
    global _checks, _repos, _workspace, _engine  # noqa: PLW0603
    _checks = checks
    _repos = repos
    _workspace = workspace
    _engine = engine


def _find_check(slug: str) -> CheckDefinition:
    for c in _checks:
        if c.slug == slug:
            return c
    raise HTTPException(status_code=404, detail=f"Check '{slug}' not found")


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/checks", tags=["checks"])


@router.get("/", response_model=list[CheckListItem])
async def list_checks(
    selection: SpaceSelection = Depends(api_space_selection),
) -> list[CheckListItem]:
    """List the check definitions with enabled status.

    ``?spaces=`` keeps the checks listed under those spaces plus the global ones (no ``spaces``).
    Targeting is unaffected.
    """
    items: list[CheckListItem] = []
    for c in _checks:
        if not selection.lists(c.spaces):
            continue
        items.append(
            CheckListItem(
                name=c.name,
                slug=c.slug,
                description=c.description,
                schedule=c.schedule,
                enabled=c.enabled,
                target_count=0,
                spaces=c.spaces,
            )
        )
    return items


@router.get("/{slug}/results", response_model=list[CheckResultResponse])
async def get_check_results(
    slug: str, selection: SpaceSelection = Depends(api_space_selection)
) -> list[CheckResultResponse]:
    """Get latest results for a check, grouped by repo+branch (``?spaces=`` filters the repos)."""
    _find_check(slug)
    assert _engine is not None

    query = text(
        "SELECT cr.check_name, cr.check_slug, cr.repo_full_name, cr.branch, "
        "cr.passed, cr.output, cr.timestamp "
        "FROM check_result cr "
        "INNER JOIN ("
        "  SELECT repo_full_name, branch, MAX(timestamp) AS max_ts "
        "  FROM check_result WHERE check_slug = :slug "
        "  GROUP BY repo_full_name, branch"
        ") latest ON cr.repo_full_name = latest.repo_full_name "
        "AND cr.branch = latest.branch "
        "AND cr.timestamp = latest.max_ts "
        "AND cr.check_slug = :slug"
    )

    async with AsyncSession(_engine) as session:
        rows = (await session.exec(query, params={"slug": slug})).all()  # type: ignore[call-arg]

    visible = selection.repo_name_filter()
    return [
        CheckResultResponse(
            check_name=row[0],
            check_slug=row[1],
            repo_full_name=row[2],
            branch=row[3],
            passed=bool(row[4]),
            output=row[5],
            timestamp=row[6],
        )
        for row in rows
        if visible(row[2])
    ]


@router.get("/{slug}/runs", response_model=list[CheckRunSummary])
async def list_check_runs(slug: str) -> list[CheckRunSummary]:
    """List run history for a check (reverse chronological)."""
    _find_check(slug)
    assert _engine is not None

    async with AsyncSession(_engine) as session:
        stmt = (
            select(CheckRunRecord)
            .where(CheckRunRecord.check_slug == slug)
            .order_by(CheckRunRecord.started_at.desc())  # type: ignore[union-attr]
        )
        runs = (await session.exec(stmt)).all()

        summaries: list[CheckRunSummary] = []
        for run in runs:
            repo_stmt = select(CheckResultRecord).where(CheckResultRecord.run_id == run.id)
            repo_results = (await session.exec(repo_stmt)).all()
            total = len(repo_results)
            passed = sum(1 for r in repo_results if r.passed)
            summaries.append(
                CheckRunSummary(
                    id=run.id,  # type: ignore[arg-type]
                    check_slug=run.check_slug,
                    check_name=run.check_name,
                    triggered_by=run.triggered_by,
                    status=run.status,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    total_repos=total,
                    passed_repos=passed,
                )
            )

    return summaries


@router.get("/{slug}/runs/{run_id}", response_model=CheckRunDetail)
async def get_check_run(slug: str, run_id: int) -> CheckRunDetail:
    """Get a specific run with per-repo results."""
    _find_check(slug)
    assert _engine is not None

    async with AsyncSession(_engine) as session:
        stmt = select(CheckRunRecord).where(
            CheckRunRecord.id == run_id,
            CheckRunRecord.check_slug == slug,
        )
        run = (await session.exec(stmt)).first()
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

        result_stmt = select(CheckResultRecord).where(CheckResultRecord.run_id == run_id)
        results = (await session.exec(result_stmt)).all()

    return CheckRunDetail(
        id=run.id,  # type: ignore[arg-type]
        check_slug=run.check_slug,
        check_name=run.check_name,
        triggered_by=run.triggered_by,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        results=[
            CheckRepoResultResponse(
                repo_full_name=r.repo_full_name,
                branch=r.branch,
                passed=r.passed,
                output=r.output,
            )
            for r in results
        ],
    )


@router.post("/{slug}/run", response_model=CheckRunDetail)
async def run_check_endpoint(
    slug: str,
    repo: str | None = None,
    background_tasks: BackgroundTasks = BackgroundTasks(),
) -> CheckRunDetail:
    """Trigger a check run. Optionally filter to a single repo.

    Returns immediately with status ``"running"``. The check runs in the
    background — poll ``is_check_running()`` or the run-status partial for
    completion.
    """
    check = _find_check(slug)
    assert _workspace is not None
    assert _engine is not None

    if is_check_running(slug):
        raise HTTPException(status_code=409, detail="Check is already running")

    workspace = _workspace
    engine = _engine

    async def _run_in_background() -> None:
        await run_check_for_all_targets(
            check, _repos, workspace, engine, specific_repo=repo, triggered_by="manual"
        )

    background_tasks.add_task(_run_in_background)

    return CheckRunDetail(
        id=0,
        check_slug=slug,
        check_name=check.name,
        triggered_by="manual",
        status="running",
        started_at=datetime.now(timezone.utc),
        finished_at=None,
        results=[],
    )


@router.post("/{slug}/toggle")
async def toggle_check(slug: str) -> dict[str, object]:
    """Toggle a check enabled/disabled and persist the state."""
    check = _find_check(slug)
    assert _engine is not None

    check.enabled = not check.enabled

    async with AsyncSession(_engine) as session:
        existing = await session.get(CheckToggleRecord, slug)
        if existing:
            existing.enabled = check.enabled
            session.add(existing)
        else:
            session.add(CheckToggleRecord(check_slug=slug, enabled=check.enabled))
        await session.commit()

    return {"slug": slug, "enabled": check.enabled}


@router.get("/{slug}/status")
async def check_status(slug: str) -> dict[str, object]:
    """Return whether a check is currently running."""
    _find_check(slug)
    return {"slug": slug, "running": is_check_running(slug)}
