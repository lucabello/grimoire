"""Tests for the Maintenance page."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from httpx import AsyncClient

from grimoire.config import MaintenanceConfig, MaintenanceGroup
from grimoire.maintenance import get_maintenance_config, set_maintenance_config


@pytest.fixture
def use_groups() -> Iterator[Callable[..., None]]:
    """Register groups for the test and restore the previous config afterwards."""
    saved = get_maintenance_config()

    def use(*groups: MaintenanceGroup) -> None:
        set_maintenance_config(MaintenanceConfig(groups=list(groups)))

    set_maintenance_config(MaintenanceConfig())
    yield use
    set_maintenance_config(saved)


async def test_empty_state_explains_how_to_configure(
    web_client: AsyncClient, use_groups: Callable[..., None]
) -> None:
    resp = await web_client.get("/maintenance")
    assert resp.status_code == 200
    assert "No maintenance groups configured" in resp.text
    assert "maintenance.groups" in resp.text
    assert "Open on GitHub" not in resp.text


async def test_lists_each_group_with_matchers_query_and_link(
    web_client: AsyncClient, use_groups: Callable[..., None]
) -> None:
    use_groups(
        MaintenanceGroup(name="renovate", authors=["app/renovate"], status="failure"),
        MaintenanceGroup(name="docs", labels=["documentation"], repos=["acme/api"]),
    )

    resp = await web_client.get("/maintenance")

    assert resp.status_code == 200
    assert "renovate" in resp.text and "docs" in resp.text
    assert "app/renovate" in resp.text
    assert "failure" in resp.text
    assert "all tracked repositories" in resp.text
    assert "acme/api" in resp.text
    assert "(repo:acme/api OR repo:acme/frontend) author:app/renovate status:failure" in resp.text
    assert resp.text.count("Open on GitHub") == 2


async def test_links_open_github_in_a_new_tab(
    web_client: AsyncClient, use_groups: Callable[..., None]
) -> None:
    use_groups(MaintenanceGroup(name="docs", labels=["documentation"]))

    resp = await web_client.get("/maintenance")

    href = (
        "https://github.com/pulls?q=is%3Apr+is%3Aopen+%28repo%3Aacme%2Fapi+OR+repo%3Aacme%2Ffrontend"
        "%29+label%3Adocumentation"
    )
    assert f'href="{href}" target="_blank"' in resp.text


async def test_page_warns_that_multi_value_links_are_unverified(
    web_client: AsyncClient, use_groups: Callable[..., None]
) -> None:
    use_groups(MaintenanceGroup(name="docs", labels=["documentation"]))

    resp = await web_client.get("/maintenance")

    assert "Unverified: github.com may not apply the parenthesised" in resp.text


async def test_group_without_tracked_repos_says_so(
    web_client: AsyncClient, use_groups: Callable[..., None]
) -> None:
    use_groups(MaintenanceGroup(name="ghost", labels=["x"], repos=["nobody/*"]))

    resp = await web_client.get("/maintenance")

    assert "ghost" in resp.text
    assert "No tracked repository is in scope" in resp.text
    assert "Open on GitHub" not in resp.text


async def test_nav_links_to_the_page(web_client: AsyncClient) -> None:
    resp = await web_client.get("/")
    assert 'href="/maintenance"' in resp.text and "Maintenance</a>" in resp.text


async def test_nav_link_is_prefixed_under_root_path(
    web_client_with_root_path: AsyncClient,
) -> None:
    resp = await web_client_with_root_path.get("/maintenance")
    assert resp.status_code == 200
    assert 'href="/grimoire/maintenance"' in resp.text
