"""Tests for rejecting cross-site state-changing requests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from starlette.datastructures import Headers

from grimoire.security import is_cross_site

TOGGLE = "/api/checks/watchdog/toggle"
CROSS_SITE = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}

# Every unauthenticated state-changing route (the slug does not need to exist: the guard runs first)
POST_ROUTES = [
    "/api/checks/watchdog/run",
    "/api/checks/watchdog/toggle",
    "/api/actions/anything/run",
    "/api/actions/anything/toggle",
    "/partials/check-run/watchdog",
    "/partials/action-run/anything",
    "/partials/refresh-trigger",
    "/api/backlog/save-weights",
    "/api/refresh",
]


async def _enabled(client: AsyncClient) -> bool:
    checks = (await client.get("/api/checks/")).json()
    return next(c["enabled"] for c in checks if c["slug"] == "watchdog")


class TestRejected:
    async def test_cross_site_post_is_rejected_and_changes_nothing(
        self, web_client_with_checks: AsyncClient
    ) -> None:
        before = await _enabled(web_client_with_checks)
        resp = await web_client_with_checks.post(TOGGLE, headers=CROSS_SITE)
        assert resp.status_code == 403
        assert await _enabled(web_client_with_checks) == before

    @pytest.mark.parametrize("path", POST_ROUTES)
    async def test_every_state_changing_route_is_guarded(
        self, web_client: AsyncClient, path: str
    ) -> None:
        resp = await web_client.post(path, headers=CROSS_SITE, json={})
        assert resp.status_code == 403

    @pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
    async def test_other_unsafe_methods_are_guarded(
        self, web_client: AsyncClient, method: str
    ) -> None:
        resp = await web_client.request(method, "/api/refresh", headers=CROSS_SITE)
        assert resp.status_code == 403

    @pytest.mark.parametrize("site", ["cross-site", "same-site", "Cross-Site"])
    async def test_sec_fetch_site_other_than_same_origin_or_none(
        self, web_client_with_checks: AsyncClient, site: str
    ) -> None:
        resp = await web_client_with_checks.post(TOGGLE, headers={"Sec-Fetch-Site": site})
        assert resp.status_code == 403

    async def test_foreign_origin_without_sec_fetch_site(
        self, web_client_with_checks: AsyncClient
    ) -> None:
        resp = await web_client_with_checks.post(
            TOGGLE, headers={"Origin": "https://evil.example"}
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize("origin", ["null", "http://test:9999", "https://test.evil.example"])
    async def test_origin_that_is_not_the_request_host(
        self, web_client_with_checks: AsyncClient, origin: str
    ) -> None:
        resp = await web_client_with_checks.post(TOGGLE, headers={"Origin": origin})
        assert resp.status_code == 403

    async def test_sec_fetch_site_wins_over_a_matching_origin(
        self, web_client_with_checks: AsyncClient
    ) -> None:
        headers = {"Origin": "http://test", "Sec-Fetch-Site": "cross-site"}
        assert (await web_client_with_checks.post(TOGGLE, headers=headers)).status_code == 403

    async def test_x_forwarded_host_is_not_trusted(
        self, web_client_with_checks: AsyncClient
    ) -> None:
        headers = {"Origin": "https://evil.example", "X-Forwarded-Host": "evil.example"}
        assert (await web_client_with_checks.post(TOGGLE, headers=headers)).status_code == 403


class TestAllowed:
    async def test_same_origin_post(self, web_client_with_checks: AsyncClient) -> None:
        before = await _enabled(web_client_with_checks)
        resp = await web_client_with_checks.post(TOGGLE, headers={"Sec-Fetch-Site": "same-origin"})
        assert resp.status_code == 200
        assert await _enabled(web_client_with_checks) != before

    async def test_user_initiated_request(self, web_client_with_checks: AsyncClient) -> None:
        resp = await web_client_with_checks.post(TOGGLE, headers={"Sec-Fetch-Site": "none"})
        assert resp.status_code == 200

    async def test_same_origin_by_origin_header_when_sec_fetch_site_is_absent(
        self, web_client_with_checks: AsyncClient
    ) -> None:
        resp = await web_client_with_checks.post(TOGGLE, headers={"Origin": "http://test"})
        assert resp.status_code == 200

    async def test_header_less_post_from_scripts_and_curl(
        self, web_client_with_checks: AsyncClient
    ) -> None:
        assert (await web_client_with_checks.post(TOGGLE)).status_code == 200

    async def test_htmx_request_of_the_app_itself(self, web_client: AsyncClient) -> None:
        headers = {"Sec-Fetch-Site": "same-origin", "HX-Request": "true", "Origin": "http://test"}
        resp = await web_client.post("/partials/refresh-trigger", headers=headers)
        assert resp.status_code != 403

    async def test_get_is_never_blocked(self, web_client: AsyncClient) -> None:
        for path in ("/", "/api/repos/", "/health"):
            assert (await web_client.get(path, headers=CROSS_SITE)).status_code == 200


class TestIsCrossSite:
    @pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
    def test_safe_methods(self, method: str) -> None:
        assert not is_cross_site(method, Headers({"sec-fetch-site": "cross-site"}))

    def test_origin_comparison_ignores_case_and_scheme(self) -> None:
        headers = Headers({"origin": "HTTPS://Grimoire.Example", "host": "grimoire.example"})
        assert not is_cross_site("POST", headers)

    def test_origin_with_port(self) -> None:
        same = Headers({"origin": "http://localhost:8000", "host": "localhost:8000"})
        other = Headers({"origin": "http://localhost:8001", "host": "localhost:8000"})
        assert not is_cross_site("POST", same)
        assert is_cross_site("POST", other)
