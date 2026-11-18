"""Tests for filtering the dashboard and /api/repos by the space selection."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from grimoire.app import create_app
from grimoire.config import SpaceConfig, StaticRepoSource
from grimoire.github.router import get_tracked_repos, update_cache
from grimoire.models import RepoMembership, RepositoryStats, TrackedRepository, WorkflowStatus
from grimoire.spaces import UNASSIGNED, SpaceSelection, set_spaces_config
from grimoire.web.router import _build_repo_viewmodels
from grimoire.web.spaces import COOKIE_NAME


def _wf(name: str, status: str = "success") -> WorkflowStatus:
    return WorkflowStatus(name=name, branch="main", status=status, url="https://example.com")


def _stats(name: str, workflows: list[WorkflowStatus], issues: int) -> RepositoryStats:
    return RepositoryStats(
        full_name=name, default_branch="main", open_issues=issues, workflows=workflows
    )


@pytest.fixture
def spaces() -> Iterator[None]:
    """`shared` is top-level and in a and b with different workflow filters."""
    repos = [
        TrackedRepository(
            full_name="o/shared",
            branches=["main"],
            memberships=[
                RepoMembership(),
                RepoMembership(space="a", workflow_include=["CI"]),
                RepoMembership(space="b", workflow_include=["Release"]),
            ],
        ),
        TrackedRepository(
            full_name="o/solo", branches=["main"], memberships=[RepoMembership(space="a")]
        ),
        TrackedRepository(full_name="o/loose", branches=["main"], memberships=[RepoMembership()]),
    ]
    stats = [
        _stats("o/shared", [_wf("CI"), _wf("Release", "failure"), _wf("Lint")], issues=1),
        _stats("o/solo", [_wf("CI")], issues=10),
        _stats("o/loose", [_wf("CI")], issues=100),
    ]
    update_cache(repos, stats)
    source = [StaticRepoSource(repo="o/x")]
    set_spaces_config(
        [
            SpaceConfig(id="a", name="Alpha", color="#112233", repositories=source),
            SpaceConfig(id="b", name="Beta", repositories=source),
        ],
        get_tracked_repos,
    )
    yield
    set_spaces_config([])
    update_cache([], [])


@pytest.fixture
async def client(spaces: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _repo_names(selection: SpaceSelection) -> list[str]:
    return sorted(r.full_name for r in await _build_repo_viewmodels(selection=selection))


@pytest.mark.parametrize(
    ("ids", "expected"),
    [
        (None, ["o/loose", "o/shared", "o/solo"]),
        (frozenset({"a"}), ["o/shared", "o/solo"]),
        (frozenset({"b"}), ["o/shared"]),
        (frozenset({"a", "b"}), ["o/shared", "o/solo"]),
        (frozenset({UNASSIGNED}), ["o/loose"]),
        (frozenset({UNASSIGNED, "b"}), ["o/loose", "o/shared"]),
    ],
)
async def test_repos_follow_the_selection(
    spaces: None, ids: frozenset[str] | None, expected: list[str]
) -> None:
    assert await _repo_names(SpaceSelection(ids)) == expected


@pytest.mark.parametrize(
    ("ids", "visible", "failures"),
    [
        (None, {"CI", "Release", "Lint"}, 1),
        (frozenset({"a"}), {"CI"}, 0),  # only a's filter applies to the overlapping repo
        (frozenset({"b"}), {"Release"}, 1),
        (frozenset({"a", "b"}), {"CI", "Release"}, 1),  # union of both filters
    ],
)
async def test_overlapping_repo_shows_the_workflows_of_the_selected_spaces(
    spaces: None, ids: frozenset[str] | None, visible: set[str], failures: int
) -> None:
    repos = await _build_repo_viewmodels(selection=SpaceSelection(ids))
    shared = next(r for r in repos if r.full_name == "o/shared")
    names = {wf.name for wfs in shared.workflows_by_branch.values() for wf in wfs}
    assert names == visible
    assert {wf.name for c in shared.workflow_columns for wf in c.workflows} == visible
    assert shared.workflow_failures == failures
    assert shared.spaces == ["a", "b"]


async def _stat_values(client: AsyncClient) -> tuple[int, int]:
    html = (await client.get("/partials/dashboard-stats")).text
    repos = re.search(r"Repositories</div>\s*<div[^>]*>(\d+)<", html)
    issues = re.search(r"Open Issues</div>\s*<div[^>]*>(\d+)<", html)
    assert repos and issues
    return int(repos.group(1)), int(issues.group(1))


class TestHtml:
    async def test_no_cookie_shows_everything(self, client: AsyncClient) -> None:
        html = (await client.get("/")).text
        assert all(n in html for n in ("o/shared", "o/solo", "o/loose"))
        assert await _stat_values(client) == (3, 111)

    async def test_cookie_filters_page_partials_and_stats(self, client: AsyncClient) -> None:
        client.cookies.set(COOKIE_NAME, "a")
        for path in ("/", "/partials/dashboard-matrix", "/partials/dashboard-list"):
            html = (await client.get(path)).text
            assert "o/shared" in html and "o/solo" in html, path
            assert "o/loose" not in html, path
        assert await _stat_values(client) == (2, 11)  # header counts only the shown repos

    async def test_unassigned(self, client: AsyncClient) -> None:
        client.cookies.set(COOKIE_NAME, UNASSIGNED)
        html = (await client.get("/partials/dashboard-matrix")).text
        assert "o/loose" in html and "o/shared" not in html
        assert await _stat_values(client) == (1, 100)

    async def test_query_applies_and_is_saved(self, client: AsyncClient) -> None:
        resp = await client.get("/", params={"spaces": "b"})
        assert "o/shared" in resp.text and "o/solo" not in resp.text
        assert resp.headers["set-cookie"].startswith(f"{COOKIE_NAME}=b;")

    async def test_rows_show_space_badges(self, client: AsyncClient) -> None:
        for path in ("/partials/dashboard-matrix", "/partials/dashboard-list"):
            html = (await client.get(path)).text
            assert 'title="Space: Alpha"' in html, path
            assert 'title="Space: Beta"' in html, path


class TestApi:
    async def test_api_ignores_the_cookie(self, client: AsyncClient) -> None:
        client.cookies.set(COOKIE_NAME, "a")
        body = (await client.get("/api/repos/")).json()
        assert len(body["repositories"]) == 3

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("a", ["o/shared", "o/solo"]),
            ("a,b", ["o/shared", "o/solo"]),
            ("_unassigned", ["o/loose"]),
            ("nope", ["o/loose", "o/shared", "o/solo"]),
        ],
    )
    async def test_api_honours_the_query(
        self, client: AsyncClient, query: str, expected: list[str]
    ) -> None:
        body = (await client.get("/api/repos/", params={"spaces": query})).json()
        assert sorted(r["full_name"] for r in body["repositories"]) == expected

    async def test_api_failures_follow_the_selected_spaces_filters(
        self, client: AsyncClient
    ) -> None:
        def failures(body: dict) -> int:
            return next(r for r in body["repositories"] if r["full_name"] == "o/shared")[
                "workflow_failures"
            ]

        assert failures((await client.get("/api/repos/")).json()) == 1
        assert failures((await client.get("/api/repos/", params={"spaces": "a"})).json()) == 0
        assert failures((await client.get("/api/repos/", params={"spaces": "b"})).json()) == 1
