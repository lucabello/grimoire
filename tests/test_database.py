"""Tests for the database setup."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from grimoire.database import CheckToggleRecord, get_engine, get_session, upgrade_database
from grimoire.github.service import load_stats_from_db, save_stats_to_db
from tests.legacy_db import (
    RUN_DETAIL_COLUMNS,
    columns,
    create_legacy_database,
    create_unversioned_current_database,
    head_revision,
    read_all,
    revision_of,
    schema_snapshot,
)


@pytest.fixture
async def engine(tmp_path) -> AsyncEngine:
    """Create a temporary in-memory database engine."""
    db_path = str(tmp_path / "test.db")
    eng = await get_engine(db_path)
    await upgrade_database(eng)
    return eng


async def test_engine_creation(engine: AsyncEngine) -> None:
    """Engine is created and usable."""
    assert engine is not None


async def test_tables_created(engine: AsyncEngine) -> None:
    """All expected tables exist after upgrade_database."""
    from sqlmodel import text

    session = await get_session(engine)
    async with session:
        result = await session.exec(
            text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")  # type: ignore[arg-type]
        )
        tables = {row[0] for row in result.all()}

    expected = {
        "check_run",
        "check_result",
        "check_toggle",
        "action_run",
        "action_run_repo",
        "cached_repository",
        "cached_issue",
        "cached_pull_request",
        "cached_workflow_status",
        "cached_etag",
    }
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"


async def test_session_usable(engine: AsyncEngine) -> None:
    """Can create a session and execute a simple query."""
    from grimoire.database import CheckToggleRecord

    session = await get_session(engine)
    async with session:
        record = CheckToggleRecord(check_slug="test-check", enabled=True)
        session.add(record)
        await session.commit()

        from sqlmodel import select

        result = await session.exec(select(CheckToggleRecord))
        rows = result.all()
        assert len(rows) == 1
        assert rows[0].check_slug == "test-check"
        assert rows[0].enabled is True


async def test_cleanup_stale_runs(engine: AsyncEngine) -> None:
    """cleanup_stale_runs marks 'running' records as 'interrupted'."""
    from grimoire.database import (
        ActionRunRecord,
        CheckRunRecord,
        cleanup_stale_runs,
    )

    session = await get_session(engine)
    async with session:
        session.add(
            CheckRunRecord(
                check_slug="c1", check_name="C1", triggered_by="manual", status="running"
            )
        )
        session.add(
            CheckRunRecord(
                check_slug="c2", check_name="C2", triggered_by="cron", status="completed"
            )
        )
        session.add(
            ActionRunRecord(
                action_slug="a1", action_name="A1", triggered_by="manual", status="running"
            )
        )
        await session.commit()

    await cleanup_stale_runs(engine)

    from sqlmodel import select

    session = await get_session(engine)
    async with session:
        c1 = (
            await session.exec(select(CheckRunRecord).where(CheckRunRecord.check_slug == "c1"))
        ).first()
        assert c1 is not None
        assert c1.status == "interrupted"
        assert c1.finished_at is not None

        c2 = (
            await session.exec(select(CheckRunRecord).where(CheckRunRecord.check_slug == "c2"))
        ).first()
        assert c2 is not None
        assert c2.status == "completed"

        a1 = (
            await session.exec(select(ActionRunRecord).where(ActionRunRecord.action_slug == "a1"))
        ).first()
        assert a1 is not None
        assert a1.status == "interrupted"
        assert a1.finished_at is not None


async def test_restore_check_toggles(engine: AsyncEngine) -> None:
    """restore_check_toggles applies persisted enabled state to check definitions."""
    from grimoire.database import CheckToggleRecord, restore_check_toggles

    session = await get_session(engine)
    async with session:
        session.add(CheckToggleRecord(check_slug="check-a", enabled=False))
        session.add(CheckToggleRecord(check_slug="check-b", enabled=True))
        await session.commit()

    # Simulate in-memory check definitions (duck-typed)
    class FakeCheck:
        def __init__(self, slug: str, enabled: bool = True) -> None:
            self.slug = slug
            self.enabled = enabled

    checks = [FakeCheck("check-a"), FakeCheck("check-b"), FakeCheck("check-c")]
    await restore_check_toggles(engine, checks)

    assert checks[0].enabled is False  # restored from DB
    assert checks[1].enabled is True  # restored (already True)
    assert checks[2].enabled is True  # no DB record, stays default


async def test_restore_action_toggles(engine: AsyncEngine) -> None:
    """restore_action_toggles applies persisted enabled state to action definitions."""
    from grimoire.database import ActionToggleRecord, restore_action_toggles

    session = await get_session(engine)
    async with session:
        session.add(ActionToggleRecord(action_slug="act-x", enabled=False))
        await session.commit()

    class FakeAction:
        def __init__(self, slug: str, enabled: bool = True) -> None:
            self.slug = slug
            self.enabled = enabled

    actions = [FakeAction("act-x"), FakeAction("act-y")]
    await restore_action_toggles(engine, actions)

    assert actions[0].enabled is False  # restored from DB
    assert actions[1].enabled is True  # no DB record, stays default


# ---------------------------------------------------------------------------
# upgrade_database
# ---------------------------------------------------------------------------


async def test_upgrade_creates_fresh_database(tmp_path: Path) -> None:
    """A database without tables ends at head with every table."""
    db = tmp_path / "fresh.db"
    engine = await get_engine(str(db))
    await upgrade_database(engine)
    await engine.dispose()

    assert revision_of(db) == head_revision()
    assert set(read_all(db)) == set(SQLModel.metadata.tables)
    assert RUN_DETAIL_COLUMNS <= columns(db, "cached_workflow_status")


async def test_upgrade_legacy_database_keeps_data(tmp_path: Path) -> None:
    """A pre-migration database gains the run-detail columns and keeps toggles and history."""
    db = tmp_path / "legacy.db"
    create_legacy_database(db)
    before = read_all(db)
    assert not RUN_DETAIL_COLUMNS & columns(db, "cached_workflow_status")

    engine = await get_engine(str(db))
    await upgrade_database(engine)

    assert revision_of(db) == head_revision()
    assert RUN_DETAIL_COLUMNS <= columns(db, "cached_workflow_status")
    after = read_all(db)
    for table, rows in before.items():
        if table == "cached_workflow_status":
            continue
        assert after[table] == rows, table
    # Old workflow rows survive and the new columns are NULL.
    (row,) = after["cached_workflow_status"]
    assert row[:7] == before["cached_workflow_status"][0][:7]
    with closing(sqlite3.connect(db)) as con:
        cols = ", ".join(sorted(RUN_DETAIL_COLUMNS))
        assert con.execute(f"SELECT {cols} FROM cached_workflow_status").fetchone() == (
            (None,) * 8
        )

    async with AsyncSession(engine) as session:
        toggles = {
            t.check_slug: t.enabled for t in (await session.exec(select(CheckToggleRecord)))
        }
        assert toggles == {"lint": False, "license": True}

    # The cache the app reads and writes on startup works against the upgraded schema.
    repos, stats = await load_stats_from_db(engine)
    assert [r.full_name for r in repos] == ["owner/repo1"]
    assert stats[0].workflows[0].name == "CI"
    assert stats[0].workflows[0].run_id is None
    await save_stats_to_db(engine, stats, repos)
    _, reloaded = await load_stats_from_db(engine)
    assert reloaded[0].workflows[0].name == "CI"
    await engine.dispose()


async def test_upgrade_unversioned_current_database_is_stamped(tmp_path: Path) -> None:
    """A database made by `create_all` after the run details is adopted without changes."""
    db = tmp_path / "current.db"
    await create_unversioned_current_database(db)
    before = read_all(db)
    schema = schema_snapshot(db)

    engine = await get_engine(str(db))
    await upgrade_database(engine)
    await engine.dispose()

    assert revision_of(db) == head_revision()
    assert read_all(db) == before
    assert [s for s in schema_snapshot(db) if "alembic_version" not in s[1]] == schema


@pytest.mark.parametrize("state", ["fresh", "legacy", "unversioned"])
async def test_upgrade_is_idempotent(tmp_path: Path, state: str) -> None:
    """Upgrading an already upgraded database changes nothing."""
    db = tmp_path / "grimoire.db"
    if state == "legacy":
        create_legacy_database(db)
    elif state == "unversioned":
        await create_unversioned_current_database(db)

    engine = await get_engine(str(db))
    await upgrade_database(engine)
    schema, data = schema_snapshot(db), read_all(db)
    await upgrade_database(engine)
    await engine.dispose()

    assert revision_of(db) == head_revision()
    assert schema_snapshot(db) == schema
    assert read_all(db) == data
