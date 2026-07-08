"""PWA plumbing: the service worker and web manifest.

Both are physically stored under ``static/`` but must be served from the site
root. A service worker only controls clients at or below the path it is served
from, so ``sw.js`` has to live at ``/sw.js`` to control the whole app (serving
it from ``/static/`` would scope it to ``/static/`` and control nothing). The
manifest is served here too so it gets the correct ``application/manifest+json``
content type, which ``StaticFiles`` does not emit for ``.webmanifest``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"

# The SW file changes rarely; tell the browser to always revalidate so a new
# worker is picked up promptly rather than sitting behind an HTTP cache.
_NO_CACHE = {"Cache-Control": "no-cache"}


@router.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    return FileResponse(
        _STATIC_DIR / "sw.js",
        media_type="text/javascript",
        headers={**_NO_CACHE, "Service-Worker-Allowed": "/"},
    )


@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> FileResponse:
    return FileResponse(
        _STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )
