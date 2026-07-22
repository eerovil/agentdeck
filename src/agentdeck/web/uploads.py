"""Validated private image uploads for Codex turns."""

from __future__ import annotations

import secrets
import tempfile
from pathlib import Path

from starlette.datastructures import FormData, UploadFile

from ..config import InjectConfig
from ..images import sniff_suffix, suffix_for_media_type


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
                expected = suffix_for_media_type(upload.content_type or "")
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
            extension = sniff_suffix(data)
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
