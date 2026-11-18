"""Protection of state-changing routes against cross-site requests.

The routes that run checks and actions, toggle them or save weights are unauthenticated POSTs.
Behind a VPN, a page on any website that a VPN user visits could fire a simple cross-site POST
at them. Browsers label such requests, so they are rejected here. Requests that carry no
browser headers (curl, scripts, other services) are allowed, so API automation keeps working.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from urllib.parse import urlsplit

from fastapi import Request, Response
from fastapi.responses import JSONResponse

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Sec-Fetch-Site values of requests the user's own page made ("none" = typed URL, bookmark)
_TRUSTED_FETCH_SITES = frozenset({"same-origin", "none"})


def is_cross_site(method: str, headers: Mapping[str, str]) -> bool:
    """True if a state-changing request was made by a page of another origin.

    ``Sec-Fetch-Site`` decides when present: every current browser sends it and it does not
    depend on how a proxy rewrites ``Host``. Without it, the ``Origin`` header must name the
    request's own host. Neither header means a non-browser client.
    """
    if method.upper() not in UNSAFE_METHODS:
        return False

    get = headers.get
    fetch_site = get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site.lower() not in _TRUSTED_FETCH_SITES

    origin = get("origin")
    if origin is None:
        return False
    # An opaque origin ("null") has no host and never matches. X-Forwarded-Host is not consulted:
    # the app does not trust proxy-supplied hosts, only the Host header it was given.
    return urlsplit(origin).netloc.lower() != (get("host") or "").lower()


async def cross_site_guard(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Middleware: answer 403 to cross-site state-changing requests."""
    if is_cross_site(request.method, request.headers):
        return JSONResponse({"detail": "Cross-site requests are not allowed"}, status_code=403)
    return await call_next(request)
