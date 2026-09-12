"""Vectors for the shared, persistence-free image processor.

Covers accepted/rejected formats and limits, first-frame flattening, EXIF
orientation, crop validation, deterministic repeated-output hash equality,
512x512 sRGB metadata-free output, and the bounded 64x64/16-KiB compact
derivative. Design of record: graph://4f9e881c-a9 §6, comment 8cc8b2ed-5ae.
"""
from __future__ import annotations

import base64
import hashlib
import io

import pytest

from PIL import Image

from tools.dashboard import profile_image
from tools.dashboard.profile_image import (
    Crop,
    ProfileImageError,
    process_profile_image,
)


# ── input builders ───────────────────────────────────────────────────


def _encode(img: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def _solid(w: int, h: int, color=(200, 100, 50), fmt="PNG", **kw) -> bytes:
    return _encode(Image.new("RGB", (w, h), color), fmt, **kw)


def _full_crop() -> dict:
    return {"x": 0.0, "y": 0.0, "size": 1.0}


# ── accepted formats ─────────────────────────────────────────────────


class TestAcceptedFormats:
    @pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
    def test_accepts_jpeg_png_webp(self, fmt):
        out = process_profile_image(_solid(300, 300, fmt=fmt), _full_crop())
        assert out.canonical_mime == "image/webp"
        canonical = Image.open(io.BytesIO(out.canonical_webp))
        assert canonical.format == "WEBP"
        assert canonical.size == (512, 512)

    def test_transparent_png_is_accepted(self):
        img = Image.new("RGBA", (300, 300), (10, 20, 30, 0))
        out = process_profile_image(_encode(img, "PNG"), _full_crop())
        canonical = Image.open(io.BytesIO(out.canonical_webp))
        assert canonical.size == (512, 512)


class TestRejectedFormats:
    def test_gif_is_unsupported(self):
        with pytest.raises(ProfileImageError) as exc:
            process_profile_image(_solid(300, 300, fmt="GIF"), _full_crop())
        assert exc.value.code == "unsupported_format"


# ── size / decode limits ─────────────────────────────────────────────


class TestLimits:
    def test_empty_input(self):
        with pytest.raises(ProfileImageError) as exc:
            process_profile_image(b"", _full_crop())
        assert exc.value.code == "empty"

    def test_undecodable_input(self):
        with pytest.raises(ProfileImageError) as exc:
            process_profile_image(b"not an image at all" * 8, _full_crop())
        assert exc.value.code == "undecodable"

    def test_under_min_dimension(self):
        with pytest.raises(ProfileImageError) as exc:
            process_profile_image(_solid(127, 400), _full_crop())
        assert exc.value.code == "too_small"

    def test_exactly_min_dimension_is_accepted(self):
        out = process_profile_image(_solid(128, 128), _full_crop())
        assert Image.open(io.BytesIO(out.canonical_webp)).size == (512, 512)

    def test_over_ten_mib(self, monkeypatch):
        # Build a payload just over the limit without decoding cost.
        raw = _solid(200, 200)
        raw = raw + b"\x00" * (profile_image.MAX_INPUT_BYTES + 1 - len(raw))
        with pytest.raises(ProfileImageError) as exc:
            process_profile_image(raw, _full_crop())
        assert exc.value.code == "too_large"

    def test_over_forty_megapixels(self, monkeypatch):
        # Lower the ceiling rather than allocate a 40 MP bitmap; the check
        # reads img.size before any full decode.
        monkeypatch.setattr(profile_image, "MAX_PIXELS", 100 * 100)
        with pytest.raises(ProfileImageError) as exc:
            process_profile_image(_solid(300, 300), _full_crop())
        assert exc.value.code == "too_many_pixels"


# ── first-frame flattening ───────────────────────────────────────────


class TestAnimationFlattening:
    def test_first_frame_of_animated_webp(self):
        frames = [
            Image.new("RGB", (200, 200), (255, 0, 0)),
            Image.new("RGB", (200, 200), (0, 255, 0)),
        ]
        buf = io.BytesIO()
        frames[0].save(
            buf, format="WEBP", save_all=True,
            append_images=frames[1:], duration=100,
        )
        out = process_profile_image(buf.getvalue(), _full_crop())
        canonical = Image.open(io.BytesIO(out.canonical_webp)).convert("RGB")
        r, g, b = canonical.getpixel((256, 256))
        assert r > 200 and g < 60 and b < 60  # the first (red) frame


# ── EXIF orientation ─────────────────────────────────────────────────


class TestExifOrientation:
    def test_orientation_is_applied_before_crop(self):
        # A landscape image, left half red / right half blue, tagged
        # orientation 6 (rotate 270° CW on display → the physical right edge
        # becomes the top). After transpose the image is portrait 160x240; a
        # full-frame crop must succeed and produce a square output.
        img = Image.new("RGB", (240, 160))
        for x in range(240):
            for y in range(160):
                img.putpixel((x, y), (255, 0, 0) if x < 120 else (0, 0, 255))
        exif = img.getexif()
        exif[274] = 6
        raw = _encode(img, "JPEG", exif=exif)
        out = process_profile_image(raw, _full_crop())
        canonical = Image.open(io.BytesIO(out.canonical_webp))
        assert canonical.size == (512, 512)


# ── crop validation ──────────────────────────────────────────────────


class TestCropValidation:
    @pytest.mark.parametrize("crop", [
        {"x": 0.6, "y": 0.0, "size": 0.5},   # x + size > 1
        {"x": 0.0, "y": 0.7, "size": 0.5},   # y + size > 1
        {"x": -0.1, "y": 0.0, "size": 0.5},  # x < 0
        {"x": 0.0, "y": 0.0, "size": 0.0},   # size == 0
        {"x": 0.0, "y": 0.0, "size": 1.5},   # size > 1
        {"x": 1.0, "y": 0.0, "size": 0.5},   # x not < 1
        {"x": 0.0, "y": 0.0},                # missing size
        {"x": 0.0, "y": 0.0, "size": 0.5, "extra": 1},  # extra key
        "not-an-object",
        {"x": "a", "y": 0.0, "size": 0.5},   # non-numeric
        {"x": float("nan"), "y": 0.0, "size": 0.5},  # non-finite
        {"x": True, "y": 0.0, "size": 0.5},  # bool masquerading as number
    ])
    def test_invalid_crops_rejected(self, crop):
        with pytest.raises(ProfileImageError) as exc:
            process_profile_image(_solid(300, 300), crop)
        assert exc.value.code == "invalid_crop"

    def test_valid_crop_selects_the_region(self):
        # Left half red, right half green; crop the right half → mostly green.
        img = Image.new("RGB", (400, 400))
        for x in range(400):
            for y in range(400):
                img.putpixel((x, y), (255, 0, 0) if x < 200 else (0, 255, 0))
        out = process_profile_image(
            _encode(img, "PNG"), {"x": 0.5, "y": 0.0, "size": 0.5}
        )
        canonical = Image.open(io.BytesIO(out.canonical_webp)).convert("RGB")
        r, g, b = canonical.getpixel((256, 256))
        assert g > 200 and r < 60


# ── deterministic output ─────────────────────────────────────────────


class TestDeterminism:
    def test_repeated_output_is_byte_identical(self):
        raw = _solid(500, 500, color=(30, 90, 150), fmt="JPEG", quality=92)
        a = process_profile_image(raw, {"x": 0.1, "y": 0.1, "size": 0.7})
        b = process_profile_image(raw, Crop(0.1, 0.1, 0.7))
        assert hashlib.sha256(a.canonical_webp).digest() == \
            hashlib.sha256(b.canonical_webp).digest()
        assert a.compact_data_uri == b.compact_data_uri


# ── canonical output shape / metadata ────────────────────────────────


class TestCanonicalOutput:
    def test_output_is_512_srgb_webp_without_metadata(self):
        img = Image.new("RGB", (400, 400), (12, 34, 56))
        exif = img.getexif()
        exif[270] = "a private comment"
        raw = _encode(img, "JPEG", exif=exif)
        out = process_profile_image(raw, _full_crop())
        canonical = Image.open(io.BytesIO(out.canonical_webp))
        assert canonical.size == (512, 512)
        assert canonical.mode in ("RGB", "RGBA")
        assert not canonical.getexif()  # metadata stripped
        assert "icc_profile" not in canonical.info

    def test_canonical_within_512_kib(self):
        out = process_profile_image(_solid(600, 600), _full_crop())
        assert len(out.canonical_webp) <= profile_image.CANONICAL_MAX_BYTES


# ── compact derivative bounds ────────────────────────────────────────


class TestCompactDerivative:
    def test_compact_is_64_and_within_bounds(self):
        out = process_profile_image(_solid(600, 600), _full_crop())
        compact = Image.open(io.BytesIO(out.compact_webp))
        assert compact.size == (64, 64)
        assert len(out.compact_webp) <= profile_image.COMPACT_MAX_BYTES
        assert len(out.compact_data_uri) <= profile_image.COMPACT_MAX_URI_CHARS

    def test_compact_data_uri_shape(self):
        out = process_profile_image(_solid(300, 300), _full_crop())
        assert out.compact_data_uri.startswith("data:image/webp;base64,")
        b64 = out.compact_data_uri.split(",", 1)[1]
        # The declared bytes round-trip through the URI unchanged.
        assert base64.b64decode(b64) == out.compact_webp
