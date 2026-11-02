"""Tests for the workspace manager."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from grimoire.config import (
    GitConfig,
    GitHubConfig,
    GitUserConfig,
    GrimoireConfig,
    StaticRepoSource,
)
from grimoire.models import TrackedRepository
from grimoire.workspace.manager import WorkspaceManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_config(
    workspace_dir: Path,
    *,
    git: GitConfig | None = None,
) -> GrimoireConfig:
    return GrimoireConfig(
        github=GitHubConfig(token="ghp_test_token"),
        git=git,
        repositories=[StaticRepoSource(repo="owner/repo")],
        workspace_dir=workspace_dir,
    )


# Environment that allows operations in bare repos (git ≥2.38 safe.bareRepository).
_SAFE_BARE_ENV = {
    **os.environ,
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.bareRepository",
    "GIT_CONFIG_VALUE_0": "all",
}


async def _create_local_origin(base: Path, branch: str = "main") -> Path:
    """Create a small local git repo to act as the remote origin.

    Returns the path to the bare ``--mirror`` clone that can be used as a
    ``file://`` URL.
    """
    src = base / "_origin_src"
    src.mkdir(parents=True)

    async def _git(*args: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=src,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        assert proc.returncode == 0, stderr.decode()

    await _git("init", "-b", branch)
    await _git("config", "user.name", "Test")
    await _git("config", "user.email", "test@test.com")

    (src / "README.md").write_text("hello")
    await _git("add", ".")
    await _git("commit", "-m", "init")

    bare = base / "_origin.git"
    proc = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        "--bare",
        str(src),
        str(bare),
        env=_SAFE_BARE_ENV,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    assert proc.returncode == 0
    return bare


# ---------------------------------------------------------------------------
# get_env tests
# ---------------------------------------------------------------------------


class TestGetEnv:
    def test_without_git_config(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path)
        mgr = WorkspaceManager(cfg)
        env = mgr.get_env()

        assert env == {
            "GH_TOKEN": "ghp_test_token",
            "GITHUB_TOKEN": "ghp_test_token",
        }

    def test_with_git_config(self, tmp_path: Path) -> None:
        git = GitConfig(user=GitUserConfig(name="Ada", email="ada@example.com"))
        cfg = _minimal_config(tmp_path, git=git)
        mgr = WorkspaceManager(cfg)
        env = mgr.get_env()

        assert env["GIT_AUTHOR_NAME"] == "Ada"
        assert env["GIT_AUTHOR_EMAIL"] == "ada@example.com"
        assert env["GIT_COMMITTER_NAME"] == "Ada"
        assert env["GIT_COMMITTER_EMAIL"] == "ada@example.com"
        assert env["GH_TOKEN"] == "ghp_test_token"

    def test_with_ssh_known_hosts(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "known_hosts"
        hosts_file.touch()
        git = GitConfig(
            user=GitUserConfig(name="Ada", email="ada@example.com"),
            ssh_known_hosts=hosts_file,
        )
        cfg = _minimal_config(tmp_path, git=git)
        mgr = WorkspaceManager(cfg)
        env = mgr.get_env()

        assert "GIT_SSH_COMMAND" in env
        assert str(hosts_file) in env["GIT_SSH_COMMAND"]


# ---------------------------------------------------------------------------
# get_workdir tests
# ---------------------------------------------------------------------------


class TestGetWorkdir:
    def test_path_structure(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path)
        mgr = WorkspaceManager(cfg)
        result = mgr.get_workdir("acme/widgets", "main")

        assert result == tmp_path / "acme" / "widgets" / "main"

    def test_branch_with_slash(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path)
        mgr = WorkspaceManager(cfg)
        result = mgr.get_workdir("acme/widgets", "feature/foo")

        assert result == tmp_path / "acme" / "widgets" / "feature/foo"


# ---------------------------------------------------------------------------
# Clone URL tests
# ---------------------------------------------------------------------------


class TestCloneUrl:
    def test_https_url_includes_token(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path)
        mgr = WorkspaceManager(cfg)
        url = mgr._clone_url("owner/repo")  # noqa: SLF001

        assert url == "https://x-access-token:ghp_test_token@github.com/owner/repo.git"


# ---------------------------------------------------------------------------
# Integration tests (real git operations)
# ---------------------------------------------------------------------------


class TestSetup:
    async def test_creates_bare_clone_and_worktree(self, tmp_path: Path) -> None:
        origin = await _create_local_origin(tmp_path, branch="main")

        cfg = _minimal_config(tmp_path / "ws")
        mgr = WorkspaceManager(cfg)

        # Monkey-patch clone URL to use local file path
        mgr._clone_url = lambda full_name: str(origin)  # type: ignore[method-assign]  # noqa: SLF001

        repo = TrackedRepository(full_name="acme/widgets", default_branch="main")
        await mgr.setup([repo])

        bare_dir = tmp_path / "ws" / "acme" / "widgets" / ".bare"
        assert bare_dir.exists()
        assert (bare_dir / "HEAD").exists()

        workdir = tmp_path / "ws" / "acme" / "widgets" / "main"
        assert workdir.exists()
        assert (workdir / "README.md").exists()

    async def test_setup_idempotent(self, tmp_path: Path) -> None:
        origin = await _create_local_origin(tmp_path, branch="main")

        cfg = _minimal_config(tmp_path / "ws")
        mgr = WorkspaceManager(cfg)
        mgr._clone_url = lambda full_name: str(origin)  # type: ignore[method-assign]  # noqa: SLF001

        repo = TrackedRepository(full_name="acme/widgets", default_branch="main")
        await mgr.setup([repo])
        # Running setup again should not raise
        await mgr.setup([repo])

    async def test_configures_git_identity(self, tmp_path: Path) -> None:
        origin = await _create_local_origin(tmp_path, branch="main")

        git = GitConfig(user=GitUserConfig(name="Bot", email="bot@ci.dev"))
        cfg = _minimal_config(tmp_path / "ws", git=git)
        mgr = WorkspaceManager(cfg)
        mgr._clone_url = lambda full_name: str(origin)  # type: ignore[method-assign]  # noqa: SLF001

        repo = TrackedRepository(full_name="acme/widgets", default_branch="main")
        await mgr.setup([repo])

        bare_dir = tmp_path / "ws" / "acme" / "widgets" / ".bare"
        proc = await asyncio.create_subprocess_exec(
            "git",
            "config",
            "user.name",
            cwd=bare_dir,
            env=_SAFE_BARE_ENV,
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        assert stdout.decode().strip() == "Bot"


class TestSyncAll:
    async def test_syncs_all_repos(self, tmp_path: Path) -> None:
        origin = await _create_local_origin(tmp_path, branch="main")

        cfg = _minimal_config(tmp_path / "ws")
        mgr = WorkspaceManager(cfg)
        mgr._clone_url = lambda full_name: str(origin)  # type: ignore[method-assign]  # noqa: SLF001

        repo = TrackedRepository(full_name="acme/widgets", default_branch="main")
        await mgr.setup([repo])

        # Push a new commit to the origin
        src = tmp_path / "_origin_src"

        async def _git(*args: str) -> None:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=src,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            assert proc.returncode == 0, stderr.decode()

        (src / "new_file.txt").write_text("new content")
        await _git("add", ".")
        await _git("commit", "-m", "second commit")

        # Update the bare mirror
        bare_origin = tmp_path / "_origin.git"
        proc = await asyncio.create_subprocess_exec(
            "git",
            "fetch",
            str(src),
            "main:main",
            cwd=bare_origin,
            env=_SAFE_BARE_ENV,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        # sync_all should fetch the new commit and reset the worktree
        await mgr.sync_all([repo])

        # The worktree should already have the new file (no reset_workdir needed)
        workdir = mgr.get_workdir("acme/widgets", "main")
        assert (workdir / "new_file.txt").exists()

    async def test_skips_failed_repos(self, tmp_path: Path) -> None:
        """sync_all should not raise even if a repo fails to sync."""
        cfg = _minimal_config(tmp_path / "ws")
        mgr = WorkspaceManager(cfg)
        repo = TrackedRepository(full_name="acme/missing", default_branch="main")
        # Should not raise — missing repos are skipped with a warning
        await mgr.sync_all([repo])


class TestSyncRepo:
    async def test_fetch_updates(self, tmp_path: Path) -> None:
        origin = await _create_local_origin(tmp_path, branch="main")

        cfg = _minimal_config(tmp_path / "ws")
        mgr = WorkspaceManager(cfg)
        mgr._clone_url = lambda full_name: str(origin)  # type: ignore[method-assign]  # noqa: SLF001

        repo = TrackedRepository(full_name="acme/widgets", default_branch="main")
        await mgr.setup([repo])
        # sync_repo should succeed (no new commits, but fetch doesn't fail)
        await mgr.sync_repo(repo)

    async def test_sync_missing_repo_warns(self, tmp_path: Path) -> None:
        cfg = _minimal_config(tmp_path / "ws")
        mgr = WorkspaceManager(cfg)
        repo = TrackedRepository(full_name="acme/missing", default_branch="main")
        # Should not raise, just log a warning
        await mgr.sync_repo(repo)


class TestResetWorkdir:
    async def test_reset_returns_clean_workdir(self, tmp_path: Path) -> None:
        origin = await _create_local_origin(tmp_path, branch="main")

        cfg = _minimal_config(tmp_path / "ws")
        mgr = WorkspaceManager(cfg)
        mgr._clone_url = lambda full_name: str(origin)  # type: ignore[method-assign]  # noqa: SLF001

        repo = TrackedRepository(full_name="acme/widgets", default_branch="main")
        await mgr.setup([repo])

        workdir = mgr.get_workdir("acme/widgets", "main")

        # Dirty the worktree
        (workdir / "junk.txt").write_text("garbage")

        result = await mgr.reset_workdir("acme/widgets", "main")

        assert result == workdir
        assert not (workdir / "junk.txt").exists()
        assert (workdir / "README.md").exists()

    async def test_reset_auto_clones_when_bare_missing(self, tmp_path: Path) -> None:
        """reset_workdir should clone the bare repo if it doesn't exist yet."""
        origin = await _create_local_origin(tmp_path, branch="main")

        cfg = _minimal_config(tmp_path / "ws")
        mgr = WorkspaceManager(cfg)
        mgr._clone_url = lambda full_name: str(origin)  # type: ignore[method-assign]  # noqa: SLF001

        # Do NOT call setup() — reset_workdir must handle missing bare repo
        bare_dir = tmp_path / "ws" / "acme" / "widgets" / ".bare"
        assert not bare_dir.exists()

        result = await mgr.reset_workdir("acme/widgets", "main")

        assert bare_dir.exists()
        assert result.exists()
        assert (result / "README.md").exists()

    async def test_concurrent_reset_same_repo(self, tmp_path: Path) -> None:
        """Concurrent reset_workdir calls for the same repo must not race on clone."""
        await _create_local_origin(tmp_path, branch="main")

        # Create a second branch in the origin
        src = tmp_path / "_origin_src"

        async def _git(*args: str) -> None:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=src,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            assert proc.returncode == 0, stderr.decode()

        await _git("checkout", "-b", "develop")
        (src / "dev.txt").write_text("dev")
        await _git("add", ".")
        await _git("commit", "-m", "dev branch")

        # Update the bare mirror
        bare_origin = tmp_path / "_origin.git"
        proc = await asyncio.create_subprocess_exec(
            "git",
            "fetch",
            str(src),
            "develop:develop",
            cwd=bare_origin,
            env=_SAFE_BARE_ENV,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        cfg = _minimal_config(tmp_path / "ws")
        mgr = WorkspaceManager(cfg)
        mgr._clone_url = lambda full_name: str(bare_origin)  # type: ignore[method-assign]  # noqa: SLF001

        # Launch two reset_workdir calls concurrently (no prior setup)
        main_result, dev_result = await asyncio.gather(
            mgr.reset_workdir("acme/widgets", "main"),
            mgr.reset_workdir("acme/widgets", "develop"),
        )

        assert main_result.exists()
        assert dev_result.exists()
        assert (main_result / "README.md").exists()
        assert (dev_result / "dev.txt").exists()


async def test_run_git_cancellation_reaps_child(tmp_path: Path, monkeypatch) -> None:
    """Cancelling a git call must not leave git running or defunct."""
    import psutil

    fake_git = tmp_path / "bin" / "git"
    fake_git.parent.mkdir()
    fake_git.write_text("#!/bin/sh\nexec sleep 60\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_git.parent}:{os.environ['PATH']}")

    manager = WorkspaceManager(_minimal_config(tmp_path / "ws"))
    for _ in range(5):
        task = asyncio.create_task(manager._run_git("status", cwd=tmp_path))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert psutil.Process().children(recursive=True) == []
