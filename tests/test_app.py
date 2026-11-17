"""Tests for the FastAPI app factory."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import respx
from httpx import ASGITransport, AsyncClient

from grimoire.app import create_app
from grimoire.database import get_engine, upgrade_database
from grimoire.github.router import update_cache
from grimoire.github.service import save_stats_to_db
from grimoire.models import RepositoryStats, TrackedRepository
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


@pytest.mark.parametrize(
    ("age", "expect_refresh"),
    [(timedelta(minutes=1), False), (timedelta(hours=1), True)],
    ids=["fresh", "stale"],
)
async def test_startup_cache_freshness(
    tmp_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    age: timedelta,
    expect_refresh: bool,
) -> None:
    """A recent cache is used as is after a restart; an old one is refreshed in the background."""
    engine = await get_engine(str(tmp_config.parent / "grimoire.db"))
    await upgrade_database(engine)
    repo = TrackedRepository(full_name="owner/repo1", default_branch="main", branches=["main"])
    stats = RepositoryStats(
        full_name="owner/repo1", default_branch="main", fetched_at=datetime.now(UTC) - age
    )
    await save_stats_to_db(engine, [stats], [repo])
    await engine.dispose()

    monkeypatch.setenv("GRIMOIRE_CONFIG", str(tmp_config))
    refreshes: list[object] = []

    async def no_clone(self: WorkspaceManager, repos: object) -> None:
        return None

    async def record_refresh(config: object, client: object) -> None:
        refreshes.append(config)
        raise RuntimeError("refresh stubbed out")

    monkeypatch.setattr(WorkspaceManager, "setup", no_clone)
    monkeypatch.setattr("grimoire.app.refresh_all_stats", record_refresh)

    app = create_app()
    try:
        with caplog.at_level(logging.INFO, logger="grimoire.app"):
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0)
                await asyncio.sleep(0)
    finally:
        update_cache([], [])

    messages = [r.getMessage() for r in caplog.records]
    assert "Data loading failed — falling back to cached data" not in messages
    assert bool(refreshes) is expect_refresh
    assert any(m.startswith("DB cache: 1 repos") for m in messages)
    assert any(
        m.startswith("Serving stale cache" if expect_refresh else "Using cached data")
        for m in messages
    )
