"""Tests for filtering the Checks and Actions pages and APIs by the space selection."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

import grimoire.actions.router as actions_router
import grimoire.checks.router as checks_router
from grimoire.actions.engine import GLOBAL_REPO
from grimoire.actions.loader import ActionDefinition
from grimoire.app import create_app
from grimoire.checks.loader import CheckDefinition
from grimoire.database import (
    ActionRunRecord,
    ActionRunRepoRecord,
    CheckResultRecord,
    CheckRunRecord,
    get_engine,
    upgrade_database,
)
from grimoire.spaces import UNASSIGNED
from grimoire.targeting import TargetSpec
from grimoire.web.spaces import COOKIE_NAME

WHEN = datetime(2026, 4, 10, tzinfo=timezone.utc)
# (repo, passed) per run; o/loose and o/solo fail, o/shared passes
RESULTS = [("o/shared", True), ("o/solo", False), ("o/loose", False)]


def _check(slug: str, spaces: list[str]) -> CheckDefinition:
    return CheckDefinition(
        name=slug,
        slug=slug,
        description="d",
        targets=TargetSpec(regex=".*"),
        script="exit 0",
        spaces=spaces,
    )


def _action(slug: str, spaces: list[str], targets: TargetSpec | None) -> ActionDefinition:
    return ActionDefinition(
        name=slug, slug=slug, description="d", targets=targets, script="true", spaces=spaces
    )


@pytest.fixture
async def spaced(overlap_spaces: None, tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """Checks/actions with and without spaces, and one run of each with results per repo."""
    engine = await get_engine(str(tmp_path / "t.db"))
    await upgrade_database(engine)

    saved = (
        checks_router._checks[:],
        checks_router._engine,
        actions_router._actions[:],
        actions_router._engine,
    )
    checks = [_check("global-check", []), _check("a-check", ["a"]), _check("b-check", ["b"])]
    actions = [
        _action("global-action", [], None),  # a global action: results under "(global)"
        _action("a-action", ["a"], TargetSpec(regex=".*")),
        _action("b-action", ["b"], TargetSpec(regex=".*")),
    ]
    checks_router._checks[:] = checks
    checks_router._engine = engine
    actions_router._actions[:] = actions
    actions_router._engine = engine

    async with AsyncSession(engine) as session:
        for check in checks:
            run = CheckRunRecord(
                check_slug=check.slug,
                check_name=check.name,
                triggered_by="cron",
                status="completed",
                started_at=WHEN,
            )
            session.add(run)
            await session.flush()
            for repo, passed in RESULTS:
                session.add(
                    CheckResultRecord(
                        run_id=run.id,
                        check_slug=check.slug,
                        check_name=check.name,
                        repo_full_name=repo,
                        branch="main",
                        passed=passed,
                        timestamp=WHEN,
                    )
                )
        for action in actions:
            run = ActionRunRecord(
                action_slug=action.slug,
                action_name=action.name,
                triggered_by="manual",
                status="completed",
                started_at=WHEN,
            )
            session.add(run)
            await session.flush()
            rows = [(GLOBAL_REPO, True)] if action.targets is None else RESULTS
            for repo, passed in rows:
                session.add(
                    ActionRunRepoRecord(
                        run_id=run.id, repo_full_name=repo, branch="main", passed=passed
                    )
                )
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        yield client

    (
        checks_router._checks[:],
        checks_router._engine,
        actions_router._actions[:],
        actions_router._engine,
    ) = saved
    await engine.dispose()


def _cards(html: str, prefix: str) -> set[str]:
    return set(re.findall(rf'id="{prefix}-card-([\w-]+)"', html))


def _counts(html: str, prefix: str, slug: str) -> tuple[int, int]:
    """(passed, failed) shown on a card."""
    card = html.split(f'id="{prefix}-card-{slug}"')[1].split(f'id="{prefix}-card-')[0]
    passed = re.search(r'fa-check"></i> (\d+)', card)
    failed = re.search(r'fa-xmark"></i> (\d+)', card)
    return (int(passed.group(1)) if passed else 0, int(failed.group(1)) if failed else 0)


SELECTIONS = [
    ("", {"global", "a", "b"}),
    ("a", {"global", "a"}),
    ("b", {"global", "b"}),
    ("a,b", {"global", "a", "b"}),
    (UNASSIGNED, {"global"}),  # Unassigned lists only the global definitions
]


class TestListing:
    @pytest.mark.parametrize(("cookie", "listed"), SELECTIONS)
    async def test_pages_list_global_and_selected_definitions(
        self, spaced: AsyncClient, cookie: str, listed: set[str]
    ) -> None:
        spaced.cookies.set(COOKIE_NAME, cookie)
        checks = _cards((await spaced.get("/checks")).text, "check")
        actions = _cards((await spaced.get("/actions")).text, "action")
        assert checks == {f"{n}-check" for n in listed}
        assert actions == {f"{n}-action" for n in listed}

    @pytest.mark.parametrize(("query", "listed"), SELECTIONS)
    async def test_apis_honour_the_query(
        self, spaced: AsyncClient, query: str, listed: set[str]
    ) -> None:
        params = {"spaces": query} if query else {}
        checks = (await spaced.get("/api/checks/", params=params)).json()
        actions = (await spaced.get("/api/actions/", params=params)).json()
        assert {c["slug"] for c in checks} == {f"{n}-check" for n in listed}
        assert {a["slug"] for a in actions} == {f"{n}-action" for n in listed}

    async def test_apis_ignore_the_cookie_and_expose_spaces(self, spaced: AsyncClient) -> None:
        spaced.cookies.set(COOKIE_NAME, "a")
        checks = {c["slug"]: c for c in (await spaced.get("/api/checks/")).json()}
        actions = {a["slug"]: a for a in (await spaced.get("/api/actions/")).json()}
        assert set(checks) == {"global-check", "a-check", "b-check"}
        assert checks["a-check"]["spaces"] == ["a"]
        assert checks["global-check"]["spaces"] == []
        assert actions["b-action"]["spaces"] == ["b"]


class TestResults:
    async def test_card_summaries_count_only_shown_repos(self, spaced: AsyncClient) -> None:
        html = (await spaced.get("/checks")).text
        assert _counts(html, "check", "global-check") == (1, 2)
        spaced.cookies.set(COOKIE_NAME, "b")  # only o/shared (passing)
        html = (await spaced.get("/checks")).text
        assert _counts(html, "check", "global-check") == (1, 0)
        spaced.cookies.set(COOKIE_NAME, UNASSIGNED)  # only o/loose (failing)
        html = (await spaced.get("/checks")).text
        assert _counts(html, "check", "global-check") == (0, 1)

    async def test_action_summaries_count_only_shown_repos(self, spaced: AsyncClient) -> None:
        spaced.cookies.set(COOKIE_NAME, "a")  # o/shared + o/solo
        html = (await spaced.get("/actions")).text
        assert _counts(html, "action", "a-action") == (1, 1)

    async def test_global_action_results_are_never_filtered_out(self, spaced: AsyncClient) -> None:
        spaced.cookies.set(COOKIE_NAME, "b")
        html = (await spaced.get("/actions")).text
        assert _counts(html, "action", "global-action") == (1, 0)
        partial = (await spaced.get("/partials/action-results/global-action")).text
        assert '<tr class="hover">' in partial  # the (global) row

    @pytest.mark.parametrize(
        ("cookie", "shown", "hidden"),
        [
            ("a", {"o/shared", "o/solo"}, {"o/loose"}),
            ("b", {"o/shared"}, {"o/solo", "o/loose"}),
            (UNASSIGNED, {"o/loose"}, {"o/shared", "o/solo"}),
            ("", {"o/shared", "o/solo", "o/loose"}, set()),
        ],
    )
    async def test_result_partials_list_only_shown_repos(
        self, spaced: AsyncClient, cookie: str, shown: set[str], hidden: set[str]
    ) -> None:
        spaced.cookies.set(COOKIE_NAME, cookie)
        for path in (
            "/partials/check-results/global-check",
            "/partials/action-results/a-action",
        ):
            html = (await spaced.get(path)).text
            assert all(r in html for r in shown), path
            assert not any(r in html for r in hidden), path

    async def test_run_partial_and_summary_counts_follow_the_selection(
        self, spaced: AsyncClient
    ) -> None:
        spaced.cookies.set(COOKIE_NAME, UNASSIGNED)
        html = (await spaced.get("/partials/check-results/global-check")).text
        assert "o/loose" in html and "o/shared" not in html
        detail = (await spaced.get("/partials/action-run/2")).text  # run 2 is a-action's
        assert "o/loose" in detail and "o/solo" not in detail and "o/shared" not in detail

    async def test_runs_without_any_shown_repo_are_hidden(self, spaced: AsyncClient) -> None:
        # the global-check run has results for every repo; narrow to a space with none of them
        from grimoire.config import SpaceConfig, StaticRepoSource
        from grimoire.github.router import get_tracked_repos
        from grimoire.models import RepoMembership
        from grimoire.spaces import set_spaces_config

        for repo in get_tracked_repos():
            repo.memberships = [RepoMembership(space="a")]
        source = [StaticRepoSource(repo="o/x")]
        set_spaces_config(
            [
                SpaceConfig(id="a", name="A", repositories=source),
                SpaceConfig(id="empty", name="E", repositories=source),
            ],
            get_tracked_repos,
        )
        spaced.cookies.set(COOKIE_NAME, "empty")
        html = (await spaced.get("/partials/check-results/global-check")).text
        assert "o/shared" not in html and "o/solo" not in html

    async def test_api_results_honour_only_the_query(self, spaced: AsyncClient) -> None:
        spaced.cookies.set(COOKIE_NAME, "b")
        everything = (await spaced.get("/api/checks/global-check/results")).json()
        assert {r["repo_full_name"] for r in everything} == {"o/shared", "o/solo", "o/loose"}
        narrow = (
            await spaced.get("/api/checks/global-check/results", params={"spaces": "a"})
        ).json()
        assert {r["repo_full_name"] for r in narrow} == {"o/shared", "o/solo"}


class TestTargetingUnchanged:
    async def test_manual_run_under_a_narrow_selection_still_uses_all_targets(
        self, spaced: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        async def fake_run(check, repos, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            seen.extend(r.full_name for r in repos)

        monkeypatch.setattr(checks_router, "run_check_for_all_targets", fake_run)
        from grimoire.github.router import get_tracked_repos

        checks_router._repos[:] = get_tracked_repos()
        checks_router._workspace = object()  # type: ignore[assignment]

        spaced.cookies.set(COOKIE_NAME, UNASSIGNED)
        resp = await spaced.post("/api/checks/global-check/run", params={"spaces": UNASSIGNED})
        assert resp.status_code == 200
        assert sorted(seen) == ["o/loose", "o/shared", "o/solo"]
