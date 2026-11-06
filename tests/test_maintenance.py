"""Tests for maintenance groups: query building and the API route."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Iterator
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient

from grimoire.app import create_app
from grimoire.config import MaintenanceConfig, MaintenanceGroup
from grimoire.github.router import update_cache
from grimoire.maintenance import (
    MAX_URL_LENGTH,
    build_group_searches,
    get_maintenance_config,
    select_repos,
    set_maintenance_config,
)
from grimoire.models import TrackedRepository

TRACKED = ["acme/api", "acme/frontend", "other/tool"]


def _group(**fields: object) -> MaintenanceGroup:
    return MaintenanceGroup.model_validate({"name": "g", **fields})


def _only_query(group: MaintenanceGroup, tracked: list[str] | None = None) -> str:
    searches = build_group_searches(group, ["acme/api"] if tracked is None else tracked)
    assert len(searches) == 1
    return searches[0].query


class TestQuery:
    def test_always_open_pull_requests_in_the_repo(self) -> None:
        assert _only_query(_group(draft=True)) == "is:pr is:open repo:acme/api draft:true"

    def test_single_author(self) -> None:
        assert "author:alice" in _only_query(_group(authors=["alice"])).split()

    def test_app_author_is_not_rewritten(self) -> None:
        assert "author:app/renovate" in _only_query(_group(authors=["app/renovate"])).split()

    def test_authors_are_alternatives(self) -> None:
        query = _only_query(_group(authors=["app/renovate", "app/dependabot"]))
        assert "(author:app/renovate OR author:app/dependabot)" in query

    def test_labels_are_alternatives(self) -> None:
        query = _only_query(_group(labels=["dependencies", "security"]))
        assert "(label:dependencies OR label:security)" in query

    def test_title_words_search_only_the_title(self) -> None:
        assert "deps in:title" in _only_query(_group(title_words=["deps"]))

    def test_each_title_word_carries_its_own_scope(self) -> None:
        query = _only_query(_group(title_words=["deps", "bump"]))
        assert "(deps in:title OR bump in:title)" in query

    @pytest.mark.parametrize(("draft", "term"), [(True, "draft:true"), (False, "draft:false")])
    def test_draft(self, draft: bool, term: str) -> None:
        assert term in _only_query(_group(draft=draft)).split()

    def test_base(self) -> None:
        assert "base:release/1.2" in _only_query(_group(base="release/1.2")).split()

    @pytest.mark.parametrize("status", ["success", "failure", "pending"])
    def test_status(self, status: str) -> None:
        assert f"status:{status}" in _only_query(_group(status=status)).split()

    def test_matchers_combine_with_and(self) -> None:
        group = _group(
            authors=["app/renovate", "bot"],
            labels=["dependencies"],
            title_words=["deps"],
            draft=False,
            base="main",
            status="failure",
        )
        assert _only_query(group) == (
            "is:pr is:open repo:acme/api (author:app/renovate OR author:bot) "
            "label:dependencies deps in:title draft:false base:main status:failure"
        )

    def test_values_with_spaces_or_colons_are_quoted(self) -> None:
        query = _only_query(
            _group(labels=["help wanted", "kind:bug", "plain"], title_words=["remove unused"])
        )
        assert '(label:"help wanted" OR label:"kind:bug" OR label:plain)' in query
        assert '"remove unused" in:title' in query

    def test_every_or_is_parenthesised(self) -> None:
        """An unparenthesised OR makes GitHub drop the other qualifiers."""
        query = _only_query(
            _group(authors=["a", "b"], labels=["x", "y"], title_words=["p", "q"]),
            ["acme/api", "acme/frontend"],
        )
        depth = 0
        tokens = query.replace("(", " ( ").replace(")", " ) ").split()
        for token in tokens:
            depth += (token == "(") - (token == ")")
            if token == "OR":
                assert depth == 1
        assert depth == 0


class TestHostileValues:
    """Values come from the config; none may change what the query is scoped to."""

    @pytest.mark.parametrize(
        ("fields", "term"),
        [
            ({"title_words": ["OR"]}, '"OR" in:title'),
            ({"title_words": ["not"]}, '"not" in:title'),
            ({"title_words": ["And"]}, '"And" in:title'),
            ({"title_words": ["-wip"]}, '"-wip" in:title'),
            ({"title_words": ["repo:x"]}, '"repo:x" in:title'),
            ({"authors": ["OR"]}, 'author:"OR"'),
            ({"authors": ["-x"]}, 'author:"-x"'),
            ({"authors": ["repo:x"]}, 'author:"repo:x"'),
            ({"labels": ["NOT"]}, 'label:"NOT"'),
            ({"labels": ["-bug"]}, 'label:"-bug"'),
            ({"labels": ["repo:x"]}, 'label:"repo:x"'),
            ({"labels": ["is:draft"]}, 'label:"is:draft"'),
            ({"base": "-main"}, 'base:"-main"'),
        ],
    )
    def test_value_is_quoted_and_the_repo_scope_is_kept(
        self, fields: dict[str, object], term: str
    ) -> None:
        query = _only_query(_group(**fields))
        assert query == f"is:pr is:open repo:acme/api {term}"

    def test_scope_survives_several_hostile_alternatives(self) -> None:
        query = _only_query(
            _group(title_words=["OR", "-wip"], labels=["-bug", "kind"]),
            ["acme/api", "acme/frontend"],
        )
        assert query == (
            'is:pr is:open (repo:acme/api OR repo:acme/frontend) (label:"-bug" OR label:kind) '
            '("OR" in:title OR "-wip" in:title)'
        )

    def test_inner_dash_and_operator_substrings_stay_plain(self) -> None:
        query = _only_query(_group(title_words=["fix-up", "order"], labels=["android"]))
        assert query == (
            "is:pr is:open repo:acme/api label:android (fix-up in:title OR order in:title)"
        )


class TestScope:
    def test_all_tracked_repos_by_default(self) -> None:
        query = _only_query(_group(draft=True), TRACKED)
        assert "(repo:acme/api OR repo:acme/frontend OR repo:other/tool)" in query

    def test_repos_are_sorted_for_stable_links(self) -> None:
        assert select_repos(_group(draft=True), ["b/x", "a/z", "a/y"]) == ["a/y", "a/z", "b/x"]

    def test_repos_glob_narrows_the_scope(self) -> None:
        query = _only_query(_group(draft=True, repos=["acme/*"]), TRACKED)
        assert "(repo:acme/api OR repo:acme/frontend)" in query
        assert "other/tool" not in query

    def test_repos_accepts_several_patterns_and_exact_names(self) -> None:
        group = _group(draft=True, repos=["acme/f*", "other/tool"])
        assert select_repos(group, TRACKED) == ["acme/frontend", "other/tool"]

    def test_repos_glob_is_case_sensitive_on_the_full_name(self) -> None:
        assert select_repos(_group(draft=True, repos=["ACME/*"]), TRACKED) == []

    def test_no_search_when_nothing_is_in_scope(self) -> None:
        """A query without repo: would search all of GitHub."""
        assert build_group_searches(_group(draft=True, repos=["nobody/*"]), TRACKED) == []

    def test_no_search_without_tracked_repositories(self) -> None:
        assert build_group_searches(_group(draft=True), []) == []


class TestUrl:
    def test_url_opens_the_search_on_github(self) -> None:
        [search] = build_group_searches(_group(labels=["help wanted"]), ["acme/api"])
        parsed = urlparse(search.url)
        assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "github.com", "/pulls")
        assert parse_qs(parsed.query) == {"q": [search.query]}

    def test_url_is_encoded(self) -> None:
        [search] = build_group_searches(
            _group(authors=["app/renovate", "bot"], labels=["help wanted"]), ["acme/api"]
        )
        query_string = urlparse(search.url).query
        assert " " not in query_string
        assert all(char not in query_string for char in ':/()"')
        assert query_string.startswith("q=is%3Apr+is%3Aopen+repo%3Aacme%2Fapi+")


class TestSplitting:
    def test_link_of_exactly_the_maximum_length_stays_in_one_part(self) -> None:
        tracked = ["acme/a", "acme/b"]
        [probe] = build_group_searches(_group(title_words=["x"]), tracked)
        padding = "x" * (MAX_URL_LENGTH - len(probe.url) + 1)

        [search] = build_group_searches(_group(title_words=[padding]), tracked)

        assert len(search.url) == MAX_URL_LENGTH
        assert search.parts == 1

    def test_one_more_character_splits_the_repositories(self) -> None:
        tracked = ["acme/a", "acme/b"]
        [probe] = build_group_searches(_group(title_words=["x"]), tracked)
        padding = "x" * (MAX_URL_LENGTH - len(probe.url) + 2)

        searches = build_group_searches(_group(title_words=[padding]), tracked)

        assert [s.parts for s in searches] == [2, 2]
        assert [s.query.split()[2] for s in searches] == ["repo:acme/a", "repo:acme/b"]

    def test_one_search_for_a_normal_repo_count(self) -> None:
        [search] = build_group_searches(_group(draft=True), [f"acme/r{i}" for i in range(67)])
        assert (search.part, search.parts) == (1, 1)
        assert len(search.url) <= MAX_URL_LENGTH

    def test_many_repos_are_split_without_losing_any(self) -> None:
        tracked = [f"some-organization/a-repository-with-a-long-name-{i}" for i in range(500)]
        group = _group(authors=["app/renovate"], draft=False)

        searches = build_group_searches(group, tracked)

        assert len(searches) > 1
        assert [s.part for s in searches] == list(range(1, len(searches) + 1))
        assert {s.parts for s in searches} == {len(searches)}
        assert {s.name for s in searches} == {"g"}
        assert all(len(s.url) <= MAX_URL_LENGTH for s in searches)
        covered = [repo for s in searches for repo in re.findall(r"repo:([^\s)]+)", s.query)]
        assert sorted(covered) == sorted(tracked)
        assert all(s.query.endswith(" author:app/renovate draft:false") for s in searches), (
            "every part keeps all the matchers"
        )


class TestConfigState:
    def test_set_and_get_round_trip(self) -> None:
        saved = get_maintenance_config()
        config = MaintenanceConfig(groups=[_group(draft=True)])
        try:
            set_maintenance_config(config)
            assert get_maintenance_config() is config
        finally:
            set_maintenance_config(saved)


@pytest.fixture
def use_groups() -> Iterator[Callable[..., None]]:
    """Register groups for the test and restore the previous config afterwards."""
    saved = get_maintenance_config()

    def use(*groups: MaintenanceGroup) -> None:
        set_maintenance_config(MaintenanceConfig(groups=list(groups)))

    set_maintenance_config(MaintenanceConfig())
    yield use
    set_maintenance_config(saved)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    update_cache([TrackedRepository(full_name=name) for name in TRACKED], [])
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    update_cache([], [])


class TestApi:
    async def test_lists_name_query_and_url_per_group(
        self, client: AsyncClient, use_groups: Callable[..., None]
    ) -> None:
        use_groups(_group(authors=["app/renovate"], repos=["acme/*"]))

        resp = await client.get("/api/maintenance/groups")

        assert resp.status_code == 200
        [item] = resp.json()
        assert item["name"] == "g"
        assert item["query"] == (
            "is:pr is:open (repo:acme/api OR repo:acme/frontend) author:app/renovate"
        )
        assert parse_qs(urlparse(item["url"]).query) == {"q": [item["query"]]}

    async def test_keeps_the_configured_group_order(
        self, client: AsyncClient, use_groups: Callable[..., None]
    ) -> None:
        use_groups(
            _group(authors=["a"]).model_copy(update={"name": "second"}),
            _group(authors=["b"]).model_copy(update={"name": "first"}),
        )
        names = [item["name"] for item in (await client.get("/api/maintenance/groups")).json()]
        assert names == ["second", "first"]

    async def test_empty_without_groups(
        self, client: AsyncClient, use_groups: Callable[..., None]
    ) -> None:
        resp = await client.get("/api/maintenance/groups")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_uses_the_current_tracked_repositories(
        self, client: AsyncClient, use_groups: Callable[..., None]
    ) -> None:
        use_groups(_group(draft=True))
        update_cache([TrackedRepository(full_name="new/repo")], [])
        [item] = (await client.get("/api/maintenance/groups")).json()
        assert item["query"] == "is:pr is:open repo:new/repo draft:true"
