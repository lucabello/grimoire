"""Tests for serving the web UI under a URL prefix (ASGI root_path)."""

from __future__ import annotations

from httpx import AsyncClient


async def test_links_are_prefixed(web_client_with_root_path: AsyncClient) -> None:
    resp = await web_client_with_root_path.get("/")
    assert resp.status_code == 200
    assert 'href="/grimoire/static/favicon.ico"' in resp.text
    assert 'href="/grimoire/backlog"' in resp.text
    assert 'const BASE_PATH = "/grimoire";' in resp.text


async def test_partial_links_are_prefixed(web_client_with_root_path: AsyncClient) -> None:
    resp = await web_client_with_root_path.get("/partials/dashboard-list?sort=name&dir=asc")
    assert 'href="/grimoire/repo/acme/api"' in resp.text


async def test_unstripped_prefix_is_routed(web_client_with_root_path: AsyncClient) -> None:
    """Ingresses that forward the prefix unchanged must still reach the routes."""
    resp = await web_client_with_root_path.get("/grimoire/partials/dashboard-list")
    assert resp.status_code == 200
    assert 'href="/grimoire/repo/acme/api"' in resp.text


async def test_loading_redirect_is_prefixed(web_client_with_root_path: AsyncClient) -> None:
    resp = await web_client_with_root_path.get("/partials/loading-status")
    assert resp.headers["HX-Redirect"] == "/grimoire/"


async def test_no_prefix_by_default(web_client: AsyncClient) -> None:
    resp = await web_client.get("/")
    assert 'href="/static/favicon.ico"' in resp.text
    assert 'const BASE_PATH = "";' in resp.text
