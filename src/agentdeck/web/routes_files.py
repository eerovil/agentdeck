"""Open host files from absolute-path links rendered in chat transcripts."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import mimetypes
import os
import re
import stat
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse

from .deps import get_templates, require_access, resolve_session

router = APIRouter(dependencies=[Depends(require_access)])

_LINE_SUFFIX = re.compile(r"^(?P<path>.+):(?P<line>[1-9]\d*)(?::[1-9]\d*)?$")
_MAX_RENDERED_TEXT_BYTES = 5 * 1024 * 1024
_PREVIEW_CSP = "; ".join(
    (
        "sandbox allow-scripts",
        "default-src 'none'",
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "media-src 'self' data: blob:",
        "connect-src 'none'",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "frame-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors 'self'",
    )
)
_PREVIEW_WRAPPER_CSP = "; ".join(
    (
        "default-src 'none'",
        "style-src 'unsafe-inline'",
        "frame-src 'self'",
        "object-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    )
)
_PREVIEW_MEDIA_TYPES = {
    ".avif": "image/avif",
    ".css": "text/css",
    ".gif": "image/gif",
    ".htm": "text/html",
    ".html": "text/html",
    ".ico": "image/x-icon",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".js": "text/javascript",
    ".json": "application/json",
    ".mjs": "text/javascript",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".otf": "font/otf",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ttf": "font/ttf",
    ".txt": "text/plain",
    ".wasm": "application/wasm",
    ".wav": "audio/wav",
    ".webm": "video/webm",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
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


def _preview_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="preview file not found")


def _preview_parts(relative_path: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if "\\" in relative_path:
        raise _preview_not_found()
    path = Path(relative_path)
    parts = tuple(part for part in path.parts if part != ".")
    if (
        path.is_absolute()
        or any(part in {"", ".."} or part.startswith(".") for part in parts)
        or (not parts and not allow_empty)
    ):
        raise _preview_not_found()
    return parts


def _open_directory(path: Path) -> int:
    """Open an absolute directory path without following any symlink component."""
    if not path.is_absolute():
        raise _preview_not_found()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError:
        os.close(fd)
        raise


def _cwd_identity(cwd: Path | None) -> tuple[str, int, int]:
    if cwd is None:
        raise _preview_not_found()
    directory_fd: int | None = None
    try:
        directory_fd = _open_directory(cwd)
        directory_stat = os.fstat(directory_fd)
        return str(cwd), directory_stat.st_dev, directory_stat.st_ino
    except OSError as exc:
        raise _preview_not_found() from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _open_preview_file(
    cwd: Path | None,
    parts: tuple[str, ...],
    *,
    expected_cwd: tuple[str, int, int] | None = None,
) -> tuple[int, os.stat_result, tuple[str, int, int]]:
    """Descriptor-anchored open that rejects symlinks and non-regular files."""
    if cwd is None:
        raise _preview_not_found()
    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        directory_fd = _open_directory(cwd)
        directory_stat = os.fstat(directory_fd)
        actual_cwd = str(cwd), directory_stat.st_dev, directory_stat.st_ino
        if expected_cwd is not None and actual_cwd != expected_cwd:
            raise OSError("preview cwd changed")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError("preview target is not a regular file")
        return file_fd, file_stat, actual_cwd
    except (OSError, RuntimeError, ValueError) as exc:
        if file_fd is not None:
            os.close(file_fd)
        raise _preview_not_found() from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _token_payload(
    session_key: str,
    root_parts: tuple[str, ...],
    cwd_identity: tuple[str, int, int],
) -> bytes:
    cwd_path, device, inode = cwd_identity
    return b"\0".join(
        (
            session_key.encode(),
            "/".join(root_parts).encode(),
            cwd_path.encode(),
            str(device).encode(),
            str(inode).encode(),
        )
    )


def _preview_token(
    request: Request,
    session_key: str,
    root_parts: tuple[str, ...],
    cwd_identity: tuple[str, int, int],
) -> str:
    root = base64.urlsafe_b64encode("/".join(root_parts).encode()).rstrip(b"=")
    encoded_root = root.decode() or "-"
    signature = hmac.new(
        request.app.state.preview_secret,
        _token_payload(session_key, root_parts, cwd_identity),
        hashlib.sha256,
    ).digest()[:18]
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{encoded_root}.{encoded_signature}"


def _preview_root(
    request: Request, session_key: str, token: str, cwd: Path | None
) -> tuple[tuple[str, ...], tuple[str, int, int]]:
    try:
        encoded_root, encoded_signature = token.split(".", 1)
        root_bytes = b"" if encoded_root == "-" else base64.urlsafe_b64decode(
            encoded_root + "=" * (-len(encoded_root) % 4)
        )
        root_parts = _preview_parts(root_bytes.decode(), allow_empty=True)
        supplied = base64.urlsafe_b64decode(
            encoded_signature + "=" * (-len(encoded_signature) % 4)
        )
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise _preview_not_found() from exc
    cwd_identity = _cwd_identity(cwd)
    expected = hmac.new(
        request.app.state.preview_secret,
        _token_payload(session_key, root_parts, cwd_identity),
        hashlib.sha256,
    ).digest()[:18]
    if not hmac.compare_digest(supplied, expected):
        raise _preview_not_found()
    return root_parts, cwd_identity


def _fd_chunks(fd: int) -> Iterator[bytes]:
    try:
        while chunk := os.read(fd, 64 * 1024):
            yield chunk
    finally:
        os.close(fd)


@router.get("/sessions/{session_key}/preview/{relative_path:path}")
async def session_preview(
    request: Request, session_key: str, relative_path: str
) -> HTMLResponse:
    """Render a trusted shell around one explicitly selected HTML entry point."""
    _account, session, _provider = resolve_session(request, session_key)
    parts = _preview_parts(relative_path)
    if Path(parts[-1]).suffix.lower() not in {".htm", ".html"}:
        raise _preview_not_found()
    file_fd, _file_stat, cwd_identity = _open_preview_file(session.cwd, parts)
    os.close(file_fd)
    root_parts = parts[:-1]
    token = _preview_token(request, session_key, root_parts, cwd_identity)
    preview_src = request.url_for(
        "session_preview_asset",
        session_key=session_key,
        token=token,
        asset_path=parts[-1],
    )
    response = get_templates(request).TemplateResponse(
        request,
        "preview.html",
        {
            "session": session,
            "entry_path": "/".join(parts),
            "preview_src": str(preview_src),
        },
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Content-Security-Policy"] = _PREVIEW_WRAPPER_CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get(
    "/session-previews/{session_key}/{token}/{asset_path:path}",
    name="session_preview_asset",
)
async def session_preview_asset(
    request: Request, session_key: str, token: str, asset_path: str
) -> StreamingResponse:
    """Serve one allowlisted file from the selected preview bundle directory."""
    _account, session, _provider = resolve_session(request, session_key)
    root_parts, cwd_identity = _preview_root(
        request, session_key, token, session.cwd
    )
    asset_parts = _preview_parts(asset_path)
    media_type = _PREVIEW_MEDIA_TYPES.get(Path(asset_parts[-1]).suffix.lower())
    if media_type is None:
        raise _preview_not_found()
    file_fd, file_stat, _actual_cwd = _open_preview_file(
        session.cwd,
        root_parts + asset_parts,
        expected_cwd=cwd_identity,
    )
    headers = {
        "Cache-Control": "private, no-store",
        "Content-Length": str(file_stat.st_size),
        "X-Content-Type-Options": "nosniff",
        "Access-Control-Allow-Origin": "null",
        "Cross-Origin-Resource-Policy": "cross-origin",
        "Content-Security-Policy": _PREVIEW_CSP,
        "Cross-Origin-Opener-Policy": "same-origin",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        ),
    }
    return StreamingResponse(_fd_chunks(file_fd), media_type=media_type, headers=headers)


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
