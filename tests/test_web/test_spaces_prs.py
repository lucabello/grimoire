"""Tests for filtering the PRs page, partial and API by the space selection."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from grimoire.config import PRGroup, PRsConfig
from grimoire.github.router import _cache
from grimoire.models import OpenPullRequest
from grimoire.prs import get_prs_config, set_prs_config
from grimoire.spaces import UNASSIGNED
from grimoire.web.spaces import COOKIE_NAME

NOW = datetime.now(UTC)


def _pr(number: int, repo: str, author: str, labels: list[str] | None = None) -> OpenPullRequest:
    return OpenPullRequest(
        number=number,
        title=f"PR {number} of {repo}",
        url=f"https://github.com/{repo}/pull/{number}",
        author=author,
        created_at=NOW,
        labels=labels or [],
        ci_state="success",
    )


@pytest.fixture
def prs(overlap_spaces: None) -> Iterator[None]:
    """Groups Bots (renovate), Humans (alice) and Docs (label docs); PRs spread over the repos.

    o/shared: renovate.  o/solo: renovate, alice (docs).  o/loose: renovate.
    """
    saved = get_prs_config()
    set_prs_config(
        PRsConfig(
            groups=[
                PRGroup(name="Bots", authors=["app/renovate"]),
                PRGroup(name="Humans", authors=["alice"]),
                PRGroup(name="Docs", labels=["docs"]),
            ]
        )
    )
    _cache["o/shared"].open_pr_items = [_pr(1, "o/shared", "renovate[bot]")]
    _cache["o/solo"].open_pr_items = [
        _pr(2, "o/solo", "renovate[bot]"),
        _pr(3, "o/solo", "alice", ["docs"]),
    ]
    _cache["o/loose"].open_pr_items = [_pr(4, "o/loose", "renovate[bot]")]
    yield
    set_prs_config(saved)


def _group_titles(html: str) -> list[str]:
    import re

    return re.findall(r'<span class="text-base font-semibold">([^<]+)</span>', html)


@pytest.mark.parametrize("path", ["/prs", "/partials/prs"])
class TestHtml:
    async def test_all_shows_every_group_and_pr(
        self, prs: None, overlap_client: AsyncClient, path: str
    ) -> None:
        html = (await overlap_client.get(path)).text
        assert _group_titles(html) == ["Bots", "Humans", "Docs"]
        assert all(f"PR {n} of" in html for n in (1, 2, 3, 4))

    async def test_one_space_hides_other_repos_and_empty_groups(
        self, prs: None, overlap_client: AsyncClient, path: str
    ) -> None:
        overlap_client.cookies.set(COOKIE_NAME, "b")  # only o/shared, which has a renovate PR
        html = (await overlap_client.get(path)).text
        assert _group_titles(html) == ["Bots"]
        assert "PR 1 of o/shared" in html
        assert not any(f"PR {n} of" in html for n in (2, 3, 4))

    async def test_several_spaces(self, prs: None, overlap_client: AsyncClient, path: str) -> None:
        overlap_client.cookies.set(COOKIE_NAME, "a,b")
        html = (await overlap_client.get(path)).text
        assert _group_titles(html) == ["Bots", "Humans", "Docs"]
        assert all(f"PR {n} of" in html for n in (1, 2, 3))
        assert "PR 4 of" not in html

    async def test_unassigned(self, prs: None, overlap_client: AsyncClient, path: str) -> None:
        overlap_client.cookies.set(COOKIE_NAME, UNASSIGNED)
        html = (await overlap_client.get(path)).text
        assert _group_titles(html) == ["Bots"]
        assert "PR 4 of o/loose" in html

    async def test_group_matching_still_applies_on_top(
        self, prs: None, overlap_client: AsyncClient, path: str
    ) -> None:
        overlap_client.cookies.set(COOKIE_NAME, "a")
        html = (await overlap_client.get(path)).text
        # alice's PR is in Humans and Docs only; renovate's only in Bots
        humans = html.split('<span class="text-base font-semibold">Humans</span>')[1].split(
            "</details>"
        )[0]
        assert "PR 3 of o/solo" in humans and "PR 2 of" not in humans

    async def test_chip_counts_follow_the_selection(
        self, prs: None, overlap_client: AsyncClient, path: str
    ) -> None:
        overlap_client.cookies.set(COOKIE_NAME, UNASSIGNED)
        html = (await overlap_client.get(path)).text
        total = html.split("Total")[1].split("</button>")[0]
        assert ">1<" in total.replace(" ", "").replace("\n", "")

    async def test_badges_next_to_repo_names(
        self, prs: None, overlap_client: AsyncClient, path: str
    ) -> None:
        html = (await overlap_client.get(path)).text
        assert 'title="Space: Alpha"' in html and 'title="Space: Beta"' in html

    async def test_message_when_no_group_has_a_pr_in_the_selection(
        self, prs: None, overlap_client: AsyncClient, path: str
    ) -> None:
        _cache["o/loose"].open_pr_items = []
        overlap_client.cookies.set(COOKIE_NAME, UNASSIGNED)
        html = (await overlap_client.get(path)).text
        assert _group_titles(html) == []
        assert "No open PRs in the selected spaces match any group" in html

    async def test_hidden_group_cannot_be_selected(
        self, prs: None, overlap_client: AsyncClient, path: str
    ) -> None:
        overlap_client.cookies.set(COOKIE_NAME, "b")
        html = (await overlap_client.get(path, params={"group": "Humans"})).text
        assert _group_titles(html) == ["Bots"]


class TestApi:
    async def test_api_ignores_the_cookie(self, prs: None, overlap_client: AsyncClient) -> None:
        overlap_client.cookies.set(COOKIE_NAME, "b")
        groups = {g["name"]: g for g in (await overlap_client.get("/api/prs")).json()["groups"]}
        assert {n: g["total"] for n, g in groups.items()} == {"Bots": 3, "Humans": 1, "Docs": 1}

    async def test_api_honours_the_query_and_keeps_empty_groups(
        self, prs: None, overlap_client: AsyncClient
    ) -> None:
        body = (await overlap_client.get("/api/prs", params={"spaces": "b"})).json()
        groups = {g["name"]: g for g in body["groups"]}
        assert list(groups) == ["Bots", "Humans", "Docs"]
        assert [p["repo"] for p in groups["Bots"]["pull_requests"]] == ["o/shared"]
        assert groups["Humans"]["pull_requests"] == [] and groups["Humans"]["total"] == 0

    @pytest.mark.parametrize(
        ("query", "bots"),
        [
            ("a", {"o/shared", "o/solo"}),
            ("_unassigned", {"o/loose"}),
            ("a,_unassigned", {"o/shared", "o/solo", "o/loose"}),
        ],
    )
    async def test_api_selections(
        self, prs: None, overlap_client: AsyncClient, query: str, bots: set[str]
    ) -> None:
        body = (await overlap_client.get("/api/prs", params={"spaces": query})).json()
        got = {
            p["repo"]
            for p in next(g for g in body["groups"] if g["name"] == "Bots")["pull_requests"]
        }
        assert got == bots
