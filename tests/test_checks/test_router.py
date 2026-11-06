"""Tests for the checks REST API router."""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from grimoire.app import create_app
from grimoire.checks.loader import CheckDefinition
from grimoire.checks.router import router, set_checks_state
from grimoire.database import get_engine, upgrade_database
from grimoire.models import TrackedRepository
from grimoire.targeting import TargetSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockWorkspace:
    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir

    async def reset_workdir(self, full_name: str, branch: str) -> Path:
        return self._workdir

    def get_workdir(self, full_name: str, branch: str) -> Path:
        return self._workdir

    def get_env(self) -> dict[str, str]:
        return {"GH_TOKEN": "test", "GITHUB_TOKEN": "test"}


def _check(**overrides: object) -> CheckDefinition:
    defaults: dict[str, object] = {
        "name": "Test Check",
        "slug": "test-check",
        "description": "A test check",
        "targets": TargetSpec(list=["acme/repo"]),
        "script": "echo ok",
        "enabled": True,
    }
    defaults.update(overrides)
    return CheckDefinition.model_validate(defaults)


def _repo() -> TrackedRepository:
    return TrackedRepository(full_name="acme/repo", default_branch="main")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def check_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """Provide an async HTTP client wired to a test app with the checks router."""
    engine = await get_engine(str(tmp_path / "test.db"))
    await upgrade_database(engine)

    checks = [_check()]
    repos = [_repo()]
    ws = MockWorkspace(tmp_path)

    set_checks_state(checks, repos, ws, engine)  # type: ignore[arg-type]

    app = create_app()
    app.include_router(router, prefix="/api")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListChecks:
    async def test_list_checks(self, check_client: AsyncClient) -> None:
        resp = await check_client.get("/api/checks/")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["slug"] == "test-check"
        assert data[0]["name"] == "Test Check"
        assert data[0]["enabled"] is True


class TestGetResults:
    async def test_results_empty(self, check_client: AsyncClient) -> None:
        resp = await check_client.get("/api/checks/test-check/results")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_results_not_found(self, check_client: AsyncClient) -> None:
        resp = await check_client.get("/api/checks/nonexistent/results")
        assert resp.status_code == 404


class TestToggleCheck:
    async def test_toggle(self, check_client: AsyncClient) -> None:
        # Starts enabled
        resp = await check_client.get("/api/checks/")
        assert resp.json()[0]["enabled"] is True

        # Toggle off
        resp = await check_client.post("/api/checks/test-check/toggle")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is False

        # Toggle back on
        resp = await check_client.post("/api/checks/test-check/toggle")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True

    async def test_toggle_not_found(self, check_client: AsyncClient) -> None:
        resp = await check_client.post("/api/checks/nonexistent/toggle")
        assert resp.status_code == 404


class TestRunCheck:
    async def test_run_check(self, check_client: AsyncClient) -> None:
        resp = await check_client.post("/api/checks/test-check/run")
        assert resp.status_code == 200
        body = resp.json()
        assert body["check_slug"] == "test-check"
        assert body["check_name"] == "Test Check"
        assert body["triggered_by"] == "manual"
        assert body["status"] == "running"
        assert body["results"] == []

    async def test_run_check_not_found(self, check_client: AsyncClient) -> None:
        resp = await check_client.post("/api/checks/nonexistent/run")
        assert resp.status_code == 404

    async def test_run_check_with_repo_filter(self, check_client: AsyncClient) -> None:
        resp = await check_client.post("/api/checks/test-check/run?repo=acme/repo")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "running"
        assert body["results"] == []

    async def test_run_then_get_results(self, check_client: AsyncClient) -> None:
        await check_client.post("/api/checks/test-check/run")
        resp = await check_client.get("/api/checks/test-check/results")
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) == 1
        assert results[0]["check_slug"] == "test-check"
        assert results[0]["passed"] is True


class TestCheckRunHistory:
    async def test_runs_empty(self, check_client: AsyncClient) -> None:
        resp = await check_client.get("/api/checks/test-check/runs")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_runs_after_execution(self, check_client: AsyncClient) -> None:
        await check_client.post("/api/checks/test-check/run")
        resp = await check_client.get("/api/checks/test-check/runs")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["check_slug"] == "test-check"
        assert data[0]["status"] == "completed"
        assert data[0]["total_repos"] == 1
        assert data[0]["passed_repos"] == 1

    async def test_runs_not_found(self, check_client: AsyncClient) -> None:
        resp = await check_client.get("/api/checks/nonexistent/runs")
        assert resp.status_code == 404


class TestCheckRunDetail:
    async def test_run_detail(self, check_client: AsyncClient) -> None:
        await check_client.post("/api/checks/test-check/run")

        runs_resp = await check_client.get("/api/checks/test-check/runs")
        runs = runs_resp.json()
        assert len(runs) >= 1
        run_id = runs[0]["id"]

        resp = await check_client.get(f"/api/checks/test-check/runs/{run_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == run_id
        assert body["check_slug"] == "test-check"
        assert len(body["results"]) == 1
        assert body["results"][0]["passed"] is True

    async def test_run_detail_not_found(self, check_client: AsyncClient) -> None:
        resp = await check_client.get("/api/checks/test-check/runs/99999")
        assert resp.status_code == 404


class TestCheckStatus:
    async def test_status_not_running(self, check_client: AsyncClient) -> None:
        resp = await check_client.get("/api/checks/test-check/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["slug"] == "test-check"
        assert body["running"] is False

    async def test_status_running(self, check_client: AsyncClient) -> None:
        from grimoire.checks.engine import CheckProgress, _running_checks

        _running_checks["test-check"] = CheckProgress()
        try:
            resp = await check_client.get("/api/checks/test-check/status")
            assert resp.status_code == 200
            assert resp.json()["running"] is True
        finally:
            _running_checks.pop("test-check", None)

    async def test_status_not_found(self, check_client: AsyncClient) -> None:
        resp = await check_client.get("/api/checks/nonexistent/status")
        assert resp.status_code == 404
