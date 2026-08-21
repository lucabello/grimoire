"""Async GitHub API client with ETag caching, rate-limit tracking, and pagination."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from grimoire.database import CachedETag
from grimoire.observability.metrics import API_REQUESTS, update_rate_limit_metrics

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GitHubAPIError(Exception):
    """Generic GitHub API error."""

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


class RateLimitError(GitHubAPIError):
    """Raised when the GitHub rate limit is exhausted."""


class NotFoundError(GitHubAPIError):
    """Raised for 404 responses."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://api.github.com"
_MAX_RETRIES = 3
_DEFAULT_BACKOFF_FACTORS = (1.0, 2.0, 4.0)
_TRANSIENT_STATUS_CODES = frozenset(range(500, 600))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GitHubClient:
    """Thin async wrapper around the GitHub REST API."""

    def __init__(
        self,
        token: str,
        engine: AsyncEngine,
        *,
        backoff_factors: tuple[float, ...] = _DEFAULT_BACKOFF_FACTORS,
    ) -> None:
        self._engine = engine
        self._http = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )
        self._backoff_factors = backoff_factors
        self._rate_limit_remaining: int | None = None
        self._rate_limit_limit: int | None = None
        self._rate_limit_reset: float | None = None

    # -- public properties ---------------------------------------------------

    @property
    def rate_limit_remaining(self) -> int | None:
        return self._rate_limit_remaining

    @property
    def rate_limit_reset(self) -> float | None:
        return self._rate_limit_reset

    @property
    def is_rate_limited(self) -> bool:
        return self._rate_limit_remaining is not None and self._rate_limit_remaining == 0

    @property
    def is_degraded(self) -> bool:
        if self._rate_limit_remaining is None or self._rate_limit_limit is None:
            return False
        if self._rate_limit_limit == 0:
            return True
        return self._rate_limit_remaining < self._rate_limit_limit * 0.1

    # -- lifecycle -----------------------------------------------------------

    async def close(self) -> None:
        await self._http.aclose()

    # -- public API methods --------------------------------------------------

    async def get_team_repos(self, org: str, team_slug: str) -> list[dict[str, Any]]:
        """Return all repos visible to *org/team_slug*."""
        result = await self._paginated_get(
            f"/orgs/{org}/teams/{team_slug}/repos",
        )
        return result if result is not None else []

    async def get_repo(self, full_name: str) -> dict[str, Any] | None:
        owner, repo = full_name.split("/", 1)
        return await self._request("GET", f"/repos/{owner}/{repo}")

    async def get_open_issues(self, full_name: str) -> list[dict[str, Any]] | None:
        """Return open issues, filtering OUT pull requests."""
        owner, repo = full_name.split("/", 1)
        items = await self._paginated_get(
            f"/repos/{owner}/{repo}/issues",
            params={"state": "open"},
        )
        if items is None:
            return None
        return [i for i in items if "pull_request" not in i]

    async def get_open_pull_requests(self, full_name: str) -> list[dict[str, Any]] | None:
        owner, repo = full_name.split("/", 1)
        return await self._paginated_get(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "open"},
        )

    async def get_workflows(self, full_name: str) -> list[dict[str, Any]] | None:
        owner, repo = full_name.split("/", 1)
        # Skip ETag caching: the workflow *list* rarely changes, but caching it
        # prevents us from fetching updated *run* statuses for individual
        # workflows (we need the IDs from this response to call get_workflow_runs).
        data = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/actions/workflows",
            params={"per_page": 100},
            use_etag=False,
        )
        if data is None:
            return None
        return data.get("workflows", [])

    async def get_workflow_runs(
        self, full_name: str, workflow_id: int, branch: str | None = None
    ) -> list[dict[str, Any]] | None:
        """Return the latest run(s) for a workflow.

        If *branch* is given, filter to runs whose ``head_branch`` matches it
        (GitHub's server-side filter). If *branch* is ``None``, return the
        single latest run regardless of branch — needed for workflows
        triggered by non-branch events (e.g. ``release``, tag pushes),
        whose ``head_branch`` never matches a tracked branch like ``main``.
        """
        owner, repo = full_name.split("/", 1)
        params: dict[str, Any] = {"per_page": 1}
        if branch is not None:
            params["branch"] = branch
        # Skip ETag caching: a run's conclusion/status can change without the
        # run list identity changing (same run ID, updated fields).  ETag-based
        # 304 responses would hide status transitions (e.g. in_progress → success).
        # Since we fetch only 1 result (per_page=1), the bandwidth cost is negligible.
        data = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs",
            params=params,
            use_etag=False,
        )
        if data is None:
            return None
        return data.get("workflow_runs", [])

    async def get_default_branch(self, full_name: str) -> str:
        data = await self.get_repo(full_name)
        if data is None:
            return "main"
        return data.get("default_branch", "main")

    async def get_branches(self, full_name: str) -> list[dict[str, Any]] | None:
        """Return all branches for a repo (paginated)."""
        owner, repo = full_name.split("/", 1)
        return await self._paginated_get(f"/repos/{owner}/{repo}/branches")

    async def get_branch(self, full_name: str, branch: str) -> dict[str, Any] | None:
        """Return metadata for a single branch, including latest commit info."""
        owner, repo = full_name.split("/", 1)
        return await self._request("GET", f"/repos/{owner}/{repo}/branches/{branch}")

    async def get_git_commit(self, full_name: str, sha: str) -> dict[str, Any] | None:
        """Return a Git commit object (lightweight, includes committer date)."""
        owner, repo = full_name.split("/", 1)
        return await self._request("GET", f"/repos/{owner}/{repo}/git/commits/{sha}")

    # -- internal helpers ----------------------------------------------------

    async def _paginated_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]] | None:
        """Follow ``Link: rel="next"`` headers to collect all pages."""
        params = dict(params) if params else {}
        params.setdefault("per_page", 100)

        all_items: list[dict[str, Any]] = []
        current_path: str | None = path
        current_params: dict[str, Any] | None = params

        while current_path is not None:
            data = await self._request("GET", current_path, params=current_params)
            if data is None:
                # 304 on the *first* page → return None (cache hit)
                if not all_items:
                    return None
                break

            if isinstance(data, list):
                all_items.extend(data)
            else:
                # Some endpoints wrap in an object
                all_items.extend(data.get("items", data.get("workflow_runs", [])))

            # Parse Link header for next page URL
            next_url = self._last_next_link
            if next_url:
                current_path = next_url
                current_params = None  # params are embedded in the next URL
            else:
                current_path = None

        return all_items

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        use_etag: bool = True,
    ) -> Any:
        """Execute a single API request with ETag caching, retries, and rate-limit tracking."""
        # Build the URL for ETag keying (path + sorted params)
        url_key = self._build_url_key(path, params)

        # Load ETag from DB
        etag_header: str | None = None
        last_modified_header: str | None = None
        if use_etag:
            etag_header, last_modified_header = await self._load_etag(url_key)

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                headers: dict[str, str] = {}
                if etag_header:
                    headers["If-None-Match"] = etag_header
                if last_modified_header:
                    headers["If-Modified-Since"] = last_modified_header

                # If path is a full URL (from pagination), use it directly
                if path.startswith("https://"):
                    response = await self._http.request(
                        method, path, headers=headers, params=params
                    )
                else:
                    response = await self._http.request(
                        method, path, headers=headers, params=params
                    )

                self._update_rate_limit(response)
                self._last_next_link = self._parse_next_link(response)
                self._record_api_request(path, response.status_code)

                if response.status_code == 304:
                    return None

                if response.status_code == 404:
                    raise NotFoundError(f"Not found: {path}", status_code=404)

                if response.status_code == 403:
                    if self.is_rate_limited:
                        raise RateLimitError("GitHub API rate limit exceeded", status_code=403)
                    raise GitHubAPIError(f"Forbidden: {response.text}", status_code=403)

                if response.status_code in _TRANSIENT_STATUS_CODES:
                    raise GitHubAPIError(
                        f"Server error {response.status_code}: {response.text}",
                        status_code=response.status_code,
                    )

                response.raise_for_status()

                # Store ETag / Last-Modified
                new_etag = response.headers.get("ETag", "")
                new_last_modified = response.headers.get("Last-Modified", "")
                if new_etag or new_last_modified:
                    await self._save_etag(url_key, new_etag, new_last_modified)

                return response.json()

            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(self._backoff_factors[attempt])
                    continue
                raise GitHubAPIError(
                    f"Request failed after {_MAX_RETRIES} retries: {exc}"
                ) from exc

            except GitHubAPIError as exc:
                if exc.status_code in _TRANSIENT_STATUS_CODES and attempt < _MAX_RETRIES - 1:
                    last_exc = exc
                    await asyncio.sleep(self._backoff_factors[attempt])
                    continue
                raise

        raise GitHubAPIError(  # pragma: no cover
            f"Request failed after {_MAX_RETRIES} retries: {last_exc}"
        )

    # -- ETag persistence ----------------------------------------------------

    async def _load_etag(self, url_key: str) -> tuple[str | None, str | None]:
        async with AsyncSession(self._engine) as session:
            result = await session.exec(
                select(CachedETag).where(CachedETag.endpoint_url == url_key)
            )
            record = result.first()
            if record is None:
                return None, None
            return record.etag or None, record.last_modified or None

    async def _save_etag(self, url_key: str, etag: str, last_modified: str) -> None:
        async with AsyncSession(self._engine) as session:
            stmt = (
                sqlite_insert(CachedETag)
                .values(endpoint_url=url_key, etag=etag, last_modified=last_modified)
                .on_conflict_do_update(
                    index_elements=["endpoint_url"],
                    set_={"etag": etag, "last_modified": last_modified},
                )
            )
            await session.execute(stmt)
            await session.commit()

    # -- rate limit ----------------------------------------------------------

    def _update_rate_limit(self, response: httpx.Response) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining")
        limit = response.headers.get("X-RateLimit-Limit")
        reset = response.headers.get("X-RateLimit-Reset")
        if remaining is not None:
            self._rate_limit_remaining = int(remaining)
        if limit is not None:
            self._rate_limit_limit = int(limit)
        if reset is not None:
            self._rate_limit_reset = float(reset)
        if self._rate_limit_remaining is not None and self._rate_limit_reset is not None:
            update_rate_limit_metrics(self._rate_limit_remaining, int(self._rate_limit_reset))

    # -- API request metrics -------------------------------------------------

    @staticmethod
    def _normalize_endpoint(path: str) -> str:
        """Normalize API path to a low-cardinality endpoint label.

        Replaces dynamic segments (owner, repo, workflow_id, sha, branch) with
        placeholders to avoid metric cardinality explosion.
        """
        import re

        if path.startswith("https://"):
            path = "/" + path.split("api.github.com/", 1)[-1]

        path = path.split("?")[0]

        path = re.sub(r"^/repos/[^/]+/[^/]+", "/repos/{owner}/{repo}", path)
        path = re.sub(r"/workflows/\d+", "/workflows/{workflow_id}", path)
        path = re.sub(r"/runs/\d+", "/runs/{run_id}", path)
        path = re.sub(r"/git/commits/[a-f0-9]+", "/git/commits/{sha}", path)
        path = re.sub(r"/branches/.+$", "/branches/{branch}", path)
        path = re.sub(r"/teams/[^/]+", "/teams/{team_slug}", path)

        return path

    def _record_api_request(self, path: str, status_code: int) -> None:
        """Record an API request in Prometheus metrics."""
        endpoint = self._normalize_endpoint(path)
        API_REQUESTS.labels(endpoint=endpoint, status=str(status_code)).inc()

    # -- pagination ----------------------------------------------------------

    _last_next_link: str | None = None

    @staticmethod
    def _parse_next_link(response: httpx.Response) -> str | None:
        link_header = response.headers.get("Link", "")
        if not link_header:
            return None
        for part in link_header.split(","):
            part = part.strip()
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                return url
        return None

    @staticmethod
    def _build_url_key(path: str, params: dict[str, Any] | None) -> str:
        if not params:
            return path
        sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return f"{path}?{sorted_params}"
