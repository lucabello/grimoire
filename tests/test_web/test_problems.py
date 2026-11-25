"""Tests for GET /api/problems."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

import grimoire.checks.router as checks_router
from grimoire.actions.router import _actions
from grimoire.app import create_app
from grimoire.checks.loader import CheckDefinition
from grimoire.config import PRGroup, PRsConfig, SpaceConfig, StaticRepoSource
from grimoire.database import CheckResultRecord, get_engine, upgrade_database
from grimoire.github.router import get_tracked_repos, update_cache
from grimoire.models import (
    OpenPullRequest,
    RepoMembership,
    RepositoryStats,
    TrackedRepository,
    WorkflowStatus,
)
from grimoire.prs import get_prs_config, set_prs_config
from grimoire.spaces import set_spaces_config
from grimoire.targeting import TargetSpec
from grimoire.web.problems import OUTPUT_LIMIT, ProblemKind
from grimoire.web.router import _build_backlog_items
from grimoire.web.spaces import COOKIE_NAME

T0 = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
FETCHED = datetime(2026, 11, 2, 8, tzinfo=timezone.utc)


def _check(slug: str, severity: str = "error", enabled: bool = True) -> CheckDefinition:
    return CheckDefinition(
        name=slug.title(),
        slug=slug,
        description=slug,
        targets=TargetSpec(regex=".*"),
        script="exit 1",
        enabled=enabled,
        severity=severity,  # type: ignore[arg-type]
    )


def _result(
    slug: str, repo: str, branch: str, passed: bool, output: str = "", at: datetime = T0
) -> CheckResultRecord:
    return CheckResultRecord(
        check_slug=slug,
        check_name=slug.title(),
        repo_full_name=repo,
        branch=branch,
        passed=passed,
        output=output,
        timestamp=at,
    )


def _pr(number: int, ci_state: str | None, **kwargs: Any) -> OpenPullRequest:
    return OpenPullRequest(
        number=number,
        title=f"PR {number}",
        url=f"https://github.com/o/alpha/pull/{number}",
        author="renovate[bot]",
        created_at=T0,
        base_ref="main",
        head_ref=f"renovate/{number}",
        head_sha=f"sha{number}",
        ci_state=ci_state,
        ci_fetched_at=FETCHED if ci_state else None,
        **kwargs,
    )


@pytest.fixture
async def problems_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """Two repos (``o/alpha`` in space ``a``, ``o/beta`` unassigned) with one of each problem."""
    saved = (_actions[:], checks_router._checks[:], checks_router._engine, get_prs_config())
    _actions.clear()
    checks_router._checks.clear()

    alpha = TrackedRepository(
        full_name="o/alpha", branches=["main"], memberships=[RepoMembership(space="a")]
    )
    beta = TrackedRepository(full_name="o/beta", branches=["main"], memberships=[RepoMembership()])
    alpha_stats = RepositoryStats(
        full_name="o/alpha",
        default_branch="main",
        fetched_at=FETCHED,
        warnings=["Could not fetch CI status for o/alpha"],
        workflows=[
            WorkflowStatus(
                name="CI",
                branch="main",
                status="failure",
                url="https://github.com/o/alpha/actions/workflows/ci.yml",
                run_url="https://github.com/o/alpha/actions/runs/7",
                run_id=7,
                run_attempt=2,
                event="push",
                head_sha="abc123",
                conclusion="failure",
                created_at=T0 - timedelta(minutes=5),
                run_started_at=T0,
            ),
            WorkflowStatus(
                name="Nightly",
                branch="main",
                status="failure",
                url="https://github.com/o/alpha/actions/workflows/nightly.yml",
                conclusion="cancelled",
                created_at=T0,
            ),
            WorkflowStatus(
                name="Slow",
                branch="main",
                status="failure",
                url="https://github.com/o/alpha/actions/workflows/slow.yml",
                conclusion="timed_out",
            ),
            WorkflowStatus(name="Lint", branch="main", status="success", url="https://x/lint"),
            WorkflowStatus(name="Docs", branch="main", status="pending", url="https://x/docs"),
        ],
        open_pr_items=[
            _pr(1, "failure", labels=["dependencies"], review_decision="review_required"),
            _pr(2, "success"),
            _pr(3, None),
        ],
    )
    beta_stats = RepositoryStats(
        full_name="o/beta",
        default_branch="main",
        workflows=[
            WorkflowStatus(name="Build", branch="main", status="failure", url="https://x/build")
        ],
    )
    update_cache([alpha, beta], [alpha_stats, beta_stats])
    set_spaces_config(
        [SpaceConfig(id="a", name="Alpha", repositories=[StaticRepoSource(repo="o/alpha")])],
        get_tracked_repos,
    )
    # Two groups match PR 1: it must still be listed once.
    set_prs_config(
        PRsConfig(
            groups=[
                PRGroup(name="bots", authors=["app/renovate"]),
                PRGroup(name="deps", labels=["dependencies"]),
            ]
        )
    )

    engine = await get_engine(str(tmp_path / "test.db"))
    await upgrade_database(engine)
    checks_router._engine = engine
    checks_router._checks.extend(
        [
            _check("lint"),
            _check("exact"),
            _check("soft", severity="warning"),
            _check("off", enabled=False),
            _check("recovered"),
        ]
    )
    async with AsyncSession(engine) as session:
        session.add_all(
            [
                _result("lint", "o/alpha", "main", False, "head-" + "x" * 5000 + "-tail"),
                _result("lint", "o/alpha", "dev", False, "untracked branch"),
                _result("lint", "o/ghost", "main", False, "untracked repo"),
                _result("exact", "o/alpha", "main", False, "y" * OUTPUT_LIMIT),
                _result("soft", "o/alpha", "main", False, "z" * (OUTPUT_LIMIT + 1)),
                _result("off", "o/alpha", "main", False, "disabled"),
                _result("recovered", "o/beta", "main", False, "old failure", T0),
                _result("recovered", "o/beta", "main", True, "ok", T0 + timedelta(hours=1)),
            ]
        )
        await session.commit()

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    _actions[:] = saved[0]
    checks_router._checks[:] = saved[1]
    checks_router._engine = saved[2]
    set_prs_config(saved[3])
    set_spaces_config([])
    update_cache([], [])
    await engine.dispose()


def _by_kind(body: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [p for p in body["problems"] if p["kind"] == kind]


async def test_every_kind_is_reported_with_its_fields(problems_client: AsyncClient) -> None:
    resp = await problems_client.get("/api/problems", follow_redirects=False)

    assert resp.status_code == 200
    body = resp.json()
    assert body["last_refresh"] is not None
    assert body["counts"] == {
        "workflow_failure": 4,
        "pr_ci_failure": 1,
        "check_failure": 3,
        "repo_warning": 1,
    }

    ci = next(p for p in _by_kind(body, "workflow_failure") if p["subject"] == "CI")
    assert ci == {
        "kind": "workflow_failure",
        "severity": "error",
        "repo": "o/alpha",
        "branch": "main",
        "subject": "CI",
        "summary": "Workflow 'CI' failing on main",
        "url": "https://github.com/o/alpha/actions/runs/7",
        "observed_at": "2026-11-01T12:00:00Z",
        "detail": {
            "kind": "workflow_failure",
            "run_id": 7,
            "run_attempt": 2,
            "event": "push",
            "conclusion": "failure",
            "head_sha": "abc123",
            "run_url": "https://github.com/o/alpha/actions/runs/7",
        },
    }

    [pr] = _by_kind(body, "pr_ci_failure")
    assert pr == {
        "kind": "pr_ci_failure",
        "severity": "error",
        "repo": "o/alpha",
        "branch": "renovate/1",
        "subject": "#1 PR 1",
        "summary": "CI failing on PR #1: PR 1",
        "url": "https://github.com/o/alpha/pull/1",
        "observed_at": "2026-11-02T08:00:00Z",
        "detail": {
            "kind": "pr_ci_failure",
            "number": 1,
            "author": "renovate[bot]",
            "draft": False,
            "labels": ["dependencies"],
            "review_decision": "review_required",
            "merge_state": None,
            "auto_merge": False,
            "head_sha": "sha1",
            "ci_url": "https://github.com/o/alpha/pull/1/checks",
            "ci_checked_at": "2026-11-02T08:00:00Z",
        },
    }

    soft = next(p for p in _by_kind(body, "check_failure") if p["detail"]["check_slug"] == "soft")
    assert soft["severity"] == "warning"
    assert soft["subject"] == "Soft"
    assert soft["branch"] == "main"
    assert soft["url"] is None
    assert soft["observed_at"] == "2026-11-01T12:00:00Z"
    assert soft["detail"]["results_path"] == "/api/checks/soft/results"
    assert soft["detail"]["timestamp"] == "2026-11-01T12:00:00Z"

    [warning] = _by_kind(body, "repo_warning")
    assert warning["severity"] == "warning"
    assert warning["repo"] == "o/alpha"
    assert warning["branch"] is None
    assert warning["observed_at"] == "2026-11-02T08:00:00Z"
    assert warning["detail"] == {
        "kind": "repo_warning",
        "warning": "Could not fetch CI status for o/alpha",
    }


async def test_cancelled_and_timed_out_runs_carry_their_conclusion(
    problems_client: AsyncClient,
) -> None:
    body = (await problems_client.get("/api/problems")).json()

    conclusions = {
        p["subject"]: p["detail"]["conclusion"]
        for p in _by_kind(body, "workflow_failure")
        if p["repo"] == "o/alpha"
    }
    assert conclusions == {"CI": "failure", "Nightly": "cancelled", "Slow": "timed_out"}
    nightly = next(p for p in body["problems"] if p["subject"] == "Nightly")
    assert nightly["observed_at"] == "2026-11-01T12:00:00Z"  # created_at without a start time
    slow = next(p for p in body["problems"] if p["subject"] == "Slow")
    assert slow["observed_at"] is None


async def test_passing_disabled_untracked_and_ungrouped_items_are_absent(
    problems_client: AsyncClient,
) -> None:
    body = (await problems_client.get("/api/problems")).json()

    checks = {
        (p["repo"], p["branch"], p["detail"]["check_slug"])
        for p in _by_kind(body, "check_failure")
    }
    assert checks == {
        ("o/alpha", "main", "lint"),
        ("o/alpha", "main", "exact"),
        ("o/alpha", "main", "soft"),
    }
    # PR 2 passes, PR 3 has no CI state, PR 1 matches two groups and is listed once
    assert [p["detail"]["number"] for p in _by_kind(body, "pr_ci_failure")] == [1]
    assert {p["subject"] for p in _by_kind(body, "workflow_failure")} == {
        "CI",
        "Nightly",
        "Slow",
        "Build",
    }


async def test_check_output_keeps_the_last_characters(problems_client: AsyncClient) -> None:
    body = (await problems_client.get("/api/problems")).json()
    by_slug = {p["detail"]["check_slug"]: p["detail"] for p in _by_kind(body, "check_failure")}

    assert len(by_slug["lint"]["output"]) == OUTPUT_LIMIT
    assert by_slug["lint"]["output"].endswith("-tail")
    assert "head-" not in by_slug["lint"]["output"]
    assert by_slug["lint"]["output_truncated"] is True
    assert by_slug["exact"]["output"] == "y" * OUTPUT_LIMIT
    assert by_slug["exact"]["output_truncated"] is False
    assert len(by_slug["soft"]["output"]) == OUTPUT_LIMIT
    assert by_slug["soft"]["output_truncated"] is True


async def test_results_path_follows_the_root_path(problems_client: AsyncClient) -> None:
    transport = ASGITransport(app=create_app(), root_path="/grimoire")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/api/problems?kind=check_failure")).json()

    paths = {p["detail"]["results_path"] for p in body["problems"]}
    assert "/grimoire/api/checks/lint/results" in paths


class TestKindFilter:
    async def test_comma_separated_kinds_filter_the_list_but_not_the_counts(
        self, problems_client: AsyncClient
    ) -> None:
        body = (await problems_client.get("/api/problems?kind=pr_ci_failure,repo_warning")).json()

        assert {p["kind"] for p in body["problems"]} == {"pr_ci_failure", "repo_warning"}
        assert len(body["problems"]) == 2
        assert body["counts"]["workflow_failure"] == 4
        assert body["counts"]["check_failure"] == 3

    @pytest.mark.parametrize("query", ["kind=nope", "kind=repo_warning,nope"])
    async def test_an_unknown_kind_is_a_422(
        self, problems_client: AsyncClient, query: str
    ) -> None:
        resp = await problems_client.get(f"/api/problems?{query}")

        assert resp.status_code == 422
        assert "nope" in resp.json()["detail"]


class TestSpaces:
    async def test_spaces_filter_items_and_counts(self, problems_client: AsyncClient) -> None:
        alpha = (await problems_client.get("/api/problems?spaces=a")).json()
        beta = (await problems_client.get("/api/problems?spaces=_unassigned")).json()

        assert {p["repo"] for p in alpha["problems"]} == {"o/alpha"}
        assert alpha["counts"]["workflow_failure"] == 3
        assert {p["repo"] for p in beta["problems"]} == {"o/beta"}
        assert beta["counts"] == {
            "workflow_failure": 1,
            "pr_ci_failure": 0,
            "check_failure": 0,
            "repo_warning": 0,
        }

    async def test_the_cookie_is_ignored(self, problems_client: AsyncClient) -> None:
        problems_client.cookies.set(COOKIE_NAME, "a")

        resp = await problems_client.get("/api/problems")

        assert {p["repo"] for p in resp.json()["problems"]} == {"o/alpha", "o/beta"}


async def test_no_data_yet_has_a_null_last_refresh(problems_client: AsyncClient) -> None:
    from grimoire.github import router as github_router

    update_cache([], [])
    github_router._last_refresh = None

    body = (await problems_client.get("/api/problems")).json()

    assert body["last_refresh"] is None
    assert body["problems"] == []
    assert body["counts"] == {kind.value: 0 for kind in ProblemKind}


async def test_works_without_checks_or_a_database(web_client: AsyncClient) -> None:
    resp = await web_client.get("/api/problems", follow_redirects=False)

    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"]["workflow_failure"] == 1
    assert body["counts"]["check_failure"] == 0


async def test_agrees_with_the_backlog(problems_client: AsyncClient) -> None:
    body = (await problems_client.get("/api/problems")).json()
    items = await _build_backlog_items()

    def backlog(prefix: str) -> set[tuple[str, str, str]]:
        found = set()
        for item in items:
            match = re.match(rf"{prefix} '(.+)' failing on `(.+)`", item.description)
            if match:
                found.add((item.repo_full_name, match[2], match[1]))
        return found

    def problems(kind: str) -> set[tuple[str, str, str]]:
        return {(p["repo"], p["branch"], p["subject"]) for p in _by_kind(body, kind)}

    assert problems("workflow_failure") == backlog("Workflow") != set()
    assert problems("check_failure") == backlog("Check") != set()


async def test_the_openapi_schema_shows_detail_as_a_discriminated_union(
    problems_client: AsyncClient,
) -> None:
    schema = (await problems_client.get("/openapi.json")).json()

    detail = schema["components"]["schemas"]["Problem"]["properties"]["detail"]
    assert detail["discriminator"]["propertyName"] == "kind"
    assert set(detail["discriminator"]["mapping"]) == {kind.value for kind in ProblemKind}
    assert "/api/problems" in schema["paths"]
