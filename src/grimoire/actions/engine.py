"""Action execution engine."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from grimoire.database import ActionRunRecord, ActionRunRepoRecord
from grimoire.models import ActionRepoResult, ActionRun
from grimoire.observability.metrics import update_action_metrics
from grimoire.script import create_script_process, kill_process
from grimoire.targeting import resolve_targets, target_env

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from grimoire.actions.loader import ActionDefinition
    from grimoire.models import TrackedRepository
    from grimoire.workspace.manager import WorkspaceManager

OUTPUT_SIZE_CAP = 64 * 1024  # 64 KB
_DEFAULT_TIMEOUT = 600  # 10 minutes


@dataclass
class ActionProgress:
    """Tracks execution progress of a running action."""

    completed: int = 0
    total: int = 0


# In-memory tracking of currently-running action slugs and their progress
_running_actions: dict[str, ActionProgress] = {}


def is_action_running(slug: str) -> bool:
    """Return True if the action is currently executing."""
    return slug in _running_actions


def get_action_progress(slug: str) -> ActionProgress | None:
    """Return the progress tracker for a running action, or None if idle."""
    return _running_actions.get(slug)


class ActionConflictError(Exception):
    """Raised when an action is already running."""


async def run_action(
    action: ActionDefinition,
    repos: list[TrackedRepository],
    workspace: WorkspaceManager,
    engine: AsyncEngine,
    triggered_by: str,
    specific_repo: str | None = None,
) -> ActionRun:
    """Run an action sequentially against each target repo+branch.

    Raises :class:`ActionConflictError` if the action is already running.
    """
    now = datetime.now(timezone.utc)

    # 1. Check concurrent run guard
    async with AsyncSession(engine) as session:
        stmt = select(ActionRunRecord).where(
            ActionRunRecord.action_slug == action.slug,
            ActionRunRecord.status == "running",
        )
        existing = (await session.exec(stmt)).first()
        if existing is not None:
            raise ActionConflictError(
                f"Action '{action.slug}' is already running (run ID: {existing.id})"
            )

    # 2. Create run record
    run_record = ActionRunRecord(
        action_slug=action.slug,
        action_name=action.name,
        triggered_by=triggered_by,
        status="running",
        started_at=now,
    )
    async with AsyncSession(engine) as session:
        session.add(run_record)
        await session.commit()
        await session.refresh(run_record)

    run_id: int = run_record.id  # type: ignore[assignment]

    # 3. Resolve targets and execute
    results: list[ActionRepoResult] = []
    progress = ActionProgress()
    _running_actions[action.slug] = progress

    try:
        if action.targets is None:
            # Global action: run script once, not per-repo
            progress.total = 1
            passed, output = await _execute_global_action(action, workspace)
            result = ActionRepoResult(
                repo_full_name="(global)",
                branch="",
                passed=passed,
                output=output,
            )
            results.append(result)
            progress.completed = 1

            repo_record = ActionRunRepoRecord(
                run_id=run_id,
                repo_full_name="(global)",
                branch="",
                passed=passed,
                output=output,
            )
            async with AsyncSession(engine) as session:
                session.add(repo_record)
                await session.commit()
        else:
            # Per-repo action
            resolved = await resolve_targets(action.targets, repos, workspace)
            if specific_repo is not None:
                resolved = [(r, b) for r, b in resolved if r.full_name == specific_repo]

            # Count total repo×branch tasks
            progress.total = sum(len(branches) for _, branches in resolved)

            for repo, branches in resolved:
                for branch in branches:
                    passed, output = await _execute_action(action, repo, branch, workspace)
                    result = ActionRepoResult(
                        repo_full_name=repo.full_name,
                        branch=branch,
                        passed=passed,
                        output=output,
                    )
                    results.append(result)
                    progress.completed += 1

                    repo_record = ActionRunRepoRecord(
                        run_id=run_id,
                        repo_full_name=repo.full_name,
                        branch=branch,
                        passed=passed,
                        output=output,
                    )
                    async with AsyncSession(engine) as session:
                        session.add(repo_record)
                        await session.commit()

        # Mark run as completed (inside try so finally always cleans up in-memory state)
        finished_at = datetime.now(timezone.utc)
        async with AsyncSession(engine) as session:
            record = await session.get(ActionRunRecord, run_id)
            assert record is not None
            record.status = "completed"
            record.finished_at = finished_at
            session.add(record)
            await session.commit()
    finally:
        _running_actions.pop(action.slug, None)

    # 6. Return summary
    duration = (finished_at - now).total_seconds()
    update_action_metrics(action.slug, duration)

    return ActionRun(
        action_name=action.name,
        action_slug=action.slug,
        triggered_by=triggered_by,
        started_at=now,
        finished_at=finished_at,
        results=results,
    )


async def _execute_global_action(
    action: ActionDefinition,
    workspace: WorkspaceManager,
) -> tuple[bool, str]:
    """Run the action script once, not tied to any specific repo."""
    cwd = workspace.workspace_dir
    cwd.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **workspace.get_env()}

    passed = False
    output = ""

    proc: asyncio.subprocess.Process | None = None
    tmp_script = None
    try:
        proc, tmp_script = await create_script_process(
            action.script,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=_DEFAULT_TIMEOUT)
        output = stdout_bytes.decode(errors="replace")
        passed = proc.returncode == 0
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

    # Cap output
    if len(output) > OUTPUT_SIZE_CAP:
        output = "[output truncated — showing last 64KB]\n" + output[-OUTPUT_SIZE_CAP:]

    return passed, output


async def _execute_action(
    action: ActionDefinition,
    repo: TrackedRepository,
    branch: str,
    workspace: WorkspaceManager,
) -> tuple[bool, str]:
    """Run the action script in a repo+branch workdir. Returns (passed, output)."""
    # Pre-execution: sync then reset
    await workspace.sync_repo(repo)
    workdir = await workspace.reset_workdir(repo.full_name, branch)
    env = target_env(workspace, repo, branch)

    passed = False
    output = ""

    proc: asyncio.subprocess.Process | None = None
    tmp_script = None
    try:
        proc, tmp_script = await create_script_process(
            action.script,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=_DEFAULT_TIMEOUT)
        output = stdout_bytes.decode(errors="replace")
        passed = proc.returncode == 0
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

    # Cap output
    if len(output) > OUTPUT_SIZE_CAP:
        output = "[output truncated — showing last 64KB]\n" + output[-OUTPUT_SIZE_CAP:]

    return passed, output
