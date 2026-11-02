"""Repository workspace manager — bare clones with git worktrees."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from grimoire.config import GrimoireConfig
from grimoire.models import TrackedRepository

logger = logging.getLogger(__name__)


class WorkspaceError(Exception):
    """Raised when a git operation in the workspace fails."""


class WorkspaceManager:
    """Clone, sync, and manage working directories for tracked repositories.

    Uses a bare-clone + worktree layout so that multiple branches of the same
    repository share a single object store::

        {workspace_dir}/{owner}/{repo}/.bare/     # bare clone
        {workspace_dir}/{owner}/{repo}/{branch}/  # worktrees
    """

    def __init__(self, config: GrimoireConfig) -> None:
        self._config = config
        # Resolve to absolute so paths passed to git subprocesses (which may
        # have a different cwd) are always correct.
        self._workspace_dir = config.workspace_dir.resolve()
        self._repo_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _repo_lock(self, full_name: str) -> asyncio.Lock:
        """Return a per-repo lock, creating it on first access."""
        if full_name not in self._repo_locks:
            self._repo_locks[full_name] = asyncio.Lock()
        return self._repo_locks[full_name]

    @property
    def workspace_dir(self) -> Path:
        """Return the workspace root directory."""
        return self._workspace_dir

    async def setup(self, repos: list[TrackedRepository]) -> None:
        """Clone/update all repos and configure git identity in each."""
        for repo in repos:
            branches = repo.branches or [repo.default_branch]
            bare_dir = self._bare_dir(repo.full_name)

            if not bare_dir.exists():
                await self._clone_bare(repo.full_name)

            await self._configure_identity(bare_dir)

            for branch in branches:
                await self._ensure_worktree(repo.full_name, branch, bare_dir)

    async def sync_repo(self, repo: TrackedRepository) -> None:
        """Fetch latest from remote and reset all worktrees for a single repo."""
        bare_dir = self._bare_dir(repo.full_name)
        if not bare_dir.exists():
            logger.warning("bare repo missing for %s — skipping sync", repo.full_name)
            return
        await self._run_git("fetch", "origin", cwd=bare_dir)

        # Reset each existing worktree to the latest remote state.
        branches = repo.branches or [repo.default_branch]
        for branch in branches:
            workdir = self.get_workdir(repo.full_name, branch)
            if not workdir.exists():
                continue
            try:
                await self._run_git("checkout", branch, cwd=workdir)
                await self._run_git("reset", "--hard", f"origin/{branch}", cwd=workdir)
                await self._run_git("clean", "-fdx", cwd=workdir)
            except WorkspaceError:
                logger.warning(
                    "Failed to reset worktree %s/%s after fetch", repo.full_name, branch
                )

    async def sync_all(self, repos: list[TrackedRepository]) -> None:
        """Fetch latest from remote for all repos with bounded concurrency."""
        sem = asyncio.Semaphore(10)

        async def _sync(repo: TrackedRepository) -> None:
            async with sem:
                try:
                    await self.sync_repo(repo)
                except WorkspaceError:
                    logger.exception("Failed to sync %s", repo.full_name)

        await asyncio.gather(*[_sync(r) for r in repos])

    async def reset_workdir(self, full_name: str, branch: str) -> Path:
        """Ensure the worktree for *full_name*/*branch* is clean and up-to-date.

        Returns the worktree path.
        """
        workdir = self.get_workdir(full_name, branch)
        bare_dir = self._bare_dir(full_name)

        # Serialize clone + worktree creation per repo to prevent races when
        # multiple branches of the same repo are set up concurrently.
        async with self._repo_lock(full_name):
            if not bare_dir.exists():
                await self._clone_bare(full_name)
                await self._configure_identity(bare_dir)

            if not workdir.exists():
                await self._ensure_worktree(full_name, branch, bare_dir)

        await self._run_git("checkout", branch, cwd=workdir)
        await self._run_git("reset", "--hard", f"origin/{branch}", cwd=workdir)
        await self._run_git("clean", "-fdx", cwd=workdir)
        return workdir

    def get_workdir(self, full_name: str, branch: str) -> Path:
        """Return the worktree path for *full_name*/*branch*."""
        owner, repo = full_name.split("/", 1)
        return self._workspace_dir / owner / repo / branch

    def get_env(self) -> dict[str, str]:
        """Return environment variables for git/gh sub-processes."""
        env: dict[str, str] = {
            "GH_TOKEN": self._config.github.token,
            "GITHUB_TOKEN": self._config.github.token,
        }
        if self._config.git:
            env.update(
                {
                    "GIT_AUTHOR_NAME": self._config.git.user.name,
                    "GIT_AUTHOR_EMAIL": self._config.git.user.email,
                    "GIT_COMMITTER_NAME": self._config.git.user.name,
                    "GIT_COMMITTER_EMAIL": self._config.git.user.email,
                }
            )
            if self._config.git.ssh_known_hosts:
                env["GIT_SSH_COMMAND"] = (
                    f"ssh -o UserKnownHostsFile={self._config.git.ssh_known_hosts}"
                )
        return env

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _bare_dir(self, full_name: str) -> Path:
        owner, repo = full_name.split("/", 1)
        return self._workspace_dir / owner / repo / ".bare"

    def _clone_url(self, full_name: str) -> str:
        token = self._config.github.token
        return f"https://x-access-token:{token}@github.com/{full_name}.git"

    async def _run_git(self, *args: str, cwd: Path) -> str:
        """Run a git command, capture output, and raise on failure."""
        cmd = ("git", *args)
        logger.debug("git %s  (cwd=%s)", " ".join(args), cwd)
        # Allow operations inside bare repos (git ≥2.38 safe.bareRepository).
        env = {
            **os.environ,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.bareRepository",
            "GIT_CONFIG_VALUE_0": "all",
        }
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await proc.communicate()
        finally:
            # On cancellation communicate() leaves git running; reap it.
            if proc.returncode is None:
                proc.kill()
                await asyncio.shield(proc.wait())
        stdout = stdout_bytes.decode()
        stderr = stderr_bytes.decode()

        if proc.returncode != 0:
            raise WorkspaceError(
                f"git {' '.join(args)} failed (rc={proc.returncode}): {stderr.strip()}"
            )

        logger.debug("git stdout: %s", stdout.strip())
        return stdout

    async def _clone_bare(self, full_name: str) -> None:
        bare_dir = self._bare_dir(full_name)
        bare_dir.parent.mkdir(parents=True, exist_ok=True)
        url = self._clone_url(full_name)
        logger.info("cloning %s → %s", full_name, bare_dir)
        await self._run_git("clone", "--bare", url, str(bare_dir), cwd=bare_dir.parent)

        # Bare clones store refs directly under refs/heads/* and lack remote
        # tracking branches.  Configure a normal fetch refspec and populate
        # refs/remotes/origin/* so worktrees can reference origin/<branch>.
        await self._run_git(
            "config",
            "remote.origin.fetch",
            "+refs/heads/*:refs/remotes/origin/*",
            cwd=bare_dir,
        )
        await self._run_git("fetch", "origin", cwd=bare_dir)

    async def _configure_identity(self, bare_dir: Path) -> None:
        git_cfg = self._config.git
        if git_cfg is None:
            return

        await self._run_git("config", "user.name", git_cfg.user.name, cwd=bare_dir)
        await self._run_git("config", "user.email", git_cfg.user.email, cwd=bare_dir)

        if git_cfg.signing:
            await self._run_git("config", "commit.gpgsign", "true", cwd=bare_dir)
            gpg_format = "openpgp" if git_cfg.signing.format == "gpg" else "ssh"
            await self._run_git("config", "gpg.format", gpg_format, cwd=bare_dir)
            await self._run_git(
                "config", "user.signingkey", str(git_cfg.signing.key_path), cwd=bare_dir
            )

    async def _ensure_worktree(self, full_name: str, branch: str, bare_dir: Path) -> None:
        workdir = self.get_workdir(full_name, branch)
        if workdir.exists():
            logger.debug("worktree already exists: %s", workdir)
            return

        try:
            await self._run_git("worktree", "add", str(workdir), branch, cwd=bare_dir)
        except WorkspaceError as exc:
            logger.warning("failed to add worktree for %s/%s: %s", full_name, branch, exc)
