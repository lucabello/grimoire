"""SQLModel table definitions and database engine setup."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import Field, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Check results
# ---------------------------------------------------------------------------


class CheckRunRecord(SQLModel, table=True):
    """Metadata for a complete check run (one execution across all targets)."""

    __tablename__ = "check_run"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    check_slug: str = Field(index=True)
    check_name: str
    triggered_by: str  # "manual" | "cron" | "refresh"
    status: str = "running"  # "running" | "completed"
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: Optional[datetime] = None


class CheckResultRecord(SQLModel, table=True):
    """Persistent record of a single check execution."""

    __tablename__ = "check_result"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    run_id: int | None = Field(default=None, index=True, foreign_key="check_run.id")
    check_slug: str = Field(index=True)
    check_name: str
    repo_full_name: str = Field(index=True)
    branch: str
    passed: bool
    output: str = ""
    timestamp: datetime = Field(default_factory=_utcnow)


class CheckToggleRecord(SQLModel, table=True):
    """Persistent enabled/disabled state for a check."""

    __tablename__ = "check_toggle"  # type: ignore[assignment]

    check_slug: str = Field(primary_key=True)
    enabled: bool = True


class ActionToggleRecord(SQLModel, table=True):
    """Persistent enabled/disabled state for an action."""

    __tablename__ = "action_toggle"  # type: ignore[assignment]

    action_slug: str = Field(primary_key=True)
    enabled: bool = True


# ---------------------------------------------------------------------------
# Action runs
# ---------------------------------------------------------------------------


class ActionRunRecord(SQLModel, table=True):
    """Metadata for a complete action run."""

    __tablename__ = "action_run"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    action_slug: str = Field(index=True)
    action_name: str
    triggered_by: str  # "manual" | "cron" | "api"
    status: str = "running"  # "running" | "completed" | "failed"
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: Optional[datetime] = None


class ActionRunRepoRecord(SQLModel, table=True):
    """Per-repo result within an action run."""

    __tablename__ = "action_run_repo"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(index=True, foreign_key="action_run.id")
    repo_full_name: str
    branch: str
    passed: bool
    output: str = ""


# ---------------------------------------------------------------------------
# Cached GitHub data
# ---------------------------------------------------------------------------


class CachedRepository(SQLModel, table=True):
    """Cached repository metadata from GitHub."""

    __tablename__ = "cached_repository"  # type: ignore[assignment]

    full_name: str = Field(primary_key=True)
    default_branch: str = "main"
    archived: bool = False
    source: str = "static"
    branches_json: str = "[]"  # JSON-encoded list of observed branches
    open_issues: int = 0
    stale_issues: int = 0
    open_pull_requests: int = 0
    stale_pull_requests: int = 0
    last_commit_at: Optional[datetime] = None
    total_branches: int = 0
    workflow_include_json: str = "[]"  # JSON-encoded glob patterns
    workflow_exclude_json: str = "[]"  # JSON-encoded glob patterns
    fetched_at: datetime = Field(default_factory=_utcnow)


class CachedIssue(SQLModel, table=True):
    """Cached open issue."""

    __tablename__ = "cached_issue"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    repo_full_name: str = Field(index=True)
    title: str
    number: int
    url: str
    author: str = ""
    created_at: datetime
    last_comment_at: Optional[datetime] = None
    fetched_at: datetime = Field(default_factory=_utcnow)


class CachedPullRequest(SQLModel, table=True):
    """Cached open pull request."""

    __tablename__ = "cached_pull_request"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    repo_full_name: str = Field(index=True)
    title: str
    number: int
    url: str
    author: str = ""
    created_at: datetime
    last_push_at: Optional[datetime] = None
    last_comment_at: Optional[datetime] = None
    fetched_at: datetime = Field(default_factory=_utcnow)


class CachedOpenPullRequest(SQLModel, table=True):
    """Cached open pull request shown on the PRs page (every open PR of a tracked repo)."""

    __tablename__ = "cached_open_pull_request"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    repo_full_name: str = Field(index=True)
    number: int
    title: str
    url: str
    author: str = ""
    created_at: datetime
    updated_at: Optional[datetime] = None
    draft: bool = False
    labels_json: str = "[]"  # JSON-encoded list of label names
    base_ref: str = ""
    head_ref: str = ""
    head_sha: str = ""
    auto_merge: bool = False
    ci_state: Optional[str] = None
    review_decision: Optional[str] = None
    merge_state: Optional[str] = None
    ci_fetched_at: Optional[datetime] = None
    fetched_at: datetime = Field(default_factory=_utcnow)


class CachedWorkflowStatus(SQLModel, table=True):
    """Cached workflow run status."""

    __tablename__ = "cached_workflow_status"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    repo_full_name: str = Field(index=True)
    workflow_name: str
    branch: str
    status: str  # "success" | "failure" | "pending" | "unknown"
    url: str = ""
    run_url: str = ""
    run_id: Optional[int] = None
    run_attempt: Optional[int] = None
    event: Optional[str] = None
    head_sha: Optional[str] = None
    conclusion: Optional[str] = None
    run_created_at: Optional[datetime] = None
    run_started_at: Optional[datetime] = None
    run_updated_at: Optional[datetime] = None
    fetched_at: datetime = Field(default_factory=_utcnow)


class CachedETag(SQLModel, table=True):
    """GitHub API ETag for conditional requests."""

    __tablename__ = "cached_etag"  # type: ignore[assignment]

    endpoint_url: str = Field(primary_key=True)
    etag: str = ""
    last_modified: str = ""


# ---------------------------------------------------------------------------
# Engine & session helpers
# ---------------------------------------------------------------------------


async def get_engine(database_path: str | None = None) -> AsyncEngine:
    """Create an async SQLite engine."""
    db_path = database_path or "grimoire.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    return create_async_engine(url, echo=False)


MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# First revision: the schema as created by `create_all` before migrations existed.
BASELINE_REVISION = "89154088af06"
# Second revision: adds the run-detail columns to `cached_workflow_status`.
RUN_DETAILS_REVISION = "ffa5587a73e6"


def alembic_config(connection: Connection) -> Config:
    """Build an in-code Alembic config that runs on an existing sync connection."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["connection"] = connection
    return config


def _upgrade_to_head(connection: Connection) -> None:
    config = alembic_config(connection)
    inspector = inspect(connection)
    tables = inspector.get_table_names()
    if tables and "alembic_version" not in tables:
        # A database created by `create_all` before migrations were adopted. Its real
        # schema decides where it sits in the history. It is stamped with the revision
        # whose schema it has, never "head": later revisions must still be applied.
        columns = {c["name"] for c in inspector.get_columns("cached_workflow_status")}
        revision = RUN_DETAILS_REVISION if "run_id" in columns else BASELINE_REVISION
        command.stamp(config, revision)
    command.upgrade(config, "head")


async def upgrade_database(engine: AsyncEngine) -> None:
    """Bring the database schema to the latest revision, creating it if needed.

    Handles fresh databases, databases created before migrations existed (adopted in
    place, keeping their data) and already versioned databases.
    """
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade_to_head)


async def get_session(engine: AsyncEngine) -> AsyncSession:
    """Get a new async session."""
    return AsyncSession(engine)


async def cleanup_stale_runs(engine: AsyncEngine) -> None:
    """Mark any 'running' check/action run records as 'interrupted'.

    Called at startup — if the app was restarted while runs were in progress,
    the in-memory tracking is gone and the DB records would be stuck forever.
    """
    from sqlalchemy import text

    async with AsyncSession(engine) as session:
        now = _utcnow()
        await session.execute(
            text(
                "UPDATE check_run SET status = 'interrupted', finished_at = :now "
                "WHERE status = 'running'"
            ),
            params={"now": now},
        )
        await session.execute(
            text(
                "UPDATE action_run SET status = 'interrupted', finished_at = :now "
                "WHERE status = 'running'"
            ),
            params={"now": now},
        )
        await session.commit()


async def restore_check_toggles(engine: AsyncEngine, checks: Sequence[Any]) -> None:
    """Apply persisted toggle state to in-memory check definitions on startup."""
    async with AsyncSession(engine) as session:
        records = (await session.exec(select(CheckToggleRecord))).all()
        toggle_map = {r.check_slug: r.enabled for r in records}
    for check in checks:
        if check.slug in toggle_map:
            check.enabled = toggle_map[check.slug]


async def restore_action_toggles(engine: AsyncEngine, actions: Sequence[Any]) -> None:
    """Apply persisted toggle state to in-memory action definitions on startup."""
    async with AsyncSession(engine) as session:
        records = (await session.exec(select(ActionToggleRecord))).all()
        toggle_map = {r.action_slug: r.enabled for r in records}
    for action in actions:
        if action.slug in toggle_map:
            action.enabled = toggle_map[action.slug]
