"""Open host files from absolute-path links rendered in chat transcripts."""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from .deps import get_templates, require_access

router = APIRouter(dependencies=[Depends(require_access)])

_LINE_SUFFIX = re.compile(r"^(?P<path>.+):(?P<line>[1-9]\d*)(?::[1-9]\d*)?$")
_MAX_RENDERED_TEXT_BYTES = 5 * 1024 * 1024
_TEXT_MEDIA_TYPES = {
    "application/javascript",
    "application/json",
    "application/ld+json",
    "application/sql",
    "application/toml",
    "application/xhtml+xml",
    "application/xml",
    "image/svg+xml",
}


def _candidate(local_path: str) -> Path:
    """Map the URL catch-all back to the absolute host path it represents."""
    return Path("/") / local_path


def _resolve_file(local_path: str) -> tuple[Path, int | None]:
    """Prefer an exact filename, then interpret a final ``:line[:column]``."""
    exact = _candidate(local_path)
    if exact.is_file():
        return exact, None

    match = _LINE_SUFFIX.fullmatch(local_path)
    if match:
        without_line = _candidate(match.group("path"))
        if without_line.is_file():
            return without_line, int(match.group("line"))

    raise HTTPException(status_code=404, detail="local file not found")


def _is_text_file(path: Path, media_type: str | None) -> bool:
    if media_type and (media_type.startswith("text/") or media_type in _TEXT_MEDIA_TYPES):
        return True
    return path.suffix.lower() in {
        ".cfg",
        ".conf",
        ".env",
        ".ini",
        ".jsonl",
        ".log",
        ".md",
        ".markdown",
        ".py",
        ".rst",
        ".sh",
        ".toml",
        ".yaml",
        ".yml",
    }


@router.get("/{local_path:path}", response_class=Response)
async def local_file(
    request: Request, local_path: str, raw: bool = False
) -> Response:
    """Serve any readable host file; render small text files without executing them."""
    path, line_number = _resolve_file(local_path)
    media_type, _ = mimetypes.guess_type(path.name)

    try:
        stat = path.stat()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="local file is not readable") from exc
    except OSError as exc:
        raise HTTPException(status_code=404, detail="local file not found") from exc

    if _is_text_file(path, media_type) and stat.st_size <= _MAX_RENDERED_TEXT_BYTES:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="local file is not readable") from exc
        except OSError as exc:
            raise HTTPException(status_code=404, detail="local file not found") from exc

        markdown = (
            path.suffix.lower() in {".md", ".markdown"}
            and not raw
            and (line_number is None or line_number == 1)
        )
        response = get_templates(request).TemplateResponse(
            request,
            "local_file.html",
            {
                "path": path,
                "content": content,
                "lines": content.splitlines(),
                "line_number": line_number,
                "markdown": markdown,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        content_disposition_type="inline",
        stat_result=stat,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
