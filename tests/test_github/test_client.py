"""Tests for the GitHubClient."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncEngine

from grimoire.database import get_engine, upgrade_database
from grimoire.github.client import (
    GitHubAPIError,
    GitHubClient,
    NotFoundError,
    PRRollup,
    RateLimitError,
)


@pytest.fixture
async def engine(tmp_path) -> AsyncEngine:
    db_path = str(tmp_path / "test.db")
    eng = await get_engine(db_path)
    await upgrade_database(eng)
    return eng


@pytest.fixture
async def client(engine: AsyncEngine) -> GitHubClient:
    c = GitHubClient(token="ghp_test", engine=engine, backoff_factors=(0.0, 0.0, 0.0))
    yield c  # type: ignore[misc]
    await c.close()


# ---------------------------------------------------------------------------
# Basic requests
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_repo(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(
            200,
            json={"full_name": "owner/repo", "default_branch": "main"},
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    data = await client.get_repo("owner/repo")
    assert data is not None
    assert data["full_name"] == "owner/repo"


@respx.mock
async def test_get_open_issues_filters_prs(client: GitHubClient) -> None:
    """Issues with a `pull_request` key should be excluded."""
    respx.get("https://api.github.com/repos/owner/repo/issues").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"number": 1, "title": "Bug"},
                {"number": 2, "title": "Feature PR", "pull_request": {"url": "..."}},
                {"number": 3, "title": "Another bug"},
            ],
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    issues = await client.get_open_issues("owner/repo")
    assert issues is not None
    assert len(issues) == 2
    assert all("pull_request" not in i for i in issues)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


@respx.mock
async def test_pagination(client: GitHubClient) -> None:
    page2_url = "https://api.github.com/repos/owner/repo/issues?state=open&per_page=100&page=2"
    route = respx.get("https://api.github.com/repos/owner/repo/issues")
    route.side_effect = [
        httpx.Response(
            200,
            json=[{"number": 1, "title": "Issue 1"}],
            headers={
                "Link": f'<{page2_url}>; rel="next"',
                "X-RateLimit-Remaining": "4999",
                "X-RateLimit-Limit": "5000",
            },
        ),
        httpx.Response(
            200,
            json=[{"number": 2, "title": "Issue 2"}],
            headers={
                "X-RateLimit-Remaining": "4998",
                "X-RateLimit-Limit": "5000",
            },
        ),
    ]
    issues = await client.get_open_issues("owner/repo")
    assert issues is not None
    assert len(issues) == 2


# ---------------------------------------------------------------------------
# ETag caching
# ---------------------------------------------------------------------------


@respx.mock
async def test_etag_caching(client: GitHubClient) -> None:
    route = respx.get("https://api.github.com/repos/owner/repo")

    # First call — returns data + ETag
    route.side_effect = [
        httpx.Response(
            200,
            json={"full_name": "owner/repo"},
            headers={
                "ETag": '"abc123"',
                "X-RateLimit-Remaining": "4999",
                "X-RateLimit-Limit": "5000",
            },
        ),
        httpx.Response(
            304,
            headers={
                "X-RateLimit-Remaining": "4998",
                "X-RateLimit-Limit": "5000",
            },
        ),
    ]

    data = await client.get_repo("owner/repo")
    assert data is not None
    assert data["full_name"] == "owner/repo"

    # Second call should send If-None-Match and get 304 → None
    data2 = await client.get_repo("owner/repo")
    assert data2 is None

    # Verify the second request included the ETag header
    assert route.call_count == 2
    second_request = route.calls[1].request
    assert second_request.headers.get("If-None-Match") == '"abc123"'


# ---------------------------------------------------------------------------
# Rate limit tracking
# ---------------------------------------------------------------------------


@respx.mock
async def test_rate_limit_tracking(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(
            200,
            json={"full_name": "owner/repo"},
            headers={
                "X-RateLimit-Remaining": "4200",
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Reset": "1700000000",
            },
        )
    )
    await client.get_repo("owner/repo")
    assert client.rate_limit_remaining == 4200
    assert client.rate_limit_reset == 1700000000.0
    assert not client.is_rate_limited
    assert not client.is_degraded


@respx.mock
async def test_rate_limit_degraded(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(
            200,
            json={"full_name": "owner/repo"},
            headers={
                "X-RateLimit-Remaining": "100",
                "X-RateLimit-Limit": "5000",
            },
        )
    )
    await client.get_repo("owner/repo")
    assert client.is_degraded  # 100 < 500 (10% of 5000)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@respx.mock
async def test_404_raises_not_found(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/owner/missing").mock(
        return_value=httpx.Response(
            404,
            json={"message": "Not Found"},
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    with pytest.raises(NotFoundError):
        await client.get_repo("owner/missing")


@respx.mock
async def test_5xx_retries(client: GitHubClient) -> None:
    """Transient 500s should be retried up to 3 times."""
    route = respx.get("https://api.github.com/repos/owner/repo")
    route.side_effect = [
        httpx.Response(
            500,
            text="Internal Server Error",
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        ),
        httpx.Response(
            500,
            text="Internal Server Error",
            headers={"X-RateLimit-Remaining": "4998", "X-RateLimit-Limit": "5000"},
        ),
        httpx.Response(
            200,
            json={"full_name": "owner/repo"},
            headers={"X-RateLimit-Remaining": "4997", "X-RateLimit-Limit": "5000"},
        ),
    ]
    data = await client.get_repo("owner/repo")
    assert data is not None
    assert route.call_count == 3


@respx.mock
async def test_5xx_exhausts_retries(client: GitHubClient) -> None:
    """After 3 failed attempts, should raise GitHubAPIError."""
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(
            500,
            text="Internal Server Error",
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    with pytest.raises(GitHubAPIError):
        await client.get_repo("owner/repo")


@respx.mock
async def test_get_default_branch(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(
            200,
            json={"full_name": "owner/repo", "default_branch": "develop"},
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    branch = await client.get_default_branch("owner/repo")
    assert branch == "develop"


@respx.mock
async def test_get_workflows(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/owner/repo/actions/workflows").mock(
        return_value=httpx.Response(
            200,
            json={"total_count": 1, "workflows": [{"id": 1, "name": "CI"}]},
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    workflows = await client.get_workflows("owner/repo")
    assert workflows is not None
    assert len(workflows) == 1
    assert workflows[0]["name"] == "CI"


@respx.mock
async def test_get_workflow_runs(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/owner/repo/actions/workflows/1/runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflow_runs": [{"id": 100, "conclusion": "success", "html_url": "https://..."}],
            },
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    runs = await client.get_workflow_runs("owner/repo", 1, "main")
    assert runs is not None
    assert len(runs) == 1
    assert runs[0]["conclusion"] == "success"


@respx.mock
async def test_get_workflow_runs_without_branch(client: GitHubClient) -> None:
    """When branch is None, the request omits the branch query param."""
    route = respx.get("https://api.github.com/repos/owner/repo/actions/workflows/1/runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflow_runs": [{"id": 100, "conclusion": "success", "head_branch": "v1.2.3"}],
            },
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    runs = await client.get_workflow_runs("owner/repo", 1)
    assert runs is not None
    assert runs[0]["head_branch"] == "v1.2.3"
    assert "branch" not in route.calls[0].request.url.params


@respx.mock
async def test_get_workflow_runs_no_etag_caching(client: GitHubClient) -> None:
    """Workflow runs must not use ETag caching so status transitions are always visible."""
    route = respx.get("https://api.github.com/repos/owner/repo/actions/workflows/1/runs")

    # First call returns in_progress run with an ETag header
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflow_runs": [{"id": 200, "conclusion": None, "html_url": "https://..."}],
            },
            headers={
                "X-RateLimit-Remaining": "4999",
                "X-RateLimit-Limit": "5000",
                "ETag": '"etag-abc"',
            },
        ),
        # Second call returns the same run now completed — must NOT be 304
        httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflow_runs": [{"id": 200, "conclusion": "success", "html_url": "https://..."}],
            },
            headers={
                "X-RateLimit-Remaining": "4998",
                "X-RateLimit-Limit": "5000",
            },
        ),
    ]

    # First fetch
    runs = await client.get_workflow_runs("owner/repo", 1, "main")
    assert runs is not None
    assert runs[0]["conclusion"] is None

    # Second fetch must return fresh data (not None from ETag 304)
    runs = await client.get_workflow_runs("owner/repo", 1, "main")
    assert runs is not None
    assert runs[0]["conclusion"] == "success"

    # Verify no If-None-Match header was sent on either request
    for call in route.calls:
        assert "If-None-Match" not in call.request.headers


# ---------------------------------------------------------------------------
# Branch methods
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_branches(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/owner/repo/branches").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "main", "commit": {"sha": "abc"}},
                {"name": "develop", "commit": {"sha": "def"}},
            ],
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    branches = await client.get_branches("owner/repo")
    assert branches is not None
    assert len(branches) == 2
    assert branches[0]["name"] == "main"


@respx.mock
async def test_get_branch(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/owner/repo/branches/main").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "main",
                "commit": {
                    "sha": "abc",
                    "commit": {
                        "committer": {"date": "2026-04-10T12:00:00Z"},
                    },
                },
            },
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    data = await client.get_branch("owner/repo", "main")
    assert data is not None
    assert data["name"] == "main"
    assert data["commit"]["commit"]["committer"]["date"] == "2026-04-10T12:00:00Z"


@respx.mock
async def test_get_branch_304(client: GitHubClient) -> None:
    """A 304 response should return None (cache hit)."""
    respx.get("https://api.github.com/repos/owner/repo/branches/main").mock(
        return_value=httpx.Response(
            304,
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    data = await client.get_branch("owner/repo", "main")
    assert data is None


@respx.mock
async def test_get_git_commit(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/owner/repo/git/commits/abc123").mock(
        return_value=httpx.Response(
            200,
            json={
                "sha": "abc123",
                "committer": {"date": "2026-04-10T12:00:00Z"},
            },
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )
    data = await client.get_git_commit("owner/repo", "abc123")
    assert data is not None
    assert data["committer"]["date"] == "2026-04-10T12:00:00Z"


# ---------------------------------------------------------------------------
# API request metrics
# ---------------------------------------------------------------------------


def test_normalize_endpoint_repos() -> None:
    """Dynamic owner/repo segments are normalized."""
    assert (
        GitHubClient._normalize_endpoint("/repos/my-org/my-repo/issues")
        == "/repos/{owner}/{repo}/issues"
    )


def test_normalize_endpoint_workflow_runs() -> None:
    """Workflow and run IDs are normalized."""
    path = "/repos/org/repo/actions/workflows/12345/runs"
    assert (
        GitHubClient._normalize_endpoint(path)
        == "/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs"
    )


def test_normalize_endpoint_git_commit() -> None:
    """Git commit SHA is normalized."""
    path = "/repos/org/repo/git/commits/abc123def456"
    assert GitHubClient._normalize_endpoint(path) == "/repos/{owner}/{repo}/git/commits/{sha}"


def test_normalize_endpoint_branch() -> None:
    """Branch names are normalized."""
    path = "/repos/org/repo/branches/feature/my-branch"
    assert GitHubClient._normalize_endpoint(path) == "/repos/{owner}/{repo}/branches/{branch}"


def test_normalize_endpoint_team_repos() -> None:
    """Team slugs are normalized."""
    path = "/orgs/my-org/teams/backend-team/repos"
    assert GitHubClient._normalize_endpoint(path) == "/orgs/my-org/teams/{team_slug}/repos"


def test_normalize_endpoint_full_url() -> None:
    """Full GitHub URLs (from pagination) are normalized."""
    url = "https://api.github.com/repos/org/repo/issues?page=2"
    assert GitHubClient._normalize_endpoint(url) == "/repos/{owner}/{repo}/issues"


@respx.mock
async def test_api_requests_metric_incremented(client: GitHubClient) -> None:
    """API_REQUESTS counter is incremented on each request."""
    from grimoire.observability.metrics import API_REQUESTS

    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(
            200,
            json={"full_name": "owner/repo"},
            headers={"X-RateLimit-Remaining": "4999", "X-RateLimit-Limit": "5000"},
        )
    )

    before = API_REQUESTS.labels(endpoint="/repos/{owner}/{repo}", status="200")._value.get()
    await client.get_repo("owner/repo")
    after = API_REQUESTS.labels(endpoint="/repos/{owner}/{repo}", status="200")._value.get()

    assert after == before + 1


# ---------------------------------------------------------------------------
# GraphQL pull request rollups
# ---------------------------------------------------------------------------

_GRAPHQL_URL = "https://api.github.com/graphql"
_GRAPHQL_HEADERS = {
    "X-RateLimit-Resource": "graphql",
    "X-RateLimit-Limit": "5000",
    "X-RateLimit-Remaining": "4975",
    "X-RateLimit-Reset": "1793999084",
}


def _pr(state: str | None, review: str | None = None, merge: str | None = "BLOCKED") -> dict:
    return {
        "reviewDecision": review,
        "mergeStateStatus": merge,
        "statusCheckRollup": None if state is None else {"state": state},
    }


@respx.mock
async def test_rollups_one_request_for_all_pull_requests(client: GitHubClient) -> None:
    route = respx.post(_GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "r0": {"p1": _pr("SUCCESS", "APPROVED", "CLEAN"), "p2": _pr("FAILURE")},
                    "r1": {"p7": _pr(None, "REVIEW_REQUIRED", "BEHIND")},
                    "rateLimit": {"cost": 1, "remaining": 4975},
                }
            },
            headers=_GRAPHQL_HEADERS,
        )
    )

    rollups = await client.get_pull_request_rollups(
        [("acme/api", 1), ("acme/api", 2), ("acme/web", 7)]
    )

    assert rollups == {
        ("acme/api", 1): PRRollup("success", "approved", "clean"),
        ("acme/api", 2): PRRollup("failure", None, "blocked"),
        ("acme/web", 7): PRRollup("none", "review_required", "behind"),
    }
    assert route.call_count == 1
    request = route.calls[0].request
    assert "If-None-Match" not in request.headers
    query = json.loads(request.content)["query"]
    assert (
        'r0: repository(owner: "acme", name: "api") { '
        "p1: pullRequest(number: 1) { reviewDecision mergeStateStatus statusCheckRollup { state } } "
        "p2: pullRequest(number: 2) {"
    ) in query
    assert 'r1: repository(owner: "acme", name: "web")' in query


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("SUCCESS", "success"),
        ("FAILURE", "failure"),
        ("ERROR", "failure"),
        ("PENDING", "pending"),
        ("EXPECTED", "pending"),
        (None, "none"),
    ],
)
@respx.mock
async def test_rollup_ci_state_mapping(
    client: GitHubClient, state: str | None, expected: str
) -> None:
    respx.post(_GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200, json={"data": {"r0": {"p1": _pr(state)}}}, headers=_GRAPHQL_HEADERS
        )
    )
    rollups = await client.get_pull_request_rollups([("acme/api", 1)])
    assert rollups[("acme/api", 1)].ci_state == expected


@respx.mock
async def test_rollups_skip_missing_repositories_and_pull_requests(client: GitHubClient) -> None:
    """GitHub answers 200 with NOT_FOUND errors and null aliases (real response shape)."""
    respx.post(_GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {"r0": {"p1": _pr("SUCCESS"), "p9": None}, "r1": None},
                "errors": [
                    {
                        "type": "NOT_FOUND",
                        "path": ["r0", "p9"],
                        "message": "Could not resolve to a PullRequest with the number of 9.",
                    },
                    {
                        "type": "NOT_FOUND",
                        "path": ["r1"],
                        "message": "Could not resolve to a Repository with the name 'acme/gone'.",
                    },
                ],
            },
            headers=_GRAPHQL_HEADERS,
        )
    )

    rollups = await client.get_pull_request_rollups(
        [("acme/api", 1), ("acme/api", 9), ("acme/gone", 1)]
    )

    assert list(rollups) == [("acme/api", 1)]


@respx.mock
async def test_graphql_response_does_not_touch_the_rest_rate_limit(client: GitHubClient) -> None:
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(
            200,
            json={"full_name": "owner/repo"},
            headers={
                "X-RateLimit-Resource": "core",
                "X-RateLimit-Remaining": "4200",
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Reset": "1700000000",
            },
        )
    )
    await client.get_repo("owner/repo")
    respx.post(_GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": {"r0": {"p1": _pr("SUCCESS")}}},
            headers={**_GRAPHQL_HEADERS, "X-RateLimit-Remaining": "3"},
        )
    )

    await client.get_pull_request_rollups([("acme/api", 1)])

    assert client.rate_limit_remaining == 4200
    assert client.rate_limit_reset == 1700000000.0
    assert not client.is_degraded


@respx.mock
async def test_rollups_rate_limited_error(client: GitHubClient) -> None:
    respx.post(_GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": None,
                "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}],
            },
            headers=_GRAPHQL_HEADERS,
        )
    )
    with pytest.raises(RateLimitError):
        await client.get_pull_request_rollups([("acme/api", 1)])


@respx.mock
async def test_rollups_other_errors_raise(client: GitHubClient) -> None:
    respx.post(_GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {"r0": None},
                "errors": [{"type": "FORBIDDEN", "path": ["r0"], "message": "SAML enforcement"}],
            },
            headers=_GRAPHQL_HEADERS,
        )
    )
    with pytest.raises(GitHubAPIError, match="SAML enforcement"):
        await client.get_pull_request_rollups([("acme/api", 1)])


@respx.mock
async def test_rollups_request_is_recorded_in_metrics(client: GitHubClient) -> None:
    from grimoire.observability.metrics import API_REQUESTS

    respx.post(_GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200, json={"data": {"r0": {"p1": _pr("SUCCESS")}}}, headers=_GRAPHQL_HEADERS
        )
    )
    before = API_REQUESTS.labels(endpoint="/graphql", status="200")._value.get()
    await client.get_pull_request_rollups([("acme/api", 1)])
    assert API_REQUESTS.labels(endpoint="/graphql", status="200")._value.get() == before + 1
