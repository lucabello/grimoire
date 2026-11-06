"""Tests for PR groups: matching, grouping, page helpers and the API route."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from grimoire.config import PRGroup, PRsConfig
from grimoire.github.router import update_cache
from grimoire.models import OpenPullRequest, RepositoryStats, TrackedRepository
from grimoire.prs import (
    build_group_results,
    distinct_ci_counts,
    filter_by_ci,
    get_prs_config,
    matches,
    normalize_author,
    search_results,
    set_prs_config,
    sort_pull_requests,
)


def _group(**fields: object) -> PRGroup:
    return PRGroup.model_validate({"name": "g", **fields})


def _pr(number: int = 1, **fields: object) -> OpenPullRequest:
    values: dict[str, object] = {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/acme/api/pull/{number}",
        "author": "alice",
        "created_at": datetime(2026, 1, number, tzinfo=UTC),
        "base_ref": "main",
    }
    values.update(fields)
    return OpenPullRequest.model_validate(values)


class TestNormalizeAuthor:
    @pytest.mark.parametrize(
        ("login", "expected"),
        [
            ("renovate[bot]", "renovate"),
            ("app/renovate", "renovate"),
            ("Renovate", "renovate"),
            ("app/dependabot", "dependabot"),
            ("dependabot[bot]", "dependabot"),
            ("observability-noctua-bot", "observability-noctua-bot"),
            ("alice", "alice"),
        ],
    )
    def test_normalised_forms(self, login: str, expected: str) -> None:
        assert normalize_author(login) == expected


class TestMatches:
    def test_bot_group_matches_the_bots_and_nobody_else(self) -> None:
        group = _group(authors=["app/renovate", "app/dependabot", "observability-noctua-bot"])
        for author in ("renovate[bot]", "dependabot[bot]", "observability-noctua-bot"):
            assert matches(group, "acme/api", _pr(author=author)), author
        for author in ("alice", "github-actions[bot]", "renovate-fork"):
            assert not matches(group, "acme/api", _pr(author=author)), author

    def test_labels_are_alternatives_ignoring_case(self) -> None:
        group = _group(labels=["Dependencies", "security"])
        assert matches(group, "acme/api", _pr(labels=["dependencies"]))
        assert matches(group, "acme/api", _pr(labels=["bug", "SECURITY"]))
        assert not matches(group, "acme/api", _pr(labels=["bug"]))

    def test_title_words_are_case_insensitive_substrings(self) -> None:
        group = _group(title_words=["bump", "deps"])
        assert matches(group, "acme/api", _pr(title="Update DEPS in foo"))
        assert matches(group, "acme/api", _pr(title="BUMP foo to 2"))
        assert not matches(group, "acme/api", _pr(title="Fix typo"))

    def test_draft_and_base_must_equal(self) -> None:
        assert matches(_group(draft=True), "acme/api", _pr(draft=True))
        assert not matches(_group(draft=True), "acme/api", _pr(draft=False))
        assert matches(_group(draft=False), "acme/api", _pr(draft=False))
        assert matches(_group(base="develop"), "acme/api", _pr(base_ref="develop"))
        assert not matches(_group(base="develop"), "acme/api", _pr(base_ref="main"))

    def test_different_matchers_are_anded(self) -> None:
        group = _group(authors=["app/renovate"], labels=["dependencies"])
        assert matches(group, "acme/api", _pr(author="renovate[bot]", labels=["dependencies"]))
        assert not matches(group, "acme/api", _pr(author="renovate[bot]", labels=["other"]))
        assert not matches(group, "acme/api", _pr(author="alice", labels=["dependencies"]))

    def test_repos_scope_uses_globs(self) -> None:
        group = _group(authors=["alice"], repos=["acme/*", "tools/lint"])
        assert matches(group, "acme/api", _pr())
        assert matches(group, "tools/lint", _pr())
        assert not matches(group, "tools/other", _pr())


def _stats(name: str, *prs: OpenPullRequest, default_branch: str = "main") -> RepositoryStats:
    return RepositoryStats(full_name=name, default_branch=default_branch, open_pr_items=list(prs))


class TestGrouping:
    def test_groups_follow_config_order_and_repos_are_sorted(self) -> None:
        config = PRsConfig(
            groups=[
                PRGroup(name="bots", authors=["bob"]),
                PRGroup(name="all", authors=["alice", "bob"]),
            ]
        )
        cache = {
            "acme/web": _stats("acme/web", _pr(1, author="alice")),
            "acme/api": _stats("acme/api", _pr(2, author="bob"), _pr(3, author="alice")),
        }

        results = build_group_results(config, cache)

        assert [g.name for g in results] == ["bots", "all"]
        assert [(i.repo, i.pr.number) for i in results[0].pull_requests] == [("acme/api", 2)]
        # a pull request matching several groups appears in each
        assert [(i.repo, i.pr.number) for i in results[1].pull_requests] == [
            ("acme/api", 2),
            ("acme/api", 3),
            ("acme/web", 1),
        ]

    def test_group_summary_counts(self) -> None:
        cache = {
            "acme/api": _stats(
                "acme/api",
                _pr(1, ci_state="failure"),
                _pr(2, ci_state="pending"),
                _pr(3, ci_state="success"),
                _pr(4, ci_state="none"),
                _pr(5, ci_state=None),
            ),
            "acme/web": _stats("acme/web", _pr(6, ci_state="failure")),
        }
        (group,) = build_group_results(PRsConfig(groups=[_group(authors=["alice"])]), cache)

        assert (group.total, group.failing, group.pending, group.passing, group.no_checks) == (
            6,
            2,
            1,
            1,
            1,
        )
        assert group.repo_count == 2

    def test_item_exposes_the_checks_url_and_repo_default_branch(self) -> None:
        cache = {"acme/api": _stats("acme/api", _pr(1), default_branch="develop")}
        (group,) = build_group_results(PRsConfig(groups=[_group(authors=["alice"])]), cache)
        item = group.pull_requests[0]
        assert item.ci_url == "https://github.com/acme/api/pull/1/checks"
        assert item.default_branch == "develop"


def _grouped(*prs: OpenPullRequest, repo: str = "acme/api"):
    cache = {repo: _stats(repo, *prs)}
    return build_group_results(PRsConfig(groups=[_group(authors=["alice"])]), cache)


class TestPageHelpers:
    def test_search_matches_repo_title_and_normalised_author(self) -> None:
        cache = {
            "acme/api": _stats(
                "acme/api",
                _pr(1, title="Update foo", author="renovate[bot]"),
                _pr(2, title="Fix bar", author="alice"),
            )
        }
        config = PRsConfig(groups=[_group(authors=["alice", "app/renovate"])])
        results = build_group_results(config, cache)
        assert [i.pr.number for i in search_results(results, "FOO")[0].pull_requests] == [1]
        assert [i.pr.number for i in search_results(results, "renovate")[0].pull_requests] == [1]
        assert [i.pr.number for i in search_results(results, "acme/ap")[0].pull_requests] == [1, 2]
        assert search_results(results, "  ") is results

    def test_ci_filter_including_unknown(self) -> None:
        results = _grouped(_pr(1, ci_state="failure"), _pr(2, ci_state=None), _pr(3))
        assert [i.pr.number for i in filter_by_ci(results, "failure")[0].pull_requests] == [1]
        assert [i.pr.number for i in filter_by_ci(results, "unknown")[0].pull_requests] == [2, 3]
        assert filter_by_ci(results, "") is results

    def test_attention_sort_puts_failing_then_pending_first_oldest_first(self) -> None:
        items = _grouped(
            _pr(1, ci_state="success"),
            _pr(2, ci_state="pending"),
            _pr(3, ci_state="failure"),
            _pr(4, ci_state="failure", created_at=datetime(2025, 5, 1, tzinfo=UTC)),
            _pr(5, ci_state=None),
            _pr(6, ci_state="none"),
        )[0].pull_requests
        assert [i.pr.number for i in sort_pull_requests(items, "attention")] == [4, 3, 2, 5, 6, 1]

    def test_other_sorts(self) -> None:
        items = _grouped(
            _pr(1, updated_at=datetime(2026, 3, 1, tzinfo=UTC)),
            _pr(2, updated_at=datetime(2026, 4, 1, tzinfo=UTC)),
            _pr(3),
        )[0].pull_requests
        assert [i.pr.number for i in sort_pull_requests(items, "age")] == [1, 2, 3]
        assert [i.pr.number for i in sort_pull_requests(items, "updated")] == [2, 1, 3]
        assert [i.pr.number for i in sort_pull_requests(items[::-1], "repo")] == [1, 2, 3]

    def test_distinct_counts_count_a_pull_request_once_across_groups(self) -> None:
        cache = {"acme/api": _stats("acme/api", _pr(1, ci_state="failure"), _pr(2, ci_state=None))}
        config = PRsConfig(groups=[_group(authors=["alice"]), PRGroup(name="h", draft=False)])
        counts = distinct_ci_counts(build_group_results(config, cache))
        assert counts == {
            "total": 2,
            "failure": 1,
            "pending": 0,
            "success": 0,
            "none": 0,
            "unknown": 1,
        }


@pytest.fixture
def use_groups() -> Iterator[None]:
    """Restore the registered PR groups after the test."""
    saved = get_prs_config()
    yield
    set_prs_config(saved)


async def test_api_returns_groups_without_github_calls(
    async_client: AsyncClient, use_groups: None
) -> None:
    update_cache(
        [TrackedRepository(full_name="acme/api", default_branch="main", branches=["main"])],
        [
            _stats(
                "acme/api",
                _pr(
                    12,
                    author="renovate[bot]",
                    title="Update dependency foo",
                    labels=["dependencies"],
                    head_ref="renovate/foo",
                    head_sha="abc123",
                    auto_merge=True,
                    ci_state="failure",
                    review_decision="review_required",
                    merge_state="blocked",
                    ci_fetched_at=datetime(2026, 1, 2, tzinfo=UTC),
                ),
                _pr(13, ci_state="success"),
            )
        ],
        timestamp=datetime(2026, 1, 3, 12, tzinfo=UTC),
    )
    set_prs_config(PRsConfig(groups=[PRGroup(name="Maintenance", authors=["app/renovate"])]))

    resp = await async_client.get("/api/prs")

    assert resp.status_code == 200
    body = resp.json()
    assert body["last_refresh"] == "2026-01-03T12:00:00Z"
    (group,) = body["groups"]
    assert (group["name"], group["total"], group["failing"]) == ("Maintenance", 1, 1)
    assert (group["pending"], group["passing"], group["no_checks"]) == (0, 0, 0)
    assert group["pull_requests"] == [
        {
            "repo": "acme/api",
            "number": 12,
            "title": "Update dependency foo",
            "url": "https://github.com/acme/api/pull/12",
            "author": "renovate[bot]",
            "created_at": "2026-01-12T00:00:00Z",
            "updated_at": None,
            "draft": False,
            "labels": ["dependencies"],
            "base": "main",
            "head_ref": "renovate/foo",
            "head_sha": "abc123",
            "auto_merge": True,
            "ci": "failure",
            "ci_url": "https://github.com/acme/api/pull/12/checks",
            "review_decision": "review_required",
            "merge_state": "blocked",
            "ci_checked_at": "2026-01-02T00:00:00Z",
        }
    ]


async def test_api_without_groups_is_empty(async_client: AsyncClient, use_groups: None) -> None:
    set_prs_config(PRsConfig())
    resp = await async_client.get("/api/prs")
    assert resp.status_code == 200
    assert resp.json()["groups"] == []


async def test_removed_maintenance_routes_are_gone(async_client: AsyncClient) -> None:
    assert (await async_client.get("/api/maintenance/groups")).status_code == 404
    assert (await async_client.get("/maintenance")).status_code == 404
