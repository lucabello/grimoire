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
            SpaceConfig(id="a", name="Alpha", color="#112233", repositories=source),
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
                "color": "#112233",
                "description": "",
                "repo_count": 1,
            },
            {"id": "b", "name": "Beta", "color": None, "description": "", "repo_count": 1},
            {
                "id": "_unassigned",
                "name": "Unassigned",
                "color": None,
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
