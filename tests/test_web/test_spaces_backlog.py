"""Tests for filtering the backlog page, partial and export by the space selection."""

from __future__ import annotations

import re

import pytest
from httpx import AsyncClient

from grimoire.spaces import UNASSIGNED, SpaceSelection
from grimoire.web.router import _build_backlog_items
from grimoire.web.spaces import COOKIE_NAME

SHARED_RELEASE = "Workflow 'Release' failing on `main`"
SHARED_LINT = "Workflow 'Lint' failing on `main`"
SOLO_CI = "Workflow 'CI' failing on `main`"


@pytest.mark.parametrize(
    ("ids", "repos"),
    [
        (None, {"o/shared", "o/solo", "o/loose"}),
        (frozenset({"a"}), {"o/shared", "o/solo"}),
        (frozenset({"b"}), {"o/shared"}),
        (frozenset({"a", "b"}), {"o/shared", "o/solo"}),
        (frozenset({UNASSIGNED}), {"o/loose"}),
    ],
)
async def test_items_come_only_from_the_selected_repos(
    overlap_spaces: None, ids: frozenset[str] | None, repos: set[str]
) -> None:
    items = await _build_backlog_items(selection=SpaceSelection(ids))
    assert {i.repo_full_name for i in items} == repos


@pytest.mark.parametrize(
    ("ids", "shared_workflows"),
    [
        (None, {SHARED_RELEASE, SHARED_LINT}),
        (frozenset({"a"}), set()),  # a only keeps CI, which passes
        (frozenset({"b"}), {SHARED_RELEASE}),
        (frozenset({"a", "b"}), {SHARED_RELEASE}),
    ],
)
async def test_failing_workflow_items_respect_the_selected_workflow_filters(
    overlap_spaces: None, ids: frozenset[str] | None, shared_workflows: set[str]
) -> None:
    items = await _build_backlog_items(selection=SpaceSelection(ids))
    got = {
        i.description
        for i in items
        if i.repo_full_name == "o/shared" and i.category.value == "failing_workflow"
    }
    assert got == shared_workflows


async def test_scores_do_not_depend_on_the_selection(overlap_spaces: None) -> None:
    everything = {(i.repo_full_name, i.description): i.score for i in await _build_backlog_items()}
    narrow = await _build_backlog_items(selection=SpaceSelection(frozenset({"a"})))
    assert narrow
    for item in narrow:
        assert everything[(item.repo_full_name, item.description)] == pytest.approx(item.score)
    scores = [i.score for i in narrow]
    assert scores == sorted(scores, reverse=True)


class TestRoutes:
    async def test_page_and_partial_follow_the_cookie(self, overlap_client: AsyncClient) -> None:
        overlap_client.cookies.set(COOKIE_NAME, "b")
        for path in ("/backlog", "/partials/backlog-items"):
            html = (await overlap_client.get(path)).text
            assert "o/shared" in html, path
            assert "o/solo" not in html and "o/loose" not in html, path

    async def test_unassigned_and_all(self, overlap_client: AsyncClient) -> None:
        overlap_client.cookies.set(COOKIE_NAME, UNASSIGNED)
        html = (await overlap_client.get("/partials/backlog-items")).text
        assert "o/loose" in html and "o/shared" not in html
        overlap_client.cookies.delete(COOKIE_NAME)
        html = (await overlap_client.get("/partials/backlog-items")).text
        assert all(r in html for r in ("o/shared", "o/solo", "o/loose"))

    async def test_summary_counts_only_the_shown_items(self, overlap_client: AsyncClient) -> None:
        overlap_client.cookies.set(COOKIE_NAME, UNASSIGNED)
        html = (await overlap_client.get("/backlog")).text
        # o/loose has one stale issue and nothing else
        assert re.search(r"1\s+item", html) or "1 problem" in html or "across 1 repo" in html

    async def test_group_by_repo_view_is_filtered(self, overlap_client: AsyncClient) -> None:
        overlap_client.cookies.set(COOKIE_NAME, "a")
        html = (await overlap_client.get("/partials/backlog-items?group_by=repo")).text
        assert "o/solo" in html and "o/loose" not in html

    async def test_rows_carry_no_space_markers(self, overlap_client: AsyncClient) -> None:
        for query in ("", "?group_by=repo"):
            html = (await overlap_client.get(f"/partials/backlog-items{query}")).text
            assert "Space: " not in html, query


class TestExport:
    async def test_export_ignores_the_cookie(self, overlap_client: AsyncClient) -> None:
        overlap_client.cookies.set(COOKIE_NAME, "b")
        md = (await overlap_client.get("/api/backlog/export")).text
        assert all(r in md for r in ("o/shared", "o/solo", "o/loose"))

    @pytest.mark.parametrize(
        ("query", "present", "absent"),
        [
            ("a", {"o/shared", "o/solo"}, {"o/loose"}),
            ("b", {"o/shared"}, {"o/solo", "o/loose"}),
            ("a,b", {"o/shared", "o/solo"}, {"o/loose"}),
            ("_unassigned", {"o/loose"}, {"o/shared", "o/solo"}),
            ("", {"o/shared", "o/solo", "o/loose"}, set()),
        ],
    )
    async def test_export_honours_the_query(
        self,
        overlap_client: AsyncClient,
        query: str,
        present: set[str],
        absent: set[str],
    ) -> None:
        md = (await overlap_client.get("/api/backlog/export", params={"spaces": query})).text
        assert all(r in md for r in present)
        assert not any(r in md for r in absent)

    async def test_page_passes_its_selection_to_the_export_links(
        self, overlap_client: AsyncClient
    ) -> None:
        overlap_client.cookies.set(COOKIE_NAME, "a,b")
        html = (await overlap_client.get("/backlog")).text
        assert "/api/backlog/export?spaces=a%2Cb" in html
        assert "'/api/backlog/export' + \"?spaces=a%2Cb\"" in html

    async def test_export_links_are_plain_for_all(self, overlap_client: AsyncClient) -> None:
        html = (await overlap_client.get("/backlog")).text
        assert 'href="/api/backlog/export"' in html
        assert "'/api/backlog/export' + \"\"" in html
