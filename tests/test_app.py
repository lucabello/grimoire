"""Tests for the FastAPI app factory."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import respx
from httpx import ASGITransport, AsyncClient

from grimoire.app import create_app
from grimoire.github.router import update_cache
from grimoire.workspace.manager import WorkspaceManager
from tests.legacy_db import create_legacy_database, head_revision, revision_of


async def test_root_returns_ok(async_client: AsyncClient) -> None:
    resp = await async_client.get("/")
    assert resp.status_code == 200
    # Dashboard now returns HTML
    assert "text/html" in resp.headers.get("content-type", "")


async def test_health_returns_ok(async_client: AsyncClient) -> None:
    resp = await async_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


async def test_startup_migrates_legacy_database(
    tmp_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An app started on a pre-migration database upgrades it and serves its cached data."""
    db = tmp_config.parent / "grimoire.db"
    create_legacy_database(db)
    monkeypatch.setenv("GRIMOIRE_CONFIG", str(tmp_config))

    async def no_clone(self: WorkspaceManager, repos: object) -> None:
        return None

    monkeypatch.setattr(WorkspaceManager, "setup", no_clone)

    app = create_app()
    try:
        with respx.mock:  # any GitHub request would fail the test
            async with app.router.lifespan_context(app):
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    resp = await client.get("/api/repos/owner/repo1")
    finally:
        update_cache([], [])

    assert resp.status_code == 200
    body = resp.json()
    assert body["open_issues"] == 4
    assert [w["name"] for w in body["workflows"]] == ["CI"]
    assert body["workflows"][0]["run_id"] is None
    assert revision_of(db) == head_revision()
    with closing(sqlite3.connect(db)) as con:
        assert con.execute(
            "SELECT enabled FROM check_toggle WHERE check_slug='lint'"
        ).fetchall() == [(0,)]
