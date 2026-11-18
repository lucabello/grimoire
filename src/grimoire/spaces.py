"""Spaces: named slices of the tracked repositories that the UI and API can filter by."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol


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
