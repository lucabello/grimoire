"""PR groups: matching, grouping and the PRs API.

Every open pull request of a tracked repository is stored with the repository stats (see
``github.service``). A group is a set of matchers applied to those stored pull requests at
read time, so changing the config needs no new fetch.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase

from fastapi import APIRouter
from pydantic import BaseModel

from grimoire.config import PRGroup, PRsConfig
from grimoire.models import OpenPullRequest, RepositoryStats

# Start of the per-repository warning added when GraphQL could not report CI status
CI_WARNING_PREFIX = "Could not fetch CI status"

# CI states that need attention first when sorting
_CI_ATTENTION_ORDER = {"failure": 0, "pending": 1, "none": 2, "success": 3}
_UNKNOWN_CI_RANK = 2


def normalize_author(login: str) -> str:
    """Lower-case a login and drop a leading ``app/`` and a trailing ``[bot]``.

    GitHub search spells a bot ``app/renovate`` while the REST API reports
    ``renovate[bot]``; both normalise to ``renovate``.
    """
    return login.strip().lower().removeprefix("app/").removesuffix("[bot]")


def matches(group: PRGroup, repo_full_name: str, pr: OpenPullRequest) -> bool:
    """Whether *pr* belongs to *group*: values of one matcher are alternatives, matchers AND."""
    if group.repos and not any(fnmatchcase(repo_full_name, p) for p in group.repos):
        return False
    if group.authors and normalize_author(pr.author) not in {
        normalize_author(a) for a in group.authors
    }:
        return False
    if group.labels:
        labels = {label.lower() for label in pr.labels}
        if not any(label.lower() in labels for label in group.labels):
            return False
    if group.title_words:
        title = pr.title.lower()
        if not any(word.lower() in title for word in group.title_words):
            return False
    if group.draft is not None and pr.draft != group.draft:
        return False
    return group.base is None or pr.base_ref == group.base


@dataclass(frozen=True)
class GroupedPR:
    """A matching pull request with the repository context the page needs."""

    repo: str
    default_branch: str
    pr: OpenPullRequest

    @property
    def ci_url(self) -> str:
        return f"{self.pr.url}/checks"


@dataclass(frozen=True)
class GroupResult:
    """The pull requests matching one configured group, sorted by repository."""

    name: str
    pull_requests: list[GroupedPR]

    def count(self, ci_state: str | None) -> int:
        return sum(1 for item in self.pull_requests if item.pr.ci_state == ci_state)

    @property
    def total(self) -> int:
        return len(self.pull_requests)

    @property
    def failing(self) -> int:
        return self.count("failure")

    @property
    def pending(self) -> int:
        return self.count("pending")

    @property
    def passing(self) -> int:
        return self.count("success")

    @property
    def no_checks(self) -> int:
        return self.count("none")

    @property
    def repo_count(self) -> int:
        return len({item.repo for item in self.pull_requests})


def build_group_results(
    config: PRsConfig, stats_by_repo: Mapping[str, RepositoryStats]
) -> list[GroupResult]:
    """Match the stored open pull requests against every group, in config order.

    A pull request matching several groups appears in each of them.
    """
    return [
        GroupResult(
            name=group.name,
            pull_requests=[
                GroupedPR(repo=full_name, default_branch=stats.default_branch, pr=pr)
                for full_name, stats in sorted(stats_by_repo.items())
                for pr in stats.open_pr_items
                if matches(group, full_name, pr)
            ],
        )
        for group in config.groups
    ]


# ---------------------------------------------------------------------------
# Page filtering and sorting
# ---------------------------------------------------------------------------

PR_SORT_LABELS = {
    "attention": "Needs attention",
    "repo": "Repository",
    "age": "Oldest first",
    "updated": "Recently updated",
}
CI_FILTERS = ("failure", "pending", "success", "none", "unknown")


def ci_filter_key(pr: OpenPullRequest) -> str:
    """The ``ci`` filter value of a pull request (``unknown`` when CI was not fetched)."""
    return pr.ci_state or "unknown"


def _search_matches(item: GroupedPR, query: str) -> bool:
    return (
        query in item.repo.lower()
        or query in item.pr.title.lower()
        or query in normalize_author(item.pr.author)
    )


def search_results(results: list[GroupResult], search: str) -> list[GroupResult]:
    """Keep the pull requests whose repository, title or author contains *search*."""
    query = search.strip().lower()
    if not query:
        return results
    return [
        GroupResult(g.name, [i for i in g.pull_requests if _search_matches(i, query)])
        for g in results
    ]


def filter_by_ci(results: list[GroupResult], ci: str) -> list[GroupResult]:
    """Keep the pull requests whose CI filter key is *ci* (all of them when empty)."""
    if not ci:
        return results
    return [
        GroupResult(g.name, [i for i in g.pull_requests if ci_filter_key(i.pr) == ci])
        for g in results
    ]


def sort_pull_requests(items: list[GroupedPR], sort: str) -> list[GroupedPR]:
    """Order pull requests; ``attention`` puts failing and pending CI first, oldest first."""
    if sort == "repo":
        return sorted(items, key=lambda i: (i.repo, i.pr.number))
    if sort == "age":
        return sorted(items, key=lambda i: i.pr.created_at)
    if sort == "updated":
        return sorted(items, key=lambda i: i.pr.updated_at or i.pr.created_at, reverse=True)
    return sorted(
        items,
        key=lambda i: (
            _CI_ATTENTION_ORDER.get(i.pr.ci_state or "", _UNKNOWN_CI_RANK),
            i.pr.created_at,
        ),
    )


def distinct_ci_counts(results: list[GroupResult]) -> dict[str, int]:
    """Number of distinct pull requests (across groups) per CI filter key, plus ``total``."""
    seen: dict[tuple[str, int], str] = {}
    for group in results:
        for item in group.pull_requests:
            seen[(item.repo, item.pr.number)] = ci_filter_key(item.pr)
    counts = {key: 0 for key in CI_FILTERS}
    for key in seen.values():
        counts[key] += 1
    return {"total": len(seen), **counts}


# ---------------------------------------------------------------------------
# State and API
# ---------------------------------------------------------------------------

_config: PRsConfig = PRsConfig()


def set_prs_config(config: PRsConfig) -> None:
    """Register the PR groups from the app config."""
    global _config  # noqa: PLW0603
    _config = config


def get_prs_config() -> PRsConfig:
    """Return the registered PR groups."""
    return _config


class PullRequestItem(BaseModel):
    """One pull request of a group; CI fields are ``None`` until GraphQL reported them."""

    repo: str
    number: int
    title: str
    url: str
    author: str
    created_at: datetime
    updated_at: datetime | None
    draft: bool
    labels: list[str]
    base: str
    head_ref: str
    head_sha: str
    auto_merge: bool
    ci: str | None
    ci_url: str
    review_decision: str | None
    merge_state: str | None
    ci_checked_at: datetime | None


class PRGroupResponse(BaseModel):
    name: str
    total: int
    failing: int
    pending: int
    passing: int
    no_checks: int
    pull_requests: list[PullRequestItem]


class PRsResponse(BaseModel):
    last_refresh: datetime | None
    groups: list[PRGroupResponse]


def _to_item(item: GroupedPR) -> PullRequestItem:
    pr = item.pr
    return PullRequestItem(
        repo=item.repo,
        number=pr.number,
        title=pr.title,
        url=pr.url,
        author=pr.author,
        created_at=pr.created_at,
        updated_at=pr.updated_at,
        draft=pr.draft,
        labels=pr.labels,
        base=pr.base_ref,
        head_ref=pr.head_ref,
        head_sha=pr.head_sha,
        auto_merge=pr.auto_merge,
        ci=pr.ci_state,
        ci_url=item.ci_url,
        review_decision=pr.review_decision,
        merge_state=pr.merge_state,
        ci_checked_at=pr.ci_fetched_at,
    )


router = APIRouter(tags=["prs"])


@router.get("/prs", response_model=PRsResponse)
async def list_prs() -> PRsResponse:
    """Open pull requests of each configured group (from the cache; no GitHub calls)."""
    from grimoire.github.router import _cache, _last_refresh

    results = build_group_results(_config, _cache)
    return PRsResponse(
        last_refresh=_last_refresh,
        groups=[
            PRGroupResponse(
                name=group.name,
                total=group.total,
                failing=group.failing,
                pending=group.pending,
                passing=group.passing,
                no_checks=group.no_checks,
                pull_requests=[_to_item(i) for i in group.pull_requests],
            )
            for group in results
        ],
    )
