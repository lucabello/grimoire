"""Spaces: named slices of the tracked repositories that the UI and API can filter by.

Spaces are a view filter, not a security boundary. This module depends only on the config and
the domain models so every layer (API routers, web pages, checks, actions) can use it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from fastapi import APIRouter, Query
from pydantic import BaseModel

from grimoire.config import SpaceConfig
from grimoire.models import RepoMembership, TrackedRepository

# Pseudo-space of the repos with no space membership. Real ids cannot start with "_".
UNASSIGNED = "_unassigned"
UNASSIGNED_NAME = "Unassigned"

_spaces: list[SpaceConfig] = []
_repos_provider: Callable[[], Iterable[TrackedRepository]] = lambda: ()  # noqa: E731


def set_spaces_config(
    spaces: list[SpaceConfig],
    repos: Callable[[], Iterable[TrackedRepository]] | None = None,
) -> None:
    """Register the configured spaces and where to read the current tracked repos from."""
    global _spaces, _repos_provider  # noqa: PLW0603
    _spaces = list(spaces)
    _repos_provider = repos if repos is not None else (lambda: ())


class SpaceInfo(BaseModel):
    """A selectable space, as returned by ``GET /api/spaces`` and exposed to templates."""

    id: str
    name: str
    color: str | None = None
    description: str = ""
    repo_count: int = 0


def list_spaces() -> list[SpaceInfo]:
    """Configured spaces with repo counts, plus Unassigned when such repos exist.

    Empty when no spaces are configured: the selector is not offered at all then.
    """
    if not _spaces:
        return []
    repos = list(_repos_provider())
    infos = [
        SpaceInfo(
            id=space.id,
            name=space.name,
            color=space.color,
            description=space.description,
            repo_count=sum(1 for repo in repos if space.id in repo.spaces),
        )
        for space in _spaces
    ]
    unassigned = sum(1 for repo in repos if not repo.spaces)
    if unassigned:
        infos.append(SpaceInfo(id=UNASSIGNED, name=UNASSIGNED_NAME, repo_count=unassigned))
    return infos


@dataclass(frozen=True)
class SpaceSelection:
    """Which spaces a view is filtered by. ``ids`` is None for All."""

    ids: frozenset[str] | None = None

    def includes(self, repo: TrackedRepository) -> bool:
        if self.ids is None:
            return True
        spaces = repo.spaces
        return bool(self.ids.intersection(spaces)) or (UNASSIGNED in self.ids and not spaces)

    def lists(self, definition_spaces: Iterable[str]) -> bool:
        """True if a check/action with these ``spaces`` is listed under the selection.

        Definitions without spaces are global and always listed; ``_unassigned`` alone therefore
        lists only the global ones (it can never be a definition's space).
        """
        spaces = list(definition_spaces)
        return not spaces or self.ids is None or bool(self.ids.intersection(spaces))

    def repo_name_filter(self) -> Callable[[str], bool]:
        """A predicate on repo full names, for rows that only carry the name (check results).

        Repos that are no longer tracked are not part of a narrower selection.
        """
        if self.ids is None:
            return lambda _name: True
        return {repo.full_name for repo in _repos_provider() if self.includes(repo)}.__contains__

    def workflow_visible(self, repo: TrackedRepository, workflow_name: str) -> bool:
        """True if a selected space's membership of the repo allows the workflow.

        A repo without any membership (nothing known yet) shows every workflow.
        """
        if not repo.memberships:
            return True
        return any(m.allows(workflow_name) for m in self._considered(repo))

    def _considered(self, repo: TrackedRepository) -> list[RepoMembership]:
        if self.ids is None:
            return repo.memberships
        unassigned = UNASSIGNED in self.ids and not repo.spaces
        return [
            m
            for m in repo.memberships
            if (m.space is None and unassigned) or (m.space is not None and m.space in self.ids)
        ]


ALL_SPACES = SpaceSelection()


def parse_space_ids(raw: str | None, known: set[str]) -> frozenset[str] | None:
    """Parse a comma-separated id list into a selection (None = All).

    Unknown ids are dropped; an empty result or every known id means All.
    """
    if not raw:
        return None
    ids = frozenset(part for part in (p.strip() for p in raw.split(",")) if part in known)
    if not ids or ids == known:
        return None
    return ids


def known_space_ids() -> set[str]:
    """Ids the current selector offers (configured spaces, plus Unassigned when offered)."""
    return {space.id for space in list_spaces()}


def api_space_selection(
    spaces: str | None = Query(
        None,
        description="Comma-separated space ids to filter by; omit for all spaces. "
        "The JSON API never reads the selection cookie.",
    ),
) -> SpaceSelection:
    """FastAPI dependency: the selection from ``?spaces=`` only (default All)."""
    return SpaceSelection(parse_space_ids(spaces, known_space_ids()))


router = APIRouter(tags=["spaces"])


@router.get("/spaces", response_model=list[SpaceInfo])
async def get_spaces() -> list[SpaceInfo]:
    """List the configured spaces (and Unassigned when such repos exist) with repo counts."""
    return list_spaces()


class _SpaceScoped(Protocol):
    slug: str
    spaces: list[str]


def validate_definition_spaces(
    kind: str, definitions: Iterable[_SpaceScoped], space_ids: set[str]
) -> None:
    """Raise ``ValueError`` if a check/action lists a space id that is not configured.

    ``_unassigned`` is a pseudo-space of the selector, never a valid value here.
    """
    for definition in definitions:
        for space_id in definition.spaces:
            if space_id not in space_ids:
                known = ", ".join(sorted(space_ids)) or "none configured"
                raise ValueError(
                    f"Unknown space '{space_id}' in {kind} '{definition.slug}' (known: {known})"
                )
