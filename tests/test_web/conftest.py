"""Shared test fixtures for web route tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from grimoire.app import create_app
from grimoire.checks.loader import CheckDefinition
from grimoire.database import CheckResultRecord, CheckRunRecord, create_tables, get_engine
from grimoire.github.router import update_cache
from grimoire.models import (
    IssueDetail,
    PullRequestDetail,
    RepositoryStats,
    TrackedRepository,
    WorkflowStatus,
)
from grimoire.targeting import TargetSpec


def _populate_cache() -> None:
    """Populate the in-memory GitHub cache with test data."""
    repos = [
        TrackedRepository(
            full_name="acme/api",
            default_branch="main",
            branches=["main"],
            source="static",
        ),
        TrackedRepository(
            full_name="acme/frontend",
            default_branch="main",
            branches=["main", "develop"],
            source="static",
        ),
    ]
    stats = [
        RepositoryStats(
            full_name="acme/api",
            default_branch="main",
            open_issues=5,
            stale_issues=2,
            open_pull_requests=3,
            stale_pull_requests=1,
            workflows=[
                WorkflowStatus(
                    name="CI",
                    branch="main",
                    status="success",
                    url="https://github.com/acme/api/actions",
                    run_url="https://github.com/acme/api/actions/runs/1",
                ),
            ],
            stale_issue_items=[
                IssueDetail(
                    number=42,
                    title="Fix legacy endpoint",
                    url="https://github.com/acme/api/issues/42",
                    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    last_activity_at=datetime(2024, 3, 15, tzinfo=timezone.utc),
                    author="alice",
                ),
                IssueDetail(
                    number=17,
                    title="Docs out of date",
                    url="https://github.com/acme/api/issues/17",
                    created_at=datetime(2023, 6, 1, tzinfo=timezone.utc),
                    last_activity_at=datetime(2023, 8, 10, tzinfo=timezone.utc),
                    author="bob",
                ),
            ],
            stale_pr_items=[
                PullRequestDetail(
                    number=99,
                    title="Refactor auth module",
                    url="https://github.com/acme/api/pull/99",
                    created_at=datetime(2025, 1, 10, tzinfo=timezone.utc),
                    last_activity_at=datetime(2025, 2, 1, tzinfo=timezone.utc),
                    author="charlie",
                ),
            ],
            warnings=[],
            fetched_at=datetime.now(tz=timezone.utc),
            last_commit_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
            total_branches=8,
        ),
        RepositoryStats(
            full_name="acme/frontend",
            default_branch="main",
            open_issues=10,
            stale_issues=0,
            open_pull_requests=1,
            stale_pull_requests=0,
            workflows=[
                WorkflowStatus(
                    name="Build",
                    branch="main",
                    status="failure",
                    url="https://github.com/acme/frontend/actions",
                ),
                WorkflowStatus(
                    name="Build",
                    branch="develop",
                    status="success",
                    url="https://github.com/acme/frontend/actions",
                ),
            ],
            warnings=["Rate limit approaching"],
            fetched_at=datetime.now(tz=timezone.utc),
            last_commit_at=datetime(2026, 4, 12, tzinfo=timezone.utc),
            total_branches=4,
        ),
    ]
    update_cache(repos, stats)


@pytest.fixture
async def web_client() -> AsyncIterator[AsyncClient]:
    """Provide an async HTTP client with pre-populated cache data."""
    from grimoire.actions.router import _actions
    from grimoire.checks.router import _checks

    # Save and clear module-level state that may have been polluted by other tests
    saved_actions = _actions[:]
    saved_checks = _checks[:]
    _actions.clear()
    _checks.clear()

    _populate_cache()
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    # Restore previous state
    _actions.clear()
    _actions.extend(saved_actions)
    _checks.clear()
    _checks.extend(saved_checks)


@pytest.fixture
async def web_client_with_root_path() -> AsyncIterator[AsyncClient]:
    """Client for an app served under the ``/grimoire`` prefix (e.g. behind an ingress)."""
    _populate_cache()
    app = create_app()
    transport = ASGITransport(app=app, root_path="/grimoire")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def web_client_with_checks(tmp_path: object) -> AsyncIterator[AsyncClient]:
    """Provide an async HTTP client with check definitions and DB results."""
    import grimoire.checks.router as checks_router
    from grimoire.actions.router import _actions

    # Save state
    saved_actions = _actions[:]
    saved_checks = checks_router._checks[:]
    saved_engine = checks_router._engine

    _actions.clear()
    checks_router._checks.clear()

    _populate_cache()

    # Create a temp DB with check result tables
    engine = await get_engine(str(tmp_path) + "/test.db")  # type: ignore[arg-type]
    await create_tables(engine)

    # Set up check definitions
    watchdog = CheckDefinition(
        name="Watchdog",
        slug="watchdog",
        description="Always green sentinel",
        targets=TargetSpec(regex=".*"),
        script="exit 0",
        schedule="0 * * * *",
    )
    operator_check = CheckDefinition(
        name="Charm Libraries",
        slug="charm-libs",
        description="Check charm libs",
        targets=TargetSpec(regex="-operator$"),
        script="exit 0",
        schedule="30 2 * * *",
    )
    checks_router._checks.extend([watchdog, operator_check])
    checks_router._engine = engine

    # Insert check run and results
    async with AsyncSession(engine) as session:
        run = CheckRunRecord(
            check_slug="watchdog",
            check_name="Watchdog",
            triggered_by="manual",
            status="completed",
            started_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
            finished_at=datetime(2026, 4, 10, 0, 1, tzinfo=timezone.utc),
        )
        session.add(run)
        await session.flush()

        # Watchdog passed for acme/api
        session.add(
            CheckResultRecord(
                run_id=run.id,
                check_slug="watchdog",
                check_name="Watchdog",
                repo_full_name="acme/api",
                branch="main",
                passed=True,
                output="OK",
                timestamp=datetime(2026, 4, 10, tzinfo=timezone.utc),
            )
        )
        # Watchdog failed for acme/frontend main
        session.add(
            CheckResultRecord(
                run_id=run.id,
                check_slug="watchdog",
                check_name="Watchdog",
                repo_full_name="acme/frontend",
                branch="main",
                passed=False,
                output="Something went wrong",
                timestamp=datetime(2026, 4, 10, tzinfo=timezone.utc),
            )
        )
        await session.commit()

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    # Restore state
    _actions.clear()
    _actions.extend(saved_actions)
    checks_router._checks.clear()
    checks_router._checks.extend(saved_checks)
    checks_router._engine = saved_engine
    await engine.dispose()


@pytest.fixture
async def web_client_with_actions(tmp_path: object) -> AsyncIterator[AsyncClient]:
    """Provide an async HTTP client with action definitions and DB run history."""
    import grimoire.actions.router as actions_router
    from grimoire.actions.loader import ActionDefinition
    from grimoire.checks.router import _checks
    from grimoire.database import ActionRunRecord, ActionRunRepoRecord

    # Save state
    saved_actions = actions_router._actions[:]
    saved_engine = actions_router._engine
    saved_checks = _checks[:]

    actions_router._actions.clear()
    _checks.clear()

    _populate_cache()

    # Create a temp DB with action run tables
    engine = await get_engine(str(tmp_path) + "/test_actions.db")  # type: ignore[arg-type]
    await create_tables(engine)

    # Set up action definitions
    test_action = ActionDefinition(
        name="Test",
        slug="test",
        description="Runs pwd in each workspace",
        targets=TargetSpec(regex=".*"),
        script="pwd\n",
        schedule="0 3 * * *",
    )
    actions_router._actions.append(test_action)
    actions_router._engine = engine

    # Insert action run records
    async with AsyncSession(engine) as session:
        run = ActionRunRecord(
            action_slug="test",
            action_name="Test",
            triggered_by="manual",
            status="completed",
            started_at=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
            finished_at=datetime(2026, 4, 10, 12, 1, tzinfo=timezone.utc),
        )
        session.add(run)
        await session.flush()

        session.add(
            ActionRunRepoRecord(
                run_id=run.id,  # type: ignore[arg-type]
                repo_full_name="acme/api",
                branch="main",
                passed=True,
                output="/workspace/acme/api",
            )
        )
        session.add(
            ActionRunRepoRecord(
                run_id=run.id,  # type: ignore[arg-type]
                repo_full_name="acme/frontend",
                branch="main",
                passed=True,
                output="/workspace/acme/frontend",
            )
        )
        await session.commit()

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    # Restore state
    actions_router._actions.clear()
    actions_router._actions.extend(saved_actions)
    actions_router._engine = saved_engine
    _checks.clear()
    _checks.extend(saved_checks)
    await engine.dispose()
