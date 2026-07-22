"""Unit tests for the supported-image-format single source of truth."""

from __future__ import annotations

import pytest

from agentdeck.images import (
    IMAGE_FORMATS,
    SUPPORTED_IMAGE_MEDIA_TYPES,
    media_type_for_suffix,
    sniff_suffix,
    suffix_for_media_type,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 8
_GIF87 = b"GIF87a" + b"\x00" * 8
_GIF89 = b"GIF89a" + b"\x00" * 8
_WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4


def test_supported_media_types_are_the_four_formats():
    assert SUPPORTED_IMAGE_MEDIA_TYPES == frozenset(
        {"image/png", "image/jpeg", "image/webp", "image/gif"}
    )
    assert {fmt.media_type for fmt in IMAGE_FORMATS} == SUPPORTED_IMAGE_MEDIA_TYPES


@pytest.mark.parametrize(
    ("media_type", "suffix"),
    [
        ("image/png", ".png"),
        ("image/jpeg", ".jpg"),
        ("image/gif", ".gif"),
        ("image/webp", ".webp"),
    ],
)
def test_media_type_and_suffix_round_trip(media_type, suffix):
    assert suffix_for_media_type(media_type) == suffix
    assert media_type_for_suffix(suffix) == media_type


def test_jpeg_alias_and_case_insensitive_suffix():
    # ".jpeg" is an accepted alias even though uploads store ".jpg", and suffix
    # lookup is case-insensitive.
    assert media_type_for_suffix(".jpeg") == "image/jpeg"
    assert media_type_for_suffix(".JPG") == "image/jpeg"
    assert media_type_for_suffix(".PNG") == "image/png"


def test_unsupported_suffix_and_media_type_return_none():
    assert media_type_for_suffix(".bmp") is None
    assert media_type_for_suffix("") is None
    assert suffix_for_media_type("image/bmp") is None
    assert suffix_for_media_type("text/plain") is None


@pytest.mark.parametrize(
    ("data", "suffix"),
    [
        (_PNG, ".png"),
        (_JPEG, ".jpg"),
        (_GIF87, ".gif"),
        (_GIF89, ".gif"),
        (_WEBP, ".webp"),
    ],
)
def test_sniff_recognizes_each_format_signature(data, suffix):
    assert sniff_suffix(data) == suffix


def test_sniff_rejects_non_images_and_partial_webp():
    assert sniff_suffix(b"not an image at all") is None
    assert sniff_suffix(b"") is None
    # RIFF container that is not WEBP must not be accepted.
    assert sniff_suffix(b"RIFF\x00\x00\x00\x00AVI ....") is None
    # RIFF header too short to carry the WEBP tag.
    assert sniff_suffix(b"RIFF\x00\x00") is None
