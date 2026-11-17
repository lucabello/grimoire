"""Tests for the actions REST API router."""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from grimoire.actions.loader import ActionDefinition
from grimoire.actions.router import router, set_actions_state
from grimoire.app import create_app
from grimoire.database import get_engine, upgrade_database
from grimoire.models import TrackedRepository
from grimoire.targeting import TargetSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockWorkspace:
    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir

    async def sync_repo(self, repo: TrackedRepository) -> None:
        pass

    async def reset_workdir(self, full_name: str, branch: str) -> Path:
        return self._workdir

    def get_workdir(self, full_name: str, branch: str) -> Path:
        return self._workdir

    def get_env(self) -> dict[str, str]:
        return {"GH_TOKEN": "test", "GITHUB_TOKEN": "test"}


def _action(**overrides: object) -> ActionDefinition:
    defaults: dict[str, object] = {
        "name": "Test Action",
        "slug": "test-action",
        "description": "A test action",
        "targets": TargetSpec(list=["acme/repo"]),
        "script": "echo ok",
    }
    defaults.update(overrides)
    return ActionDefinition.model_validate(defaults)


def _repo() -> TrackedRepository:
    return TrackedRepository(full_name="acme/repo", default_branch="main")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def action_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """Provide an async HTTP client wired to a test app with the actions router."""
    engine = await get_engine(str(tmp_path / "test.db"))
    await upgrade_database(engine)

    actions = [_action()]
    repos = [_repo()]
    ws = MockWorkspace(tmp_path)

    set_actions_state(actions, repos, ws, engine)  # type: ignore[arg-type]

    app = create_app()
    app.include_router(router, prefix="/api")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def action_client_with_engine(tmp_path: Path) -> AsyncIterator[tuple[AsyncClient, object]]:
    """Provide an async HTTP client plus the engine for direct DB access."""
    engine = await get_engine(str(tmp_path / "test.db"))
    await upgrade_database(engine)

    actions = [_action()]
    repos = [_repo()]
    ws = MockWorkspace(tmp_path)

    set_actions_state(actions, repos, ws, engine)  # type: ignore[arg-type]

    app = create_app()
    app.include_router(router, prefix="/api")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListActions:
    async def test_list_actions(self, action_client: AsyncClient) -> None:
        resp = await action_client.get("/api/actions/")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["slug"] == "test-action"
        assert data[0]["name"] == "Test Action"


class TestRunAction:
    async def test_run_action(self, action_client: AsyncClient) -> None:
        resp = await action_client.post("/api/actions/test-action/run")
        assert resp.status_code == 200
        body = resp.json()
        assert body["action_slug"] == "test-action"
        assert body["status"] == "running"
        assert body["results"] == []

    async def test_run_action_not_found(self, action_client: AsyncClient) -> None:
        resp = await action_client.post("/api/actions/nonexistent/run")
        assert resp.status_code == 404

    async def test_run_action_conflict(
        self,
        action_client_with_engine: tuple[AsyncClient, object],
    ) -> None:
        client, engine = action_client_with_engine
        # Use in-memory tracker to simulate a running action
        from grimoire.actions.engine import ActionProgress, _running_actions

        _running_actions["test-action"] = ActionProgress(completed=0, total=1)
        try:
            resp = await client.post("/api/actions/test-action/run")
            assert resp.status_code == 409
            assert "already running" in resp.json()["detail"]
        finally:
            _running_actions.pop("test-action", None)

    async def test_run_action_with_repo_filter(self, action_client: AsyncClient) -> None:
        resp = await action_client.post("/api/actions/test-action/run?repo=acme/repo")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "running"


class TestRunHistory:
    async def test_runs_empty(self, action_client: AsyncClient) -> None:
        resp = await action_client.get("/api/actions/test-action/runs")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_runs_after_execution(self, action_client: AsyncClient) -> None:
        await action_client.post("/api/actions/test-action/run")
        resp = await action_client.get("/api/actions/test-action/runs")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["action_slug"] == "test-action"
        assert data[0]["status"] == "completed"
        assert data[0]["total_repos"] == 1
        assert data[0]["passed_repos"] == 1

    async def test_runs_not_found(self, action_client: AsyncClient) -> None:
        resp = await action_client.get("/api/actions/nonexistent/runs")
        assert resp.status_code == 404


class TestRunDetail:
    async def test_run_detail(self, action_client: AsyncClient) -> None:
        # Trigger a run (background task runs before response completes in test)
        await action_client.post("/api/actions/test-action/run")

        # Fetch run history to get the actual run ID
        runs_resp = await action_client.get("/api/actions/test-action/runs")
        runs = runs_resp.json()
        assert len(runs) >= 1
        run_id = runs[0]["id"]

        resp = await action_client.get(f"/api/actions/test-action/runs/{run_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == run_id
        assert body["action_slug"] == "test-action"
        assert len(body["results"]) == 1
        assert body["results"][0]["passed"] is True

    async def test_run_detail_not_found(self, action_client: AsyncClient) -> None:
        resp = await action_client.get("/api/actions/test-action/runs/99999")
        assert resp.status_code == 404

    async def test_run_detail_wrong_slug(self, action_client: AsyncClient) -> None:
        resp = await action_client.get("/api/actions/nonexistent/runs/1")
        assert resp.status_code == 404


class TestActionStatus:
    async def test_status_not_running(self, action_client: AsyncClient) -> None:
        resp = await action_client.get("/api/actions/test-action/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["slug"] == "test-action"
        assert body["running"] is False

    async def test_status_not_found(self, action_client: AsyncClient) -> None:
        resp = await action_client.get("/api/actions/nonexistent/status")
        assert resp.status_code == 404


class TestToggleAction:
    async def test_toggle(self, action_client: AsyncClient) -> None:
        # Starts enabled
        resp = await action_client.post("/api/actions/test-action/toggle")
        assert resp.status_code == 200
        body = resp.json()
        assert body["slug"] == "test-action"
        assert body["enabled"] is False

        # Toggle back on
        resp = await action_client.post("/api/actions/test-action/toggle")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True

    async def test_toggle_not_found(self, action_client: AsyncClient) -> None:
        resp = await action_client.post("/api/actions/nonexistent/toggle")
        assert resp.status_code == 404
