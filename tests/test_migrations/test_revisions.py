"""Tests for the Alembic revision history."""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection
from sqlmodel import SQLModel

import grimoire.database
from grimoire.database import (
    BASELINE_REVISION,
    MIGRATIONS_DIR,
    RUN_DETAILS_REVISION,
    alembic_config,
    get_engine,
    upgrade_database,
)
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


def model_drift(connection: Connection) -> list:
    context = MigrationContext.configure(connection, opts={"compare_type": True})
    return compare_metadata(context, SQLModel.metadata)


async def test_history_is_linear_from_the_baseline() -> None:
    """One head, one root, and the root is the baseline legacy databases are stamped with."""
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    assert len(script.get_heads()) == 1
    assert script.get_bases() == [BASELINE_REVISION]


async def test_migrated_schema_matches_models(tmp_path: Path) -> None:
    """A model change without a revision fails here: run `just migration "message"`."""
    engine = await get_engine(str(tmp_path / "grimoire.db"))
    await upgrade_database(engine)
    async with engine.connect() as conn:
        assert await conn.run_sync(model_drift) == []
    await engine.dispose()


async def test_drift_check_detects_missing_revision(tmp_path: Path) -> None:
    """The drift check is not vacuous: a database one revision behind differs from the models."""
    engine = await get_engine(str(tmp_path / "grimoire.db"))
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: command.upgrade(alembic_config(c), BASELINE_REVISION))
    async with engine.connect() as conn:
        assert await conn.run_sync(model_drift) != []
    await engine.dispose()


async def test_downgrade_of_run_details_keeps_rows(tmp_path: Path) -> None:
    """Downgrading the run-details revision drops only its columns of a populated DB."""
    db = tmp_path / "grimoire.db"
    create_legacy_database(db)
    engine = await get_engine(str(db))

    def upgrade_to_run_details(connection: Connection) -> None:
        config = alembic_config(connection)
        command.stamp(config, BASELINE_REVISION)
        command.upgrade(config, RUN_DETAILS_REVISION)

    async with engine.begin() as conn:
        await conn.run_sync(upgrade_to_run_details)
    with closing(sqlite3.connect(db)) as con:
        con.execute(
            "INSERT INTO cached_workflow_status "
            "(repo_full_name, workflow_name, branch, status, url, run_url, fetched_at, run_id) "
            "VALUES ('owner/repo2', 'Build', 'main', 'failure', '', '', '2026-03-01 00:00:00', 5)"
        )
        con.commit()
    surviving = (
        "SELECT id, repo_full_name, workflow_name, branch, status, url, run_url, fetched_at "
        "FROM cached_workflow_status ORDER BY id"
    )
    with closing(sqlite3.connect(db)) as con:
        workflows = con.execute(surviving).fetchall()
    assert len(workflows) == 2
    before = read_all(db)
    del before["cached_workflow_status"]

    async with engine.begin() as conn:
        await conn.run_sync(lambda c: command.downgrade(alembic_config(c), "-1"))

    assert revision_of(db) == BASELINE_REVISION
    assert not RUN_DETAIL_COLUMNS & columns(db, "cached_workflow_status")
    with closing(sqlite3.connect(db)) as con:
        assert con.execute(surviving).fetchall() == workflows
    after = read_all(db)
    del after["cached_workflow_status"]
    assert after == before
    with closing(sqlite3.connect(db)) as con:
        indexes = {r[1] for r in con.execute("PRAGMA index_list(cached_workflow_status)")}
    assert "ix_cached_workflow_status_repo_full_name" in indexes

    # The downgraded database can be upgraded again.
    await upgrade_database(engine)
    await engine.dispose()
    assert revision_of(db) != BASELINE_REVISION
    assert RUN_DETAIL_COLUMNS <= columns(db, "cached_workflow_status")


async def test_downgrade_to_base_drops_all_tables(tmp_path: Path) -> None:
    """Downgrading the baseline removes every application table."""
    db = tmp_path / "grimoire.db"
    engine = await get_engine(str(db))
    await upgrade_database(engine)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: command.downgrade(alembic_config(c), "base"))
    await engine.dispose()

    assert set(read_all(db)) == set()
    assert {s[1] for s in schema_snapshot(db) if s[0] == "table"} == {"alembic_version"}


THROWAWAY_REVISION = "0000000000aa"
THROWAWAY_REVISION_SOURCE = f'''"""add a throwaway column"""

import sqlalchemy as sa
from alembic import op

revision = "{THROWAWAY_REVISION}"
down_revision = "{head_revision()}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("cached_workflow_status") as batch_op:
        batch_op.add_column(sa.Column("throwaway", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("cached_workflow_status") as batch_op:
        batch_op.drop_column("throwaway")
'''


@pytest.mark.parametrize("state", ["legacy", "unversioned"])
async def test_adopted_database_receives_later_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """An unversioned database is stamped with the revision it has, then upgraded to head.

    The history is a copy extended by a throwaway revision, so the test keeps working
    as real revisions are added.
    """
    migrations = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, migrations, ignore=shutil.ignore_patterns("__pycache__"))
    (migrations / "versions" / f"{THROWAWAY_REVISION}_throwaway.py").write_text(
        THROWAWAY_REVISION_SOURCE
    )
    monkeypatch.setattr(grimoire.database, "MIGRATIONS_DIR", migrations)

    db = tmp_path / "grimoire.db"
    if state == "legacy":
        create_legacy_database(db)
    else:
        await create_unversioned_current_database(db)

    engine = await get_engine(str(db))
    await upgrade_database(engine)
    await engine.dispose()

    assert "throwaway" in columns(db, "cached_workflow_status")
    assert revision_of(db) == THROWAWAY_REVISION


async def test_open_pull_requests_revision_purges_only_pulls_etags(tmp_path: Path) -> None:
    """The new table starts empty, so a stored pulls ETag would keep the PR list empty on 304."""
    db = tmp_path / "grimoire.db"
    create_legacy_database(db)
    engine = await get_engine(str(db))

    def upgrade_to_run_details(connection: Connection) -> None:
        config = alembic_config(connection)
        command.stamp(config, BASELINE_REVISION)
        command.upgrade(config, RUN_DETAILS_REVISION)

    async with engine.begin() as conn:
        await conn.run_sync(upgrade_to_run_details)
    pulls_keys = [
        "/repos/acme/api/pulls?per_page=100&state=open",
        "https://api.github.com/repositories/1/pulls?per_page=100&page=2&state=open",
    ]
    other_keys = [
        "/repos/acme/api/issues?per_page=100&state=open",
        "/orgs/acme/teams/core/repos?per_page=100",
        "/repos/acme/api/pulls/7",
    ]
    with closing(sqlite3.connect(db)) as con:
        con.executemany(
            "INSERT INTO cached_etag VALUES (?, 'abc', '')",
            [(key,) for key in pulls_keys + other_keys],
        )
        con.commit()
    before = read_all(db)

    await upgrade_database(engine)

    after = read_all(db)
    assert "cached_open_pull_request" in after
    assert after.pop("cached_open_pull_request") == []
    assert {row[0] for row in before["cached_etag"]} - {
        row[0] for row in after["cached_etag"]
    } == (set(pulls_keys))
    del before["cached_etag"], after["cached_etag"]
    assert after == before
    assert revision_of(db) == head_revision()

    async with engine.begin() as conn:
        await conn.run_sync(lambda c: command.downgrade(alembic_config(c), "-1"))
    await engine.dispose()

    assert revision_of(db) == RUN_DETAILS_REVISION
    downgraded = read_all(db)
    assert "cached_open_pull_request" not in downgraded
    del downgraded["cached_etag"]
    assert downgraded == before
