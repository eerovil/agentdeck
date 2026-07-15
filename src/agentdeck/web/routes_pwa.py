"""PWA plumbing: the service worker and web manifest.

Both are physically stored under ``static/`` but must be served from the site
root. A service worker only controls clients at or below the path it is served
from, so ``sw.js`` has to live at ``/sw.js`` to control the whole app (serving
it from ``/static/`` would scope it to ``/static/`` and control nothing). The
manifest is served here too so it gets the correct ``application/manifest+json``
content type, which ``StaticFiles`` does not emit for ``.webmanifest``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"

# The SW file changes rarely; tell the browser to always revalidate so a new
# worker is picked up promptly rather than sitting behind an HTTP cache.
_NO_CACHE = {"Cache-Control": "no-cache"}

# Shell assets whose content decides the SW cache name — any change to one of
# these busts the cache (see the __CACHE_STAMP__ note in sw.js).
_STAMP_ASSETS = (
    "app.css",
    "sse.js",
    "htmx.min.js",
    "mobile_session_stack.js",
    "manifest.webmanifest",
)


def cache_stamp() -> str:
    """Short content hash over the cached shell assets, so both the SW cache
    name and the versioned asset URLs change whenever any of them does."""
    h = hashlib.sha1()
    for name in _STAMP_ASSETS:
        try:
            h.update((_STATIC_DIR / name).read_bytes())
        except OSError:
            h.update(b"?")
    return h.hexdigest()[:12]


@router.get("/sw.js", include_in_schema=False)
async def service_worker() -> Response:
    body = (_STATIC_DIR / "sw.js").read_text().replace("__CACHE_STAMP__", cache_stamp())
    return Response(
        body,
        media_type="text/javascript",
        headers={**_NO_CACHE, "Service-Worker-Allowed": "/"},
    )


@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> FileResponse:
    return FileResponse(
        _STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )
