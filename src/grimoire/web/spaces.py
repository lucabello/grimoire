"""Space selection for HTML pages: the ``grimoire_spaces`` cookie and ``?spaces=``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request, Response

from grimoire.spaces import (
    SpaceSelection,
    count_repos,
    known_space_ids,
    list_spaces,
    parse_space_ids,
)

COOKIE_NAME = "grimoire_spaces"
COOKIE_MAX_AGE = 365 * 24 * 60 * 60


def cookie_path(request: Request) -> str:
    """Cookie path: the app's root_path, so several instances under one host do not clash."""
    return str(request.scope.get("root_path", "")).rstrip("/") or "/"


def resolve_html_selection(request: Request) -> SpaceSelection:
    """The selection for an HTML page or partial: ``?spaces=`` beats the cookie.

    A ``?spaces=`` parameter also asks for the cookie to be (re)written, which
    ``space_cookie_middleware`` does on the way out. The result is computed once per request.
    """
    cached = getattr(request.state, "space_selection", None)
    if cached is not None:
        return cached

    known = known_space_ids()
    if "spaces" in request.query_params:
        selection = SpaceSelection(parse_space_ids(request.query_params["spaces"], known))
        request.state.space_cookie = ",".join(sorted(selection.ids or ()))
    else:
        selection = SpaceSelection(parse_space_ids(request.cookies.get(COOKIE_NAME), known))
    request.state.space_selection = selection
    return selection


def html_space_selection(request: Request) -> SpaceSelection:
    """FastAPI dependency for HTML routes (see ``resolve_html_selection``)."""
    return resolve_html_selection(request)


def space_filter_bar(
    selection: SpaceSelection, spaces: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Data of the active-filter bar, or None under All (nothing is filtered).

    Every chip links to ``?spaces=`` with that space removed, so the bar needs no script: the
    server applies the new selection and saves it in the cookie. Removing the last space
    leaves an empty value, which means All.
    """
    if selection.ids is None:
        return None
    selected = [space for space in spaces if space["id"] in selection.ids]
    shown, total = count_repos(selection)
    return {
        "chips": [
            {
                "name": space["name"],
                "remove_href": "?spaces="
                + ",".join(other["id"] for other in selected if other is not space),
            }
            for space in selected
        ],
        "shown": shown,
        "total": total,
    }


def space_context(request: Request) -> dict[str, Any]:
    """Template context: ``spaces`` (as ``GET /api/spaces``), ``space_selection``, ``space_filter``."""
    selection = resolve_html_selection(request)
    spaces = [space.model_dump() for space in list_spaces()]
    return {
        "spaces": spaces,
        "space_selection": selection,
        "space_filter": space_filter_bar(selection, spaces),
    }


async def space_cookie_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Persist a ``?spaces=`` selection in the cookie.

    FastAPI does not merge cookies set on a dependency's response into a directly returned
    ``TemplateResponse``, so the header is written here. Ids only contain ``[a-z0-9_-]`` and
    commas, which the browser-side script writes unquoted too.
    """
    response = await call_next(request)
    value = getattr(request.state, "space_cookie", None)
    if value is not None:
        response.headers.append(
            "set-cookie",
            f"{COOKIE_NAME}={value}; Max-Age={COOKIE_MAX_AGE}; "
            f"Path={cookie_path(request)}; SameSite=Lax",
        )
    return response
