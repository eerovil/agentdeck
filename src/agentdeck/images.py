"""Single source of truth for the image formats AgentDeck supports.

Every supported-set membership test, suffix<->media-type conversion, and
content sniff derives from ONE table, so the accepted formats cannot drift
apart across the upload, image-serving, inject-tracking, transcript-rendering,
and Claude-worker paths that each used to keep their own copy.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageFormat:
    """One supported raster image format."""

    media_type: str
    suffix: str  # the canonical suffix an accepted upload is stored under
    matches: Callable[[bytes], bool]  # true when bytes carry this format's signature


def _is_webp(data: bytes) -> bool:
    # WEBP is a RIFF container tagged "WEBP" at byte 8, not a plain prefix.
    return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"


IMAGE_FORMATS: tuple[ImageFormat, ...] = (
    ImageFormat("image/png", ".png", lambda d: d.startswith(b"\x89PNG\r\n\x1a\n")),
    ImageFormat("image/jpeg", ".jpg", lambda d: d.startswith(b"\xff\xd8\xff")),
    ImageFormat("image/gif", ".gif", lambda d: d.startswith((b"GIF87a", b"GIF89a"))),
    ImageFormat("image/webp", ".webp", _is_webp),
)

#: The media types AgentDeck accepts anywhere it renders or forwards an image.
SUPPORTED_IMAGE_MEDIA_TYPES: frozenset[str] = frozenset(
    fmt.media_type for fmt in IMAGE_FORMATS
)

_FORMAT_BY_MEDIA_TYPE = {fmt.media_type: fmt for fmt in IMAGE_FORMATS}
# Every accepted upload is stored under a format's canonical suffix, but ".jpeg"
# is a common alias a caller may hand us, so it resolves to image/jpeg too.
_MEDIA_TYPE_BY_SUFFIX = {fmt.suffix: fmt.media_type for fmt in IMAGE_FORMATS}
_MEDIA_TYPE_BY_SUFFIX[".jpeg"] = "image/jpeg"


def media_type_for_suffix(suffix: str) -> str | None:
    """The media type for a file suffix, or None if it is not a supported image."""
    return _MEDIA_TYPE_BY_SUFFIX.get(suffix.lower())


def suffix_for_media_type(media_type: str) -> str | None:
    """The canonical suffix for a media type, or None if it is not supported."""
    fmt = _FORMAT_BY_MEDIA_TYPE.get(media_type)
    return fmt.suffix if fmt is not None else None


def sniff_suffix(data: bytes) -> str | None:
    """Canonical suffix for the format whose signature ``data`` starts with.

    Returns None when the bytes match no supported format, so callers can reject
    a file whose declared type does not match its actual contents.
    """
    for fmt in IMAGE_FORMATS:
        if fmt.matches(data):
            return fmt.suffix
    return None
