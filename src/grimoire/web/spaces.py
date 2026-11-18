"""Space selection for HTML pages: the ``grimoire_spaces`` cookie and ``?spaces=``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request, Response

from grimoire.spaces import SpaceSelection, known_space_ids, list_spaces, parse_space_ids

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


def space_context(request: Request) -> dict[str, Any]:
    """Template context: ``spaces`` (as ``GET /api/spaces``) and ``space_selection``."""
    return {
        "spaces": [space.model_dump() for space in list_spaces()],
        "space_selection": resolve_html_selection(request),
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
