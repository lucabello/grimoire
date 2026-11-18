"""Tests for filtering the dashboard and /api/repos by the space selection."""

from __future__ import annotations

import re

import pytest
from httpx import AsyncClient

from grimoire.spaces import UNASSIGNED, SpaceSelection
from grimoire.web.router import _build_repo_viewmodels
from grimoire.web.spaces import COOKIE_NAME


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
    overlap_spaces: None, ids: frozenset[str] | None, expected: list[str]
) -> None:
    assert await _repo_names(SpaceSelection(ids)) == expected


@pytest.mark.parametrize(
    ("ids", "visible", "failures"),
    [
        (None, {"CI", "Release", "Lint"}, 2),
        (frozenset({"a"}), {"CI"}, 0),  # only a's filter applies to the overlapping repo
        (frozenset({"b"}), {"Release"}, 1),
        (frozenset({"a", "b"}), {"CI", "Release"}, 1),  # union of both filters
    ],
)
async def test_overlapping_repo_shows_the_workflows_of_the_selected_spaces(
    overlap_spaces: None, ids: frozenset[str] | None, visible: set[str], failures: int
) -> None:
    repos = await _build_repo_viewmodels(selection=SpaceSelection(ids))
    shared = next(r for r in repos if r.full_name == "o/shared")
    names = {wf.name for wfs in shared.workflows_by_branch.values() for wf in wfs}
    assert names == visible
    assert {wf.name for c in shared.workflow_columns for wf in c.workflows} == visible
    assert shared.workflow_failures == failures
    assert shared.spaces == ["a", "b"]


async def _stat_values(overlap_client: AsyncClient) -> tuple[int, int]:
    html = (await overlap_client.get("/partials/dashboard-stats")).text
    repos = re.search(r"Repositories</div>\s*<div[^>]*>(\d+)<", html)
    issues = re.search(r"Open Issues</div>\s*<div[^>]*>(\d+)<", html)
    assert repos and issues
    return int(repos.group(1)), int(issues.group(1))


class TestHtml:
    async def test_no_cookie_shows_everything(self, overlap_client: AsyncClient) -> None:
        html = (await overlap_client.get("/")).text
        assert all(n in html for n in ("o/shared", "o/solo", "o/loose"))
        assert await _stat_values(overlap_client) == (3, 111)

    async def test_cookie_filters_page_partials_and_stats(
        self, overlap_client: AsyncClient
    ) -> None:
        overlap_client.cookies.set(COOKIE_NAME, "a")
        for path in ("/", "/partials/dashboard-matrix", "/partials/dashboard-list"):
            html = (await overlap_client.get(path)).text
            assert "o/shared" in html and "o/solo" in html, path
            assert "o/loose" not in html, path
        assert await _stat_values(overlap_client) == (2, 11)  # header counts only the shown repos

    async def test_unassigned(self, overlap_client: AsyncClient) -> None:
        overlap_client.cookies.set(COOKIE_NAME, UNASSIGNED)
        html = (await overlap_client.get("/partials/dashboard-matrix")).text
        assert "o/loose" in html and "o/shared" not in html
        assert await _stat_values(overlap_client) == (1, 100)

    async def test_query_applies_and_is_saved(self, overlap_client: AsyncClient) -> None:
        resp = await overlap_client.get("/", params={"spaces": "b"})
        assert "o/shared" in resp.text and "o/solo" not in resp.text
        assert resp.headers["set-cookie"].startswith(f"{COOKIE_NAME}=b;")

    async def test_rows_show_space_badges(self, overlap_client: AsyncClient) -> None:
        for path in ("/partials/dashboard-matrix", "/partials/dashboard-list"):
            html = (await overlap_client.get(path)).text
            assert 'title="Space: Alpha"' in html, path
            assert 'title="Space: Beta"' in html, path


class TestApi:
    async def test_api_ignores_the_cookie(self, overlap_client: AsyncClient) -> None:
        overlap_client.cookies.set(COOKIE_NAME, "a")
        body = (await overlap_client.get("/api/repos/")).json()
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
        self, overlap_client: AsyncClient, query: str, expected: list[str]
    ) -> None:
        body = (await overlap_client.get("/api/repos/", params={"spaces": query})).json()
        assert sorted(r["full_name"] for r in body["repositories"]) == expected

    async def test_api_failures_follow_the_selected_spaces_filters(
        self, overlap_client: AsyncClient
    ) -> None:
        def failures(body: dict) -> int:
            return next(r for r in body["repositories"] if r["full_name"] == "o/shared")[
                "workflow_failures"
            ]

        assert failures((await overlap_client.get("/api/repos/")).json()) == 2
        assert (
            failures((await overlap_client.get("/api/repos/", params={"spaces": "a"})).json()) == 0
        )
        assert (
            failures((await overlap_client.get("/api/repos/", params={"spaces": "b"})).json()) == 1
        )
