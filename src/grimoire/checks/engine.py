"""Check execution engine."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlmodel.ext.asyncio.session import AsyncSession

from grimoire.database import CheckResultRecord, CheckRunRecord
from grimoire.models import CheckResult
from grimoire.observability.metrics import update_check_metrics
from grimoire.script import create_script_process, kill_process
from grimoire.targeting import resolve_targets, target_env

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from grimoire.checks.loader import CheckDefinition
    from grimoire.models import TrackedRepository
    from grimoire.workspace.manager import WorkspaceManager

OUTPUT_SIZE_CAP = 64 * 1024  # 64 KB
_DEFAULT_TIMEOUT = 300  # 5 minutes
_CONCURRENCY = 5


@dataclass
class CheckProgress:
    """Tracks execution progress of a running check."""

    completed: int = 0
    total: int = 0


# In-memory tracking of currently-running check slugs and their progress
_running_checks: dict[str, CheckProgress] = {}


def is_check_running(slug: str) -> bool:
    """Return True if the check is currently executing."""
    return slug in _running_checks


def get_check_progress(slug: str) -> CheckProgress | None:
    """Return the progress tracker for a running check, or None if idle."""
    return _running_checks.get(slug)


async def run_check(
    check: CheckDefinition,
    repo: TrackedRepository,
    branch: str,
    workspace: WorkspaceManager,
    engine: AsyncEngine,
    run_id: int | None = None,
) -> CheckResult:
    """Run a single check script against a repo+branch and persist the result."""
    passed = False
    output = ""
    start_time = time.monotonic()

    try:
        workdir = await workspace.reset_workdir(repo.full_name, branch)
    except Exception as exc:
        output = f"Workspace setup failed: {exc}"
        return await _persist_result(check, repo, branch, passed, output, engine, run_id)

    env = target_env(workspace, repo, branch)

    # Give each check subprocess its own temp/runtime directory to avoid
    # conflicts when tools (e.g. charmcraft) use shared state files.
    tmpdir = tempfile.mkdtemp(prefix="grimoire-check-")
    env["TMPDIR"] = tmpdir
    env["XDG_RUNTIME_DIR"] = tmpdir

    proc: asyncio.subprocess.Process | None = None
    tmp_script = None
    try:
        proc, tmp_script = await create_script_process(
            check.script,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=_DEFAULT_TIMEOUT
        )
        stdout_text = stdout_bytes.decode(errors="replace").rstrip()
        stderr_text = stderr_bytes.decode(errors="replace").rstrip()
        passed = proc.returncode == 0

        # Build combined output with clear sections
        parts: list[str] = []
        if stdout_text:
            parts.append(stdout_text)
        if stderr_text:
            if stdout_text:
                parts.append("")
            parts.append(f"[stderr]\n{stderr_text}")
        if not passed:
            parts.append(f"\n[exit code {proc.returncode}]")
        output = "\n".join(parts)
    except asyncio.TimeoutError:
        output = f"Timed out after {_DEFAULT_TIMEOUT}s"
        passed = False
    except OSError as exc:
        output = f"Failed to execute: {exc}"
        passed = False
    finally:
        if proc is not None:
            await kill_process(proc)
        if tmp_script is not None:
            tmp_script.unlink(missing_ok=True)
        shutil.rmtree(tmpdir, ignore_errors=True)

    # Cap output
    if len(output) > OUTPUT_SIZE_CAP:
        output = "[output truncated — showing last 64KB]\n" + output[-OUTPUT_SIZE_CAP:]

    duration = time.monotonic() - start_time
    update_check_metrics(check.slug, repo.full_name, branch, passed, duration)

    return await _persist_result(check, repo, branch, passed, output, engine, run_id)


async def _persist_result(
    check: CheckDefinition,
    repo: TrackedRepository,
    branch: str,
    passed: bool,
    output: str,
    engine: AsyncEngine,
    run_id: int | None = None,
) -> CheckResult:
    """Create a CheckResult, persist it to the database, and return it."""
    now = datetime.now(timezone.utc)
    result = CheckResult(
        check_name=check.name,
        check_slug=check.slug,
        repo_full_name=repo.full_name,
        branch=branch,
        passed=passed,
        output=output,
        timestamp=now,
    )

    record = CheckResultRecord(
        run_id=run_id,
        check_slug=check.slug,
        check_name=check.name,
        repo_full_name=repo.full_name,
        branch=branch,
        passed=passed,
        output=output,
        timestamp=now,
    )
    async with AsyncSession(engine) as session:
        session.add(record)
        await session.commit()

    return result


async def run_check_for_all_targets(
    check: CheckDefinition,
    repos: list[TrackedRepository],
    workspace: WorkspaceManager,
    engine: AsyncEngine,
    specific_repo: str | None = None,
    triggered_by: str = "manual",
) -> list[CheckResult]:
    """Resolve targets and run the check for every repo × branch combination."""
    now = datetime.now(timezone.utc)

    # Create a run record to group all results
    run_record = CheckRunRecord(
        check_slug=check.slug,
        check_name=check.name,
        triggered_by=triggered_by,
        status="running",
        started_at=now,
    )
    async with AsyncSession(engine) as session:
        session.add(run_record)
        await session.commit()
        await session.refresh(run_record)

    run_id: int = run_record.id  # type: ignore[assignment]

    progress = CheckProgress()
    _running_checks[check.slug] = progress
    try:
        resolved = await resolve_targets(check.targets, repos, workspace)

        if specific_repo is not None:
            resolved = [(r, b) for r, b in resolved if r.full_name == specific_repo]

        # Count total tasks before starting
        task_list: list[tuple[TrackedRepository, str]] = []
        for repo, branches in resolved:
            for branch in branches:
                task_list.append((repo, branch))

        progress.total = len(task_list)

        sem = asyncio.Semaphore(_CONCURRENCY)

        async def _run(repo: TrackedRepository, branch: str) -> CheckResult:
            async with sem:
                result = await run_check(check, repo, branch, workspace, engine, run_id)
                progress.completed += 1
                return result

        tasks: list[asyncio.Task[CheckResult]] = []
        for repo, branch in task_list:
            tasks.append(asyncio.create_task(_run(repo, branch)))

        if not tasks:
            results: list[CheckResult] = []
        else:
            results = list(await asyncio.gather(*tasks))

        # Mark run as completed
        async with AsyncSession(engine) as session:
            record = await session.get(CheckRunRecord, run_id)
            assert record is not None
            record.status = "completed"
            record.finished_at = datetime.now(timezone.utc)
            session.add(record)
            await session.commit()

        return results
    finally:
        _running_checks.pop(check.slug, None)
