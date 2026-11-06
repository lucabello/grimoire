"""Tests for the PRs page and its HTMX partial."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from grimoire.config import PRGroup, PRsConfig
from grimoire.github.router import update_cache
from grimoire.models import OpenPullRequest, RepositoryStats, TrackedRepository
from grimoire.prs import get_prs_config, set_prs_config

NOW = datetime.now(UTC)

RENOVATE = OpenPullRequest(
    number=12,
    title="Update dependency foo to v2",
    url="https://github.com/acme/api/pull/12",
    author="renovate[bot]",
    created_at=NOW - timedelta(days=9),
    updated_at=NOW - timedelta(hours=3),
    labels=["dependencies", "renovate"],
    base_ref="main",
    head_ref="renovate/foo-2.x",
    head_sha="aaa",
    auto_merge=True,
    ci_state="failure",
    review_decision="review_required",
    merge_state="blocked",
    ci_fetched_at=NOW - timedelta(minutes=5),
)
DEPENDABOT = OpenPullRequest(
    number=3,
    title="Bump bar from 1 to 2",
    url="https://github.com/acme/web/pull/3",
    author="dependabot[bot]",
    created_at=NOW - timedelta(days=30),
    updated_at=NOW - timedelta(days=2),
    draft=True,
    base_ref="develop",
    head_ref="dependabot/bar",
    head_sha="bbb",
    ci_state="success",
    review_decision="approved",
    merge_state="clean",
    ci_fetched_at=NOW,
)
NOCTUA = OpenPullRequest(
    number=7,
    title="Refresh rock lockfile",
    url="https://github.com/acme/web/pull/7",
    author="observability-noctua-bot",
    created_at=NOW - timedelta(days=1),
    base_ref="main",
    head_ref="noctua/lock",
    head_sha="ccc",
    ci_state="pending",
    review_decision=None,
    merge_state="behind",
    ci_fetched_at=NOW,
)
HUMAN = OpenPullRequest(
    number=20,
    title="Human change",
    url="https://github.com/acme/web/pull/20",
    author="alice",
    created_at=NOW,
    base_ref="main",
)
UNFETCHED = OpenPullRequest(
    number=21,
    title="Bot PR without CI data",
    url="https://github.com/acme/api/pull/21",
    author="renovate[bot]",
    created_at=NOW - timedelta(days=4),
    base_ref="main",
)

MAINTENANCE = PRGroup(
    name="Maintenance",
    authors=["app/renovate", "app/dependabot", "observability-noctua-bot"],
)


@pytest.fixture
def seed() -> Iterator[Callable[..., None]]:
    """Fill the cache and register groups; the groups are restored afterwards."""
    saved = get_prs_config()

    def use(
        groups: list[PRGroup],
        api: list[OpenPullRequest],
        web: list[OpenPullRequest],
        warnings: list[str] | None = None,
    ) -> None:
        set_prs_config(PRsConfig(groups=groups))
        repos = [
            TrackedRepository(full_name=n, default_branch="main", branches=["main"])
            for n in ("acme/api", "acme/web")
        ]
        stats = [
            RepositoryStats(
                full_name="acme/api",
                default_branch="main",
                open_pr_items=api,
                warnings=warnings or [],
            ),
            RepositoryStats(full_name="acme/web", default_branch="main", open_pr_items=web),
        ]
        update_cache(repos, stats)

    yield use
    set_prs_config(saved)


async def test_page_lists_the_matching_prs_of_the_group(
    web_client: AsyncClient, seed: Callable[..., None]
) -> None:
    seed([MAINTENANCE], [RENOVATE], [DEPENDABOT, NOCTUA, HUMAN])

    resp = await web_client.get("/prs")

    assert resp.status_code == 200
    html = resp.text
    assert "<title>PRs — Grimoire</title>" in html
    assert html.count("<details") == 1 and "Maintenance" in html
    assert "3 PRs in 2 repos" in html
    assert "1 failing" in html and "1 pending" in html
    for number in ("#12", "#3", "#7"):
        assert number in html
    assert "Human change" not in html
    # the PR title opens GitHub in a new tab
    assert (
        'href="https://github.com/acme/api/pull/12" target="_blank" rel="noopener noreferrer"'
        in html
    )
    # CI badge links to the checks tab
    assert 'href="https://github.com/acme/api/pull/12/checks"' in html
    assert "status-icon-failure" in html and "status-icon-success" in html
    assert "status-icon-pending" in html
    # repository link, labels, badges, branch
    assert 'href="/repo/acme/api"' in html
    assert ">dependencies<" in html and ">renovate<" in html
    assert "auto-merge" in html and ">draft<" in html
    assert "renovate/foo-2.x" in html
    # author normalised, bots get the robot icon
    assert 'title="Bot"></i> renovate' in html
    assert "[bot]" not in html
    assert ">observability-noctua-bot<" not in html  # no robot icon, but still shown
    assert "observability-noctua-bot" in html
    # review and merge state
    assert "review required" in html and "approved" in html
    assert "ready" in html and "behind" in html
    # failing rows get the accent
    assert "border-l-4 border-error" in html


async def test_review_required_hides_the_redundant_blocked_badge(
    web_client: AsyncClient, seed: Callable[..., None]
) -> None:
    seed([MAINTENANCE], [RENOVATE], [])
    html = (await web_client.get("/prs")).text
    assert "review required" in html
    assert ">blocked<" not in html


async def test_base_branch_is_shown_only_when_it_differs_from_the_default(
    web_client: AsyncClient, seed: Callable[..., None]
) -> None:
    seed([MAINTENANCE], [RENOVATE], [DEPENDABOT])
    html = (await web_client.get("/prs")).text
    assert "fa-arrow-right" in html and "develop" in html
    assert html.count("fa-arrow-right") == 1


async def test_pr_without_ci_data_shows_unavailable(
    web_client: AsyncClient, seed: Callable[..., None]
) -> None:
    seed([MAINTENANCE], [UNFETCHED], [])
    html = (await web_client.get("/prs")).text
    assert "CI status not available" in html
    assert "Bot PR without CI data" in html


async def test_summary_chips_count_distinct_prs(
    web_client: AsyncClient, seed: Callable[..., None]
) -> None:
    both = PRGroup(name="Renovate", authors=["app/renovate"])
    seed([MAINTENANCE, both], [RENOVATE, UNFETCHED], [DEPENDABOT, NOCTUA])

    html = (await web_client.get("/partials/prs")).text

    # 4 distinct PRs although RENOVATE and UNFETCHED are in two groups
    for label, count in (
        ("Total", 4),
        ("Failing", 1),
        ("Pending", 1),
        ("Passing", 1),
        ("No checks", 0),
        ("Unknown", 1),
    ):
        assert re.search(
            rf'{label}\s*<span class="font-mono-nums opacity-70">{count}</span>', html
        ), label
    assert html.count('id="prs-group-') == 2


async def test_ci_filter_keeps_matching_prs_and_marks_the_chip(
    web_client: AsyncClient, seed: Callable[..., None]
) -> None:
    seed([MAINTENANCE], [RENOVATE], [DEPENDABOT, NOCTUA])

    html = (await web_client.get("/partials/prs?ci=failure")).text

    assert "Update dependency foo" in html
    assert "Bump bar" not in html and "Refresh rock" not in html
    assert 'name="ci" value="failure"' in html  # out-of-band update of the form input
    assert "No PRs match the current filters" not in html


async def test_filter_without_result_is_not_the_celebration_state(
    web_client: AsyncClient, seed: Callable[..., None]
) -> None:
    seed([MAINTENANCE], [RENOVATE], [])
    html = (await web_client.get("/partials/prs?ci=success")).text
    assert "No PRs match the current filters" in html
    assert "🎉" not in html


async def test_search_matches_repo_title_and_author(
    web_client: AsyncClient, seed: Callable[..., None]
) -> None:
    seed([MAINTENANCE], [RENOVATE], [DEPENDABOT, NOCTUA])

    by_title = (await web_client.get("/partials/prs?search=lockfile")).text
    assert "Refresh rock lockfile" in by_title and "Bump bar" not in by_title
    by_author = (await web_client.get("/partials/prs?search=dependabot")).text
    assert "Bump bar" in by_author and "Update dependency" not in by_author
    by_repo = (await web_client.get("/partials/prs?search=acme/web")).text
    assert "Bump bar" in by_repo and "Update dependency" not in by_repo


async def test_sort_orders(web_client: AsyncClient, seed: Callable[..., None]) -> None:
    seed([MAINTENANCE], [RENOVATE], [DEPENDABOT, NOCTUA])

    def order(html: str) -> list[int]:
        titles = ["Update dependency", "Bump bar", "Refresh rock"]
        return [i for _, i in sorted((html.index(t), n) for n, t in enumerate(titles))]

    attention = (await web_client.get("/partials/prs")).text
    assert order(attention) == [0, 2, 1]  # failing, pending, passing
    age = (await web_client.get("/partials/prs?sort=age")).text
    assert order(age) == [1, 0, 2]  # oldest first
    updated = (await web_client.get("/partials/prs?sort=updated")).text
    assert order(updated) == [0, 2, 1]  # last update, or creation when never updated
    repo = (await web_client.get("/partials/prs?sort=repo")).text
    assert order(repo) == [0, 1, 2]


async def test_group_selector_limits_the_groups(
    web_client: AsyncClient, seed: Callable[..., None]
) -> None:
    seed(
        [MAINTENANCE, PRGroup(name="Drafts", draft=True)],
        [RENOVATE],
        [DEPENDABOT],
    )

    page = (await web_client.get("/prs")).text
    assert 'id="prs-group"' in page and page.count("<details") == 2
    only = (await web_client.get("/partials/prs?group=Drafts")).text
    assert only.count("<details") == 1 and "Drafts" in only and "Maintenance" not in only


async def test_unknown_query_values_are_ignored(
    web_client: AsyncClient, seed: Callable[..., None]
) -> None:
    seed([MAINTENANCE], [RENOVATE], [])
    resp = await web_client.get("/prs?sort=bogus&ci=bogus&group=bogus")
    assert resp.status_code == 200
    assert "Update dependency foo" in resp.text


async def test_no_groups_explains_how_to_configure(
    web_client: AsyncClient, seed: Callable[..., None]
) -> None:
    seed([], [RENOVATE], [])
    html = (await web_client.get("/prs")).text
    assert "No PR groups configured" in html
    assert "prs.groups" in html and "authors:" in html
    assert "<details" not in html


async def test_waiting_for_the_first_refresh(
    web_client: AsyncClient, seed: Callable[..., None]
) -> None:
    seed([MAINTENANCE], [], [])
    update_cache([], [])
    html = (await web_client.get("/prs")).text
    assert "Waiting for the first refresh" in html
    assert "<details" not in html


async def test_group_without_matching_prs(
    web_client: AsyncClient, seed: Callable[..., None]
) -> None:
    seed([MAINTENANCE], [HUMAN], [])
    html = (await web_client.get("/prs")).text
    assert "Maintenance" in html
    assert "No open PRs match this group 🎉" in html
    assert "0 PRs in 0 repos" in html


async def test_ci_fetch_warnings_show_one_line_with_the_count(
    web_client: AsyncClient, seed: Callable[..., None]
) -> None:
    seed(
        [MAINTENANCE],
        [RENOVATE],
        [],
        warnings=["Could not fetch CI status for acme/api: GraphQL query failed: boom"],
    )
    html = (await web_client.get("/prs")).text
    assert "CI status could not be fetched (1 warning)" in html
    assert "GraphQL query failed: boom" in html


async def test_nav_links_to_the_page(web_client: AsyncClient) -> None:
    html = (await web_client.get("/")).text
    assert 'href="/prs"' in html and "PRs</a>" in html
    assert "Maintenance</a>" not in html


async def test_links_are_prefixed_under_root_path(
    web_client_with_root_path: AsyncClient, seed: Callable[..., None]
) -> None:
    seed([MAINTENANCE], [RENOVATE], [])
    resp = await web_client_with_root_path.get("/prs")
    assert resp.status_code == 200
    assert 'href="/grimoire/prs"' in resp.text
    assert 'hx-get="/grimoire/partials/prs"' in resp.text
    assert 'href="/grimoire/repo/acme/api"' in resp.text
