"""Maintenance PR groups: GitHub pull-request search queries and links.

Grimoire stores no pull requests for these groups. It only knows the tracked
repositories, so it builds the search query and the ``github.com/pulls`` link.

The queries use GitHub's *advanced* issue search syntax (explicit ``OR`` inside
parentheses), which the REST API evaluates with ``advanced_search=true``
(GraphQL: ``type: ISSUE_ADVANCED``).
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from urllib.parse import quote_plus

from fastapi import APIRouter
from pydantic import BaseModel

from grimoire.config import MaintenanceConfig, MaintenanceGroup

PULLS_URL = "https://github.com/pulls"

# GitHub answers 414 for request lines above ~8.1 KB (verified on github.com and
# api.github.com), so a group with many repositories is split into several searches.
MAX_URL_LENGTH = 7000

_PLAIN_VALUE = re.compile(r"[\w./@-]+")
# Unquoted, these are search operators (OR drops the repo: scope) or negation.
_OPERATORS = {"OR", "AND", "NOT"}


class MaintenanceSearch(BaseModel):
    """One GitHub search for a group; ``part`` of ``parts`` when the repos had to be split."""

    name: str
    query: str
    url: str
    part: int = 1
    parts: int = 1


def _quote(value: str) -> str:
    plain = _PLAIN_VALUE.fullmatch(value) and value.upper() not in _OPERATORS
    return value if plain and not value.startswith("-") else f'"{value}"'


def _any_of(terms: list[str]) -> str:
    """Join terms as alternatives; parentheses are required by the advanced syntax."""
    return terms[0] if len(terms) == 1 else f"({' OR '.join(terms)})"


def _url(query: str) -> str:
    return f"{PULLS_URL}?q={quote_plus(query)}"


def select_repos(group: MaintenanceGroup, tracked: list[str]) -> list[str]:
    """Tracked repositories a group applies to, sorted; ``group.repos`` globs narrow them."""
    if not group.repos:
        return sorted(tracked)
    return sorted(
        name for name in tracked if any(fnmatchcase(name, pattern) for pattern in group.repos)
    )


def _matchers(group: MaintenanceGroup) -> list[str]:
    """Query terms for every matcher of the group (the repository scope excluded)."""
    terms: list[str] = []
    if group.authors:
        terms.append(_any_of([f"author:{_quote(author)}" for author in group.authors]))
    if group.labels:
        terms.append(_any_of([f"label:{_quote(label)}" for label in group.labels]))
    if group.title_words:
        # in:title must sit next to each word: "(a OR b) in:title" would search the body too.
        terms.append(_any_of([f"{_quote(word)} in:title" for word in group.title_words]))
    if group.draft is not None:
        terms.append(f"draft:{str(group.draft).lower()}")
    if group.base is not None:
        terms.append(f"base:{_quote(group.base)}")
    if group.status is not None:
        terms.append(f"status:{group.status}")
    return terms


def _query(matchers: list[str], repos: list[str]) -> str:
    repo_term = _any_of([f"repo:{repo}" for repo in repos])
    return " ".join(["is:pr", "is:open", repo_term, *matchers])


def build_group_searches(group: MaintenanceGroup, tracked: list[str]) -> list[MaintenanceSearch]:
    """Build the search(es) for one group over the tracked repositories.

    Values of one list matcher are alternatives, different matchers are ANDed.
    Returns no search when no tracked repository is in scope: a query without ``repo:``
    would search all of GitHub.
    """
    repos = select_repos(group, tracked)
    if not repos:
        return []
    matchers = _matchers(group)

    chunks: list[list[str]] = [[]]
    for repo in repos:
        if chunks[-1] and len(_url(_query(matchers, [*chunks[-1], repo]))) > MAX_URL_LENGTH:
            chunks.append([])
        chunks[-1].append(repo)

    searches: list[MaintenanceSearch] = []
    for index, chunk in enumerate(chunks, start=1):
        query = _query(matchers, chunk)
        searches.append(
            MaintenanceSearch(
                name=group.name,
                query=query,
                url=_url(query),
                part=index,
                parts=len(chunks),
            )
        )
    return searches


# ---------------------------------------------------------------------------
# State and API
# ---------------------------------------------------------------------------

_config: MaintenanceConfig = MaintenanceConfig()


def set_maintenance_config(config: MaintenanceConfig) -> None:
    """Register the maintenance groups from the app config."""
    global _config  # noqa: PLW0603
    _config = config


def get_maintenance_config() -> MaintenanceConfig:
    """Return the registered maintenance groups."""
    return _config


def tracked_repo_names() -> list[str]:
    """Full names of the repositories Grimoire currently tracks."""
    from grimoire.github.router import _repos

    return list(_repos)


router = APIRouter(prefix="/maintenance", tags=["maintenance"])


@router.get("/groups", response_model=list[MaintenanceSearch])
async def list_maintenance_groups() -> list[MaintenanceSearch]:
    """GitHub searches for the configured maintenance groups (config + tracked repos only)."""
    tracked = tracked_repo_names()
    return [search for group in _config.groups for search in build_group_searches(group, tracked)]
