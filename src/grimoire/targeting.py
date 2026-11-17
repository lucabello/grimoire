"""Shared target resolution for checks and actions."""

from __future__ import annotations

import asyncio
import os
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, model_validator
from typing_extensions import Self

from grimoire.script import create_script_process, kill_process

if TYPE_CHECKING:
    from grimoire.models import TrackedRepository
    from grimoire.workspace.manager import WorkspaceManager

_TARGETING_TIMEOUT = 30

# Alias the builtin so the ``list`` *field* on TargetSpec doesn't shadow the
# type during deferred annotation evaluation (from __future__ import annotations).
_List = list


class TargetSpec(BaseModel):
    """Targeting configuration. Exactly one field must be set."""

    list: _List[str] | None = None
    regex: str | None = None
    script: str | None = None

    @model_validator(mode="after")
    def exactly_one_target(self) -> Self:
        set_count = sum(1 for v in [self.list, self.regex, self.script] if v is not None)
        if set_count != 1:
            raise ValueError("Exactly one of 'list', 'regex', or 'script' must be set")
        return self


ResolvedTarget = tuple["TrackedRepository", _List[str]]
"""A repo and the branches (of its observed set) the target script matched."""


def _observed_branches(repo: TrackedRepository) -> _List[str]:
    """Branches Grimoire tracks for *repo* (explicit list, or default only)."""
    return repo.branches or [repo.default_branch]


def target_env(
    workspace: WorkspaceManager,
    repo: TrackedRepository,
    branch: str,
) -> dict[str, str]:
    """Build the environment for a target/check/action script invocation.

    Includes the shared workspace env vars (``GH_TOKEN`` etc.) plus per-invocation
    context (``REPO_OWNER``, ``REPO_NAME``, ``REPO_FULL_NAME``, ``BRANCH``,
    ``DEFAULT_BRANCH``) so scripts can branch on which branch they're running for.
    """
    owner, name = repo.full_name.split("/", 1)
    env = {**os.environ, **workspace.get_env()}
    env.update(
        {
            "REPO_OWNER": owner,
            "REPO_NAME": name,
            "REPO_FULL_NAME": repo.full_name,
            "BRANCH": branch,
            "DEFAULT_BRANCH": repo.default_branch,
        }
    )
    return env


async def resolve_targets(
    targets: TargetSpec,
    repos: _List[TrackedRepository],
    workspace: WorkspaceManager,
) -> _List[ResolvedTarget]:
    """Resolve which ``(repo, branches)`` pairs a check or action applies to.

    - ``list``  → repo matches by full name; all its observed branches are included.
    - ``regex`` → repo matches by full-name pattern; all its observed branches are included.
    - ``script`` → the script is executed once per observed branch (in that branch's
      workdir with ``BRANCH``/``DEFAULT_BRANCH`` env vars set); a branch is included
      when the script exits ``0``. Repos with no matching branch are dropped.

    Repos with no matching branch are excluded entirely from the result.
    """
    if targets.list is not None:
        allowed = set(targets.list)
        return [(r, _observed_branches(r)) for r in repos if r.full_name in allowed]

    if targets.regex is not None:
        pattern = re.compile(targets.regex)
        return [(r, _observed_branches(r)) for r in repos if pattern.search(r.full_name)]

    # script targeting: evaluated per branch
    assert targets.script is not None
    resolved: _List[ResolvedTarget] = []
    for repo in repos:
        matched_branches: _List[str] = []
        for branch in _observed_branches(repo):
            if await _script_matches(targets.script, workspace, repo, branch):
                matched_branches.append(branch)
        if matched_branches:
            resolved.append((repo, matched_branches))
    return resolved


async def _script_matches(
    script: str,
    workspace: WorkspaceManager,
    repo: TrackedRepository,
    branch: str,
) -> bool:
    """Return True when *script* exits 0 for ``(repo, branch)``."""
    cwd = workspace.get_workdir(repo.full_name, branch)
    env = target_env(workspace, repo, branch)
    proc: asyncio.subprocess.Process | None = None
    tmp_script = None
    try:
        proc, tmp_script = await create_script_process(
            script,
            cwd=cwd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        rc = await asyncio.wait_for(proc.wait(), timeout=_TARGETING_TIMEOUT)
        return rc == 0
    except (asyncio.TimeoutError, OSError):
        return False
    finally:
        if proc is not None:
            await kill_process(proc)
        if tmp_script is not None:
            tmp_script.unlink(missing_ok=True)
