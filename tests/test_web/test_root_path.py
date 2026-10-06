"""Tests for serving the web UI under a URL prefix."""

from __future__ import annotations

import re

from httpx import ASGITransport, AsyncClient

from grimoire.app import create_app
from tests.test_web.conftest import _populate_cache


async def test_no_prefix_by_default(web_client: AsyncClient) -> None:
    html = (await web_client.get("/")).text
    assert 'href="/backlog"' in html
    assert 'const BASE_PATH = "";' in html


async def test_prefix_applied_to_links_and_scripts() -> None:
    _populate_cache()
    app = create_app()
    transport = ASGITransport(app=app, root_path="/proj")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        html = (await client.get("/")).text
    assert 'href="/proj/backlog"' in html
    assert 'src="/proj/static/logo-nav.png"' in html
    assert 'const BASE_PATH = "/proj";' in html
    assert not re.search(r'(href|src|hx-get|hx-post)="/(?!proj)[a-z]', html)


async def test_partials_are_prefixed() -> None:
    _populate_cache()
    app = create_app()
    transport = ASGITransport(app=app, root_path="/proj")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        html = (await client.get("/partials/dashboard-matrix")).text
    assert 'hx-get="/proj/partials/dashboard-matrix' in html
    assert 'href="/proj/repo/acme/api"' in html
