"""Validated private image uploads for Codex turns."""

from __future__ import annotations

import secrets
import tempfile
from pathlib import Path

from starlette.datastructures import FormData, UploadFile

from ..config import InjectConfig

_CONTENT_TYPES = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class ImageUploadError(ValueError):
    """An image upload failed validation."""


def cleanup_image_files(images: list[Path] | tuple[Path, ...]) -> None:
    """Remove uploaded images and their private directories."""
    parents = {path.parent for path in images}
    for path in images:
        path.unlink(missing_ok=True)
    for parent in parents:
        try:
            parent.rmdir()
        except OSError:
            pass


def _sniff_extension(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


async def save_uploaded_images(form: FormData, config: InjectConfig) -> list[Path]:
    """Validate and save multipart image fields."""
    uploads = [item for item in form.getlist("images") if isinstance(item, UploadFile)]
    uploads = [upload for upload in uploads if upload.filename]
    if len(uploads) > config.max_images:
        raise ImageUploadError("too many images")

    saved: list[Path] = []
    total = 0
    directory: Path | None = None
    try:
        for upload in uploads:
            try:
                expected = _CONTENT_TYPES.get(upload.content_type or "")
                if expected is None:
                    raise ImageUploadError("unsupported image content type")
                data = await upload.read(config.max_image_bytes + 1)
            finally:
                await upload.close()
            if len(data) > config.max_image_bytes:
                raise ImageUploadError("image is too large")
            total += len(data)
            if total > config.max_image_total_bytes:
                raise ImageUploadError("images are too large in total")
            extension = _sniff_extension(data)
            if extension is None or extension != expected:
                raise ImageUploadError("uploaded file is not the declared image type")
            if directory is None:
                directory = Path(tempfile.mkdtemp(prefix="agentdeck-images-"))
            path = directory / f"{secrets.token_hex(16)}{extension}"
            path.write_bytes(data)
            saved.append(path)
    except BaseException:
        cleanup_image_files(saved)
        if directory is not None:
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return saved
