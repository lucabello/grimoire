"""Databases in the states Grimoire has shipped, for the migration tests."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from grimoire.database import MIGRATIONS_DIR, CachedWorkflowStatus, CheckToggleRecord, get_engine

# The schema created by `SQLModel.metadata.create_all` before migrations existed
# (releases up to 0.3.1). Kept verbatim: it is what real legacy databases look like.
LEGACY_SCHEMA = """
CREATE TABLE action_run (
    id INTEGER NOT NULL, action_slug VARCHAR NOT NULL, action_name VARCHAR NOT NULL,
    triggered_by VARCHAR NOT NULL, status VARCHAR NOT NULL, started_at DATETIME NOT NULL,
    finished_at DATETIME, PRIMARY KEY (id)
);
CREATE TABLE action_run_repo (
    id INTEGER NOT NULL, run_id INTEGER NOT NULL, repo_full_name VARCHAR NOT NULL,
    branch VARCHAR NOT NULL, passed BOOLEAN NOT NULL, output VARCHAR NOT NULL,
    PRIMARY KEY (id), FOREIGN KEY(run_id) REFERENCES action_run (id)
);
CREATE TABLE action_toggle (
    action_slug VARCHAR NOT NULL, enabled BOOLEAN NOT NULL, PRIMARY KEY (action_slug)
);
CREATE TABLE cached_etag (
    endpoint_url VARCHAR NOT NULL, etag VARCHAR NOT NULL, last_modified VARCHAR NOT NULL,
    PRIMARY KEY (endpoint_url)
);
CREATE TABLE cached_issue (
    id INTEGER NOT NULL, repo_full_name VARCHAR NOT NULL, title VARCHAR NOT NULL,
    number INTEGER NOT NULL, url VARCHAR NOT NULL, author VARCHAR NOT NULL,
    created_at DATETIME NOT NULL, last_comment_at DATETIME, fetched_at DATETIME NOT NULL,
    PRIMARY KEY (id)
);
CREATE TABLE cached_pull_request (
    id INTEGER NOT NULL, repo_full_name VARCHAR NOT NULL, title VARCHAR NOT NULL,
    number INTEGER NOT NULL, url VARCHAR NOT NULL, author VARCHAR NOT NULL,
    created_at DATETIME NOT NULL, last_push_at DATETIME, last_comment_at DATETIME,
    fetched_at DATETIME NOT NULL, PRIMARY KEY (id)
);
CREATE TABLE cached_repository (
    full_name VARCHAR NOT NULL, default_branch VARCHAR NOT NULL, archived BOOLEAN NOT NULL,
    source VARCHAR NOT NULL, branches_json VARCHAR NOT NULL, open_issues INTEGER NOT NULL,
    stale_issues INTEGER NOT NULL, open_pull_requests INTEGER NOT NULL,
    stale_pull_requests INTEGER NOT NULL, last_commit_at DATETIME,
    total_branches INTEGER NOT NULL, workflow_include_json VARCHAR NOT NULL,
    workflow_exclude_json VARCHAR NOT NULL, fetched_at DATETIME NOT NULL,
    PRIMARY KEY (full_name)
);
CREATE TABLE cached_workflow_status (
    id INTEGER NOT NULL, repo_full_name VARCHAR NOT NULL, workflow_name VARCHAR NOT NULL,
    branch VARCHAR NOT NULL, status VARCHAR NOT NULL, url VARCHAR NOT NULL,
    run_url VARCHAR NOT NULL, fetched_at DATETIME NOT NULL, PRIMARY KEY (id)
);
CREATE TABLE check_result (
    id INTEGER NOT NULL, run_id INTEGER, check_slug VARCHAR NOT NULL,
    check_name VARCHAR NOT NULL, repo_full_name VARCHAR NOT NULL, branch VARCHAR NOT NULL,
    passed BOOLEAN NOT NULL, output VARCHAR NOT NULL, timestamp DATETIME NOT NULL,
    PRIMARY KEY (id), FOREIGN KEY(run_id) REFERENCES check_run (id)
);
CREATE TABLE check_run (
    id INTEGER NOT NULL, check_slug VARCHAR NOT NULL, check_name VARCHAR NOT NULL,
    triggered_by VARCHAR NOT NULL, status VARCHAR NOT NULL, started_at DATETIME NOT NULL,
    finished_at DATETIME, PRIMARY KEY (id)
);
CREATE TABLE check_toggle (
    check_slug VARCHAR NOT NULL, enabled BOOLEAN NOT NULL, PRIMARY KEY (check_slug)
);
CREATE INDEX ix_action_run_action_slug ON action_run (action_slug);
CREATE INDEX ix_action_run_repo_run_id ON action_run_repo (run_id);
CREATE INDEX ix_cached_issue_repo_full_name ON cached_issue (repo_full_name);
CREATE INDEX ix_cached_pull_request_repo_full_name ON cached_pull_request (repo_full_name);
CREATE INDEX ix_cached_workflow_status_repo_full_name ON cached_workflow_status (repo_full_name);
CREATE INDEX ix_check_result_check_slug ON check_result (check_slug);
CREATE INDEX ix_check_result_repo_full_name ON check_result (repo_full_name);
CREATE INDEX ix_check_result_run_id ON check_result (run_id);
CREATE INDEX ix_check_run_check_slug ON check_run (check_slug);
"""

LEGACY_DATA = """
INSERT INTO check_toggle VALUES ('lint', 0), ('license', 1);
INSERT INTO action_toggle VALUES ('bump', 0);
INSERT INTO check_run VALUES (1, 'lint', 'Lint', 'manual', 'completed',
    '2026-03-01 10:00:00.000000', '2026-03-01 10:00:05.000000');
INSERT INTO check_result VALUES (1, 1, 'lint', 'Lint', 'owner/repo1', 'main', 1, 'all good',
    '2026-03-01 10:00:04.000000');
INSERT INTO action_run VALUES (1, 'bump', 'Bump', 'api', 'completed',
    '2026-03-02 09:00:00.000000', '2026-03-02 09:00:09.000000');
INSERT INTO action_run_repo VALUES (1, 1, 'owner/repo1', 'main', 0, 'boom');
INSERT INTO cached_repository VALUES ('owner/repo1', 'main', 0, 'static', '["main"]', 4, 1, 2, 0,
    '2026-03-01 08:00:00.000000', 1, '[]', '[]', '{fetched_at}');
INSERT INTO cached_issue VALUES (1, 'owner/repo1', 'Bug', 7, 'https://example.com/7', 'ann',
    '2026-02-01 00:00:00.000000', NULL, '{fetched_at}');
INSERT INTO cached_workflow_status VALUES (1, 'owner/repo1', 'CI', 'main', 'success',
    'https://example.com/ci', 'https://example.com/runs/1', '{fetched_at}');
INSERT INTO cached_etag VALUES ('https://api.github.com/x', 'abc', 'yesterday');
"""

RUN_DETAIL_COLUMNS = {
    "run_id",
    "run_attempt",
    "event",
    "head_sha",
    "conclusion",
    "run_created_at",
    "run_started_at",
    "run_updated_at",
}


def _fetched_at() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def create_legacy_database(path: Path) -> None:
    """Create a database as released up to 0.3.1: no `alembic_version`, no run details."""
    with closing(sqlite3.connect(path)) as con:
        con.executescript(LEGACY_SCHEMA)
        con.executescript(LEGACY_DATA.format(fetched_at=_fetched_at()))
        con.commit()


async def create_unversioned_current_database(path: Path) -> None:
    """Create a database as `create_all` made it after the run details, before migrations."""
    engine = await get_engine(str(path))
    # Tables added by later revisions did not exist yet
    tables = [t for t in SQLModel.metadata.sorted_tables if t.name != "cached_open_pull_request"]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.create_all(c, tables=tables))
    async with AsyncSession(engine) as session:
        session.add(CheckToggleRecord(check_slug="lint", enabled=False))
        session.add(
            CachedWorkflowStatus(
                repo_full_name="owner/repo1",
                workflow_name="CI",
                branch="main",
                status="failure",
                run_id=99,
                conclusion="timed_out",
            )
        )
        await session.commit()
    await engine.dispose()


def read_all(path: Path) -> dict[str, list[tuple]]:
    """Every table's rows, for before/after comparisons."""
    with closing(sqlite3.connect(path)) as con:
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name != 'alembic_version'"
            )
        ]
        return {t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables}


def columns(path: Path, table: str) -> set[str]:
    with closing(sqlite3.connect(path)) as con:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def schema_snapshot(path: Path) -> list[tuple]:
    with closing(sqlite3.connect(path)) as con:
        return con.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()


def revision_of(path: Path) -> str:
    with closing(sqlite3.connect(path)) as con:
        (revision,) = con.execute("SELECT version_num FROM alembic_version").fetchone()
        return revision


def head_revision() -> str | None:
    return ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()
