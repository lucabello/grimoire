"""Tests for the space selection of HTML pages and the spaces API."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from grimoire.config import SpaceConfig, StaticRepoSource
from grimoire.github.router import get_tracked_repos
from grimoire.models import RepoMembership
from grimoire.spaces import SpaceSelection, api_space_selection, router, set_spaces_config
from grimoire.web.spaces import COOKIE_NAME, html_space_selection, space_cookie_middleware


@pytest.fixture
def spaces() -> Iterator[None]:
    """Two spaces over the repos of the web test cache; acme/frontend is in `a` and `b`."""
    from tests.test_web.conftest import _populate_cache

    _populate_cache()
    source = [StaticRepoSource(repo="acme/api")]
    set_spaces_config(
        [
            SpaceConfig(id="a", name="Alpha", repositories=source),
            SpaceConfig(id="b", name="Beta", repositories=source),
        ],
        get_tracked_repos,
    )
    memberships = {
        "acme/api": [RepoMembership()],  # unassigned
        "acme/frontend": [RepoMembership(space="a"), RepoMembership(space="b")],
    }
    for repo in get_tracked_repos():
        repo.memberships = memberships[repo.full_name]
    yield
    set_spaces_config([])


def _probe_app(root_path: str = "") -> FastAPI:
    """A minimal app exposing the HTML and API dependencies as JSON."""
    app = FastAPI(root_path=root_path)
    app.middleware("http")(space_cookie_middleware)

    @app.get("/page")
    async def page(selection: SpaceSelection = Depends(html_space_selection)) -> dict:
        return {"ids": None if selection.ids is None else sorted(selection.ids)}

    @app.get("/api/probe")
    async def api(selection: SpaceSelection = Depends(api_space_selection)) -> dict:
        return {"ids": None if selection.ids is None else sorted(selection.ids)}

    app.include_router(router, prefix="/api")
    return app


@pytest.fixture
async def probe(spaces: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=_probe_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestHtmlSelection:
    async def test_defaults_to_all_without_cookie_or_query(self, probe: AsyncClient) -> None:
        resp = await probe.get("/page")
        assert resp.json() == {"ids": None}
        assert "set-cookie" not in resp.headers

    async def test_cookie_is_read(self, probe: AsyncClient) -> None:
        probe.cookies.set(COOKIE_NAME, "b")
        assert (await probe.get("/page")).json() == {"ids": ["b"]}

    async def test_cookie_with_several_ids_and_unknown_ones(self, probe: AsyncClient) -> None:
        probe.cookies.set(COOKIE_NAME, "a,nope,_unassigned")
        assert (await probe.get("/page")).json() == {"ids": ["_unassigned", "a"]}

    async def test_query_beats_cookie_and_is_saved(self, probe: AsyncClient) -> None:
        probe.cookies.set(COOKIE_NAME, "b")
        resp = await probe.get("/page", params={"spaces": "a"})
        assert resp.json() == {"ids": ["a"]}
        cookie = resp.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE_NAME}=a;")
        assert "Max-Age=31536000" in cookie
        assert "SameSite=Lax" in cookie
        assert "Path=/" in cookie.split("; ")

    async def test_saved_cookie_is_comma_separated_and_sorted(self, probe: AsyncClient) -> None:
        resp = await probe.get("/page", params={"spaces": "b,_unassigned"})
        assert resp.headers["set-cookie"].startswith(f"{COOKIE_NAME}=_unassigned,b;")
        # The browser sends it back unquoted and it round-trips.
        assert (await probe.get("/page")).json() == {"ids": ["_unassigned", "b"]}

    async def test_empty_query_resets_to_all_and_clears_the_saved_selection(
        self, probe: AsyncClient
    ) -> None:
        probe.cookies.set(COOKIE_NAME, "b")
        resp = await probe.get("/page", params={"spaces": ""})
        assert resp.json() == {"ids": None}
        assert resp.headers["set-cookie"].startswith(f"{COOKIE_NAME}=;")

    async def test_selecting_every_space_is_saved_as_all(self, probe: AsyncClient) -> None:
        resp = await probe.get("/page", params={"spaces": "a,b,_unassigned"})
        assert resp.json() == {"ids": None}
        assert resp.headers["set-cookie"].startswith(f"{COOKIE_NAME}=;")

    async def test_unknown_ids_are_not_saved(self, probe: AsyncClient) -> None:
        resp = await probe.get("/page", params={"spaces": "a,gone"})
        assert resp.headers["set-cookie"].startswith(f"{COOKIE_NAME}=a;")

    async def test_cookie_path_is_the_root_path(self, spaces: None) -> None:
        transport = ASGITransport(app=_probe_app(), root_path="/grimoire")
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/page", params={"spaces": "a"})
        assert "Path=/grimoire" in resp.headers["set-cookie"].split("; ")


class TestApiSelection:
    async def test_api_ignores_the_cookie(self, probe: AsyncClient) -> None:
        probe.cookies.set(COOKIE_NAME, "b")
        resp = await probe.get("/api/probe")
        assert resp.json() == {"ids": None}

    async def test_api_reads_the_query_and_never_sets_the_cookie(self, probe: AsyncClient) -> None:
        resp = await probe.get("/api/probe", params={"spaces": "a,nope"})
        assert resp.json() == {"ids": ["a"]}
        assert "set-cookie" not in resp.headers


class TestSpacesEndpoint:
    async def test_lists_spaces_with_counts_and_unassigned(self, probe: AsyncClient) -> None:
        resp = await probe.get("/api/spaces")
        assert resp.status_code == 200
        assert resp.json() == [
            {
                "id": "a",
                "name": "Alpha",
                "description": "",
                "repo_count": 1,
            },
            {"id": "b", "name": "Beta", "description": "", "repo_count": 1},
            {
                "id": "_unassigned",
                "name": "Unassigned",
                "description": "",
                "repo_count": 1,
            },
        ]

    async def test_no_unassigned_entry_without_unassigned_repos(self, probe: AsyncClient) -> None:
        for repo in get_tracked_repos():
            repo.memberships = [RepoMembership(space="a")]
        ids = [s["id"] for s in (await probe.get("/api/spaces")).json()]
        assert ids == ["a", "b"]

    async def test_empty_without_configured_spaces(self, web_client: AsyncClient) -> None:
        set_spaces_config([])
        assert (await web_client.get("/api/spaces")).json() == []


class TestRealApp:
    async def test_templates_get_spaces_and_selection(self, spaces: None) -> None:
        from fastapi import Request

        from grimoire.web.spaces import space_context

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [(b"cookie", f"{COOKIE_NAME}=b".encode())],
                "query_string": b"",
            }
        )
        context = space_context(request)
        assert [s["id"] for s in context["spaces"]] == ["a", "b", "_unassigned"]
        assert context["space_selection"] == SpaceSelection(frozenset({"b"}))

    async def test_a_page_with_spaces_query_saves_the_cookie(
        self, spaces: None, web_client: AsyncClient
    ) -> None:
        resp = await web_client.get("/", params={"spaces": "a"})
        assert resp.status_code == 200
        assert resp.headers["set-cookie"].startswith(f"{COOKIE_NAME}=a;")

    async def test_a_page_without_query_does_not_touch_the_cookie(
        self, spaces: None, web_client: AsyncClient
    ) -> None:
        web_client.cookies.set(COOKIE_NAME, "a")
        resp = await web_client.get("/")
        assert "set-cookie" not in resp.headers


class TestSelectorUI:
    async def test_absent_without_configured_spaces(self, web_client: AsyncClient) -> None:
        set_spaces_config([])
        for path in ("/", "/backlog", "/prs"):
            assert 'id="space-selector"' not in (await web_client.get(path)).text

    async def test_present_with_one_row_per_space_and_unassigned(
        self, spaces: None, web_client: AsyncClient
    ) -> None:
        html = (await web_client.get("/")).text
        assert 'id="space-selector"' in html
        for space_id in ("a", "b", "_unassigned"):
            assert f'data-space-id="{space_id}"' in html
        assert "saved in this browser" in html
        assert 'id="space-search"' in html  # the dropdown can be searched
        assert 'data-space-row="alpha"' in html and 'data-space-row="beta"' in html

    async def test_unassigned_row_only_when_offered(
        self, spaces: None, web_client: AsyncClient
    ) -> None:
        for repo in get_tracked_repos():
            repo.memberships = [RepoMembership(space="a")]
        html = (await web_client.get("/")).text
        assert 'data-space-id="a"' in html
        assert 'data-space-id="_unassigned"' not in html

    @pytest.mark.parametrize(
        ("cookie", "label"),
        [
            ("", "Spaces"),
            ("b", "Beta"),
            ("a,b", "Spaces (2)"),
            ("a,b,_unassigned", "Spaces"),
        ],
    )
    async def test_collapsed_label(
        self, spaces: None, web_client: AsyncClient, cookie: str, label: str
    ) -> None:
        web_client.cookies.set(COOKIE_NAME, cookie)
        html = (await web_client.get("/")).text
        assert f'id="space-selector-label" class="whitespace-nowrap">{label}</span>' in html

    async def test_checked_boxes_follow_the_selection(
        self, spaces: None, web_client: AsyncClient
    ) -> None:
        web_client.cookies.set(COOKIE_NAME, "b")
        html = (await web_client.get("/")).text
        row_b = html.split('data-space-id="b"')[1].split(">")[0]
        row_a = html.split('data-space-id="a"')[1].split(">")[0]
        assert "checked" in row_b
        assert "checked" not in row_a

    async def test_name_is_the_label_of_the_checkbox_and_only_is_separate(
        self, spaces: None, web_client: AsyncClient
    ) -> None:
        html = (await web_client.get("/")).text
        row = html.split('data-space-row="alpha"')[1].split("</li>")[0]
        # clicking the name toggles the checkbox, like clicking the box itself
        assert row.index("<label") < row.index('data-space-id="a"') < row.index("Alpha</span>")
        assert row.index("Alpha</span>") < row.index("</label>")
        # "only" is the one control that selects a single space, and the name is not one
        assert row.count("data-space-only") == 1
        assert ">only</button>" in row

    async def test_repo_counts_are_spelled_out(
        self, spaces: None, web_client: AsyncClient
    ) -> None:
        for repo in get_tracked_repos():
            repo.memberships = [RepoMembership(space="a")]
        html = (await web_client.get("/")).text
        row_a = html.split('data-space-row="alpha"')[1].split("</li>")[0]
        row_b = html.split('data-space-row="beta"')[1].split("</li>")[0]
        assert "2 repos<" in row_a  # both cached repos are in a
        assert "0 repos<" in row_b
        for repo in get_tracked_repos()[:1]:
            repo.memberships = [RepoMembership(space="b")]
        html = (await web_client.get("/")).text
        assert "1 repo<" in html.split('data-space-row="beta"')[1].split("</li>")[0]
        assert "1 repos" not in html

    async def test_cookie_is_written_with_the_root_path(
        self, spaces: None, web_client_with_root_path: AsyncClient
    ) -> None:
        html = (await web_client_with_root_path.get("/")).text
        assert 'const BASE_PATH = "/grimoire";' in html
        assert "Path=' +\n            (BASE_PATH || '/') + '; SameSite=Lax" in html

    async def test_markup_avoids_what_some_browsers_hide(
        self, spaces: None, web_client: AsyncClient
    ) -> None:
        """A user's browser hid a wrapper with `shrink-0` / `data-cookie-path`; keep it a <details>."""
        html = (await web_client.get("/")).text
        assert '<details class="dropdown" id="space-selector">' in html  # no shrink-0, no data-*
        assert "data-cookie-path" not in html

    async def test_nothing_is_conveyed_by_colour(
        self, spaces: None, web_client: AsyncClient
    ) -> None:
        """Spaces are told apart by name: no colour dots in the button, rows or badges."""
        web_client.cookies.set(COOKIE_NAME, "a")
        html = (await web_client.get("/")).text
        assert "space-selector-dots" not in html
        assert "data-space-dot" not in html
        assert (
            "background-color" not in html.split('id="space-selector"')[1].split("</details>")[0]
        )

    @pytest.mark.parametrize(
        ("cookie", "title"),
        [
            ("", "Filter every page by space"),
            ("b", "Showing: Beta"),
            ("a,b", "Showing: Alpha, Beta"),
        ],
    )
    async def test_button_tooltip_names_the_selection(
        self, spaces: None, web_client: AsyncClient, cookie: str, title: str
    ) -> None:
        web_client.cookies.set(COOKIE_NAME, cookie)
        html = (await web_client.get("/")).text
        assert f'aria-label="Filter by space" title="{title}"' in html

    async def test_repo_detail_lists_the_spaces_as_plain_text(
        self, spaces: None, web_client: AsyncClient
    ) -> None:
        from tests.test_web.conftest import _populate_cache

        _populate_cache()
        for repo in get_tracked_repos():
            repo.memberships = [RepoMembership(space="b"), RepoMembership(space="a")]
        html = (await web_client.get("/repo/acme/api")).text
        line = html.split('id="repo-spaces">')[1].split("</p>")[0].strip()
        assert line == "Spaces: Alpha, Beta"  # config order, no badges or chips
        assert "badge" not in line

    async def test_repo_detail_of_a_repo_in_no_space(
        self, spaces: None, web_client: AsyncClient
    ) -> None:
        from tests.test_web.conftest import _populate_cache

        _populate_cache()
        for repo in get_tracked_repos():
            repo.memberships = [RepoMembership()]
        html = (await web_client.get("/repo/acme/api")).text
        assert html.split('id="repo-spaces">')[1].split("</p>")[0].strip() == "Not in any space"

    async def test_repo_detail_has_no_spaces_line_without_spaces(
        self, web_client: AsyncClient
    ) -> None:
        set_spaces_config([])
        assert "repo-spaces" not in (await web_client.get("/repo/acme/api")).text


class TestFilterBar:
    """The bar under the navbar that says what is filtered and how to undo it."""

    async def test_absent_under_all(self, web_client: AsyncClient, spaces: None) -> None:
        for cookie in ("", "a,b,_unassigned"):
            web_client.cookies.set(COOKIE_NAME, cookie)
            assert 'id="space-filter-bar"' not in (await web_client.get("/")).text

    async def test_absent_without_configured_spaces(self, web_client: AsyncClient) -> None:
        set_spaces_config([])
        web_client.cookies.set(COOKIE_NAME, "a")
        assert 'id="space-filter-bar"' not in (await web_client.get("/")).text

    async def test_one_space(self, web_client: AsyncClient, spaces: None) -> None:
        web_client.cookies.set(COOKIE_NAME, "b")  # only acme/frontend of the two cached repos
        html = (await web_client.get("/")).text
        bar = html.split('id="space-filter-bar"')[1].split("</nav>")[0]
        assert "Showing" in bar and "Beta" in bar
        assert "1 of 2 repositories" in bar
        # removing the only chip leaves an empty value, which means All
        assert 'href="?spaces="' in bar
        assert 'aria-label="Remove Beta from the filter"' in bar

    async def test_each_chip_removes_only_its_space(
        self, web_client: AsyncClient, spaces: None
    ) -> None:
        web_client.cookies.set(COOKIE_NAME, "a,b,_unassigned")
        web_client.cookies.set(COOKIE_NAME, "a,b")
        html = (await web_client.get("/")).text
        bar = html.split('id="space-filter-bar"')[1].split("</nav>")[0]
        remove_a = bar.split('aria-label="Remove Alpha from the filter"')[0].rsplit("href=", 1)[1]
        remove_b = bar.split('aria-label="Remove Beta from the filter"')[0].rsplit("href=", 1)[1]
        assert remove_a.startswith('"?spaces=b"')
        assert remove_b.startswith('"?spaces=a"')

    async def test_the_links_apply_and_persist_the_new_selection(
        self, web_client: AsyncClient, spaces: None
    ) -> None:
        web_client.cookies.set(COOKIE_NAME, "a,b")
        resp = await web_client.get("/", params={"spaces": "b"})  # what the Alpha chip links to
        assert 'aria-label="Remove Beta from the filter"' in resp.text
        assert 'aria-label="Remove Alpha' not in resp.text
        assert resp.headers["set-cookie"].startswith(f"{COOKIE_NAME}=b;")
        cleared = await web_client.get("/", params={"spaces": ""})  # "Show all spaces"
        assert 'id="space-filter-bar"' not in cleared.text
        assert cleared.headers["set-cookie"].startswith(f"{COOKIE_NAME}=;")

    async def test_present_on_every_page(self, web_client: AsyncClient, spaces: None) -> None:
        web_client.cookies.set(COOKIE_NAME, "a")
        for path in ("/", "/backlog", "/checks", "/actions", "/prs", "/repo/acme/api"):
            assert 'id="space-filter-bar"' in (await web_client.get(path)).text, path

    async def test_unassigned_is_a_chip_too(self, web_client: AsyncClient, spaces: None) -> None:
        web_client.cookies.set(COOKIE_NAME, "_unassigned")
        bar = (await web_client.get("/")).text.split('id="space-filter-bar"')[1].split("</nav>")[0]
        assert "Unassigned" in bar and "1 of 2 repositories" in bar

    async def test_singular_total(self, web_client: AsyncClient, spaces: None) -> None:
        from grimoire.github.router import _repos

        _repos.pop("acme/api")
        web_client.cookies.set(COOKIE_NAME, "a")
        bar = (await web_client.get("/")).text.split('id="space-filter-bar"')[1].split("</nav>")[0]
        assert "1 of 1 repository" in bar and "repositories" not in bar
