"""Tests for the action execution engine."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from grimoire.actions.engine import (
    OUTPUT_SIZE_CAP,
    ActionConflictError,
    run_action,
)
from grimoire.actions.loader import ActionDefinition
from grimoire.database import ActionRunRecord, ActionRunRepoRecord, get_engine, upgrade_database
from grimoire.models import TrackedRepository
from grimoire.targeting import TargetSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockWorkspace:
    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self.sync_calls: list[str] = []
        self.reset_calls: list[tuple[str, str]] = []

    @property
    def workspace_dir(self) -> Path:
        return self._workdir

    async def sync_repo(self, repo: TrackedRepository) -> None:
        self.sync_calls.append(repo.full_name)

    async def reset_workdir(self, full_name: str, branch: str) -> Path:
        self.reset_calls.append((full_name, branch))
        return self._workdir

    def get_workdir(self, full_name: str, branch: str) -> Path:
        return self._workdir

    def get_env(self) -> dict[str, str]:
        return {"GH_TOKEN": "test", "GITHUB_TOKEN": "test"}


def _action(script: str = "echo hello") -> ActionDefinition:
    return ActionDefinition(
        name="Test Action",
        slug="test-action",
        description="A test",
        targets=TargetSpec(list=["acme/repo"]),
        script=script,
    )


def _repo(name: str = "acme/repo", branches: list[str] | None = None) -> TrackedRepository:
    return TrackedRepository(
        full_name=name,
        default_branch="main",
        branches=branches or [],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunAction:
    async def test_successful_action(self, tmp_path: Path) -> None:
        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)
        ws = MockWorkspace(tmp_path)

        result = await run_action(
            _action("echo hello"),
            [_repo()],
            ws,
            engine,
            triggered_by="manual",  # type: ignore[arg-type]
        )

        assert result.action_slug == "test-action"
        assert result.triggered_by == "manual"
        assert result.finished_at is not None
        assert len(result.results) == 1
        assert result.results[0].passed is True
        assert "hello" in result.results[0].output

    async def test_failed_action(self, tmp_path: Path) -> None:
        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)
        ws = MockWorkspace(tmp_path)

        result = await run_action(
            _action("exit 1"),
            [_repo()],
            ws,
            engine,
            triggered_by="manual",  # type: ignore[arg-type]
        )

        assert len(result.results) == 1
        assert result.results[0].passed is False

    async def test_sequential_execution(self, tmp_path: Path) -> None:
        """Verify repos are processed sequentially (order preserved)."""
        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)
        ws = MockWorkspace(tmp_path)

        repos = [_repo("acme/alpha"), _repo("acme/beta")]
        action = ActionDefinition(
            name="Multi Action",
            slug="multi-action",
            description="Runs on two repos",
            targets=TargetSpec(list=["acme/alpha", "acme/beta"]),
            script="echo ok",
        )

        result = await run_action(action, repos, ws, engine, triggered_by="manual")  # type: ignore[arg-type]

        assert len(result.results) == 2
        assert result.results[0].repo_full_name == "acme/alpha"
        assert result.results[1].repo_full_name == "acme/beta"
        # Verify sync and reset were called in order
        assert ws.sync_calls == ["acme/alpha", "acme/beta"]
        assert ws.reset_calls == [("acme/alpha", "main"), ("acme/beta", "main")]

    async def test_concurrent_run_guard(self, tmp_path: Path) -> None:
        """Running the same action twice concurrently should raise ActionConflictError."""
        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)

        # Insert a "running" record manually
        run_record = ActionRunRecord(
            action_slug="test-action",
            action_name="Test Action",
            triggered_by="manual",
            status="running",
        )
        async with AsyncSession(engine) as session:
            session.add(run_record)
            await session.commit()

        ws = MockWorkspace(tmp_path)

        with pytest.raises(ActionConflictError, match="already running"):
            await run_action(
                _action(),
                [_repo()],
                ws,
                engine,
                triggered_by="manual",  # type: ignore[arg-type]
            )

    async def test_output_capture_and_truncation(self, tmp_path: Path) -> None:
        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)
        ws = MockWorkspace(tmp_path)

        big_script = f"python3 -c \"print('x' * {OUTPUT_SIZE_CAP + 1000})\""
        result = await run_action(
            _action(big_script),
            [_repo()],
            ws,
            engine,
            triggered_by="manual",  # type: ignore[arg-type]
        )

        assert result.results[0].output.startswith("[output truncated")

    async def test_db_persistence(self, tmp_path: Path) -> None:
        """ActionRunRecord and ActionRunRepoRecord must be created."""
        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)
        ws = MockWorkspace(tmp_path)

        await run_action(
            _action("echo persisted"),
            [_repo()],
            ws,
            engine,
            triggered_by="manual",  # type: ignore[arg-type]
        )

        async with AsyncSession(engine) as session:
            runs = (await session.exec(select(ActionRunRecord))).all()
            assert len(runs) == 1
            assert runs[0].action_slug == "test-action"
            assert runs[0].status == "completed"
            assert runs[0].finished_at is not None

            repo_results = (await session.exec(select(ActionRunRepoRecord))).all()
            assert len(repo_results) == 1
            assert repo_results[0].passed is True
            assert "persisted" in repo_results[0].output

    async def test_pre_execution_calls_sync_and_reset(self, tmp_path: Path) -> None:
        """sync_repo and reset_workdir must be called for each repo+branch."""
        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)
        ws = MockWorkspace(tmp_path)

        await run_action(
            _action("echo ok"),
            [_repo()],
            ws,
            engine,
            triggered_by="manual",  # type: ignore[arg-type]
        )

        assert "acme/repo" in ws.sync_calls
        assert ("acme/repo", "main") in ws.reset_calls

    async def test_specific_repo_filter(self, tmp_path: Path) -> None:
        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)
        ws = MockWorkspace(tmp_path)

        repos = [_repo("acme/alpha"), _repo("acme/beta")]
        action = ActionDefinition(
            name="Multi",
            slug="multi",
            description="test",
            targets=TargetSpec(list=["acme/alpha", "acme/beta"]),
            script="echo ok",
        )

        result = await run_action(
            action,
            repos,
            ws,
            engine,
            triggered_by="manual",
            specific_repo="acme/beta",  # type: ignore[arg-type]
        )

        assert len(result.results) == 1
        assert result.results[0].repo_full_name == "acme/beta"

    async def test_timeout_handling(self, tmp_path: Path) -> None:
        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)
        ws = MockWorkspace(tmp_path)

        import grimoire.actions.engine as eng

        original = eng._DEFAULT_TIMEOUT
        eng._DEFAULT_TIMEOUT = 1
        try:
            result = await run_action(
                _action("sleep 30"),
                [_repo()],
                ws,
                engine,
                triggered_by="manual",  # type: ignore[arg-type]
            )
            assert result.results[0].passed is False
            assert "Timed out" in result.results[0].output
        finally:
            eng._DEFAULT_TIMEOUT = original

    async def test_multiple_branches(self, tmp_path: Path) -> None:
        """Actions run against all observed branches."""
        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)
        ws = MockWorkspace(tmp_path)

        repo = _repo("acme/repo", branches=["main", "develop"])
        result = await run_action(
            _action("echo ok"),
            [repo],
            ws,
            engine,
            triggered_by="cron",  # type: ignore[arg-type]
        )

        assert len(result.results) == 2
        assert result.results[0].branch == "main"
        assert result.results[1].branch == "develop"
        assert ws.reset_calls == [("acme/repo", "main"), ("acme/repo", "develop")]

    async def test_script_targeting_restricts_to_default_branch(self, tmp_path: Path) -> None:
        """Script targeting evaluated per branch scopes the action correctly."""
        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)
        ws = MockWorkspace(tmp_path)

        action = ActionDefinition(
            name="Default only",
            slug="default-only",
            description="",
            targets=TargetSpec(script='[ "$BRANCH" = "$DEFAULT_BRANCH" ]'),
            script="echo ok",
        )
        repo = _repo("acme/repo", branches=["main", "develop", "release/1.0"])

        result = await run_action(
            action,
            [repo],
            ws,
            engine,
            triggered_by="manual",  # type: ignore[arg-type]
        )

        branches = [r.branch for r in result.results]
        assert branches == ["main"]
        assert ws.reset_calls == [("acme/repo", "main")]


def _global_action(script: str = "echo global") -> ActionDefinition:
    return ActionDefinition(
        name="Global Action",
        slug="global-action",
        description="A global test action",
        targets=None,
        script=script,
    )


class TestGlobalAction:
    async def test_global_action_runs_once(self, tmp_path: Path) -> None:
        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)
        ws = MockWorkspace(tmp_path)

        result = await run_action(
            _global_action("echo global"),
            [_repo()],
            ws,
            engine,
            triggered_by="manual",
        )

        assert len(result.results) == 1
        assert result.results[0].repo_full_name == "(global)"
        assert result.results[0].branch == ""
        assert result.results[0].passed is True
        assert "global" in result.results[0].output

    async def test_global_action_no_sync_or_reset(self, tmp_path: Path) -> None:
        """Global actions must not call sync_repo or reset_workdir."""
        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)
        ws = MockWorkspace(tmp_path)

        await run_action(
            _global_action(),
            [_repo()],
            ws,
            engine,
            triggered_by="manual",
        )

        assert ws.sync_calls == []
        assert ws.reset_calls == []

    async def test_global_action_db_persistence(self, tmp_path: Path) -> None:
        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)
        ws = MockWorkspace(tmp_path)

        await run_action(
            _global_action("echo persisted"),
            [],
            ws,
            engine,
            triggered_by="cron",
        )

        async with AsyncSession(engine) as session:
            runs = (await session.exec(select(ActionRunRecord))).all()
            assert len(runs) == 1
            assert runs[0].status == "completed"

            repo_results = (await session.exec(select(ActionRunRepoRecord))).all()
            assert len(repo_results) == 1
            assert repo_results[0].repo_full_name == "(global)"
            assert repo_results[0].branch == ""
            assert "persisted" in repo_results[0].output

    async def test_global_action_failure(self, tmp_path: Path) -> None:
        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)
        ws = MockWorkspace(tmp_path)

        result = await run_action(
            _global_action("exit 1"),
            [],
            ws,
            engine,
            triggered_by="manual",
        )

        assert len(result.results) == 1
        assert result.results[0].passed is False


class TestActionProgress:
    """Tests for in-memory action progress tracking."""

    def test_initial_state(self) -> None:
        from grimoire.actions.engine import get_action_progress, is_action_running

        assert not is_action_running("nonexistent")
        assert get_action_progress("nonexistent") is None

    async def test_progress_tracked_during_run(self, tmp_path: Path) -> None:
        """Progress should be set during run and cleared after completion."""
        from grimoire.actions.engine import get_action_progress, is_action_running

        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)
        ws = MockWorkspace(tmp_path)

        repos = [_repo("acme/alpha"), _repo("acme/beta")]
        action = ActionDefinition(
            name="Progress Action",
            slug="progress-action",
            description="Test progress",
            targets=TargetSpec(list=["acme/alpha", "acme/beta"]),
            script="echo ok",
        )

        result = await run_action(action, repos, ws, engine, triggered_by="manual")

        # After completion, progress should be cleared
        assert not is_action_running("progress-action")
        assert get_action_progress("progress-action") is None
        assert len(result.results) == 2

    async def test_progress_cleared_on_error(self, tmp_path: Path) -> None:
        """Progress should be cleared even if the action fails."""
        from grimoire.actions.engine import get_action_progress, is_action_running

        engine = await get_engine(str(tmp_path / "test.db"))
        await upgrade_database(engine)
        ws = MockWorkspace(tmp_path)

        result = await run_action(_action("exit 1"), [_repo()], ws, engine, triggered_by="manual")

        assert not is_action_running("test-action")
        assert get_action_progress("test-action") is None
        assert result.results[0].passed is False
