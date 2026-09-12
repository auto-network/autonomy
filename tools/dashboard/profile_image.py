"""Shared, persistence-free profile-image normalization seam.

One neutral byte→byte processor that every profile-photo consumer (the
organization icon routes today; Personal and member profiles later) runs an
uploaded image through before it is stored. It contains no Personal- or
organization-specific logic and performs no persistence — it takes raw input
bytes plus an explicit crop and returns normalized output bytes. The caller
decides where those bytes live.

Design of record: graph://4f9e881c-a9 §6 and comment 8cc8b2ed-5ae (the
"canonical derivative" and "security and privacy" contracts), applied to
organization icons for auto-j1y0z.

Contract:

* Accepts JPEG, PNG, or WebP input.
* Rejects, before producing any output, an input that is empty, over 10 MiB,
  undecodable, an unsupported format, under 128x128, or over 40 megapixels.
* Flattens an animated image to its first fully decoded frame.
* Applies EXIF orientation, then the explicit normalized square crop (finite
  ``{x, y, size}`` in the EXIF-oriented image, ``0 <= x,y < 1``,
  ``0 < size <= 1``, ``x+size <= 1`` and ``y+size <= 1``).
* Converts to sRGB and strips all metadata (EXIF/GPS/comments/embedded
  thumbnails/ICC).
* Deterministically emits a 512x512 WebP no larger than 512 KiB, plus a bounded
  64x64 WebP derivative (at most 16 KiB encoded and 24,000 data-URI
  characters).

Every rejection raises :class:`ProfileImageError` carrying a stable ``code``
so the caller can map it to an actionable message without parsing prose.
"""
from __future__ import annotations

import base64
import io
import math
from dataclasses import dataclass

from PIL import Image, ImageCms, ImageOps

# Pillow's own decompression-bomb guard. We additionally check dimensions
# explicitly (below) so the message is our own actionable code rather than a
# Pillow warning/exception, but raising the ceiling here keeps a genuinely
# hostile file from being fully decoded before our check runs.
_MAX_MEGAPIXELS = 40
MAX_PIXELS = _MAX_MEGAPIXELS * 1_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

#: Input guards.
MAX_INPUT_BYTES = 10 * 1024 * 1024
MIN_SOURCE_DIMENSION = 128
ACCEPTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})

#: Canonical output.
CANONICAL_SIZE = 512
CANONICAL_MAX_BYTES = 512 * 1024

#: Bounded compact derivative.
COMPACT_SIZE = 64
COMPACT_MAX_BYTES = 16 * 1024
COMPACT_MAX_URI_CHARS = 24_000
COMPACT_MIME = "image/webp"

#: Deterministic WebP encoding: fixed method, quality ladder walked
#: highest-first so the first size-conforming quality wins and repeated runs
#: over the same input produce byte-identical output.
_WEBP_METHOD = 6
_QUALITY_LADDER = (90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40)


class ProfileImageError(ValueError):
    """A rejected profile image. ``code`` is a stable machine token.

    Codes: ``empty``, ``too_large``, ``undecodable``, ``unsupported_format``,
    ``too_small``, ``too_many_pixels``, ``invalid_crop``, ``output_too_large``.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Crop:
    """A normalized square crop in the EXIF-oriented image's coordinate space.

    ``x``/``y`` are the top-left corner and ``size`` the extent, each a
    fraction of the corresponding axis. Validity: all finite, ``0 <= x,y < 1``,
    ``0 < size <= 1``, ``x+size <= 1`` and ``y+size <= 1``.
    """

    x: float
    y: float
    size: float

    @classmethod
    def from_obj(cls, obj: object) -> "Crop":
        """Build a Crop from a JSON-decoded ``{x, y, size}`` object.

        Raises :class:`ProfileImageError` (``invalid_crop``) for anything that
        is not exactly that shape with finite numeric values in range. Booleans
        are rejected even though ``bool`` is an ``int`` subclass — a crop
        coordinate of ``True`` is a malformed body, not a coordinate of 1.
        """
        if not isinstance(obj, dict):
            raise ProfileImageError("invalid_crop", "crop must be an object")
        if set(obj) != {"x", "y", "size"}:
            raise ProfileImageError(
                "invalid_crop", "crop must have exactly x, y, and size"
            )
        vals: dict[str, float] = {}
        for name in ("x", "y", "size"):
            raw = obj[name]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ProfileImageError(
                    "invalid_crop", f"crop.{name} must be a finite number"
                )
            value = float(raw)
            if not math.isfinite(value):
                raise ProfileImageError(
                    "invalid_crop", f"crop.{name} must be a finite number"
                )
            vals[name] = value
        crop = cls(x=vals["x"], y=vals["y"], size=vals["size"])
        crop.validate()
        return crop

    def validate(self) -> None:
        if not (0.0 <= self.x < 1.0) or not (0.0 <= self.y < 1.0):
            raise ProfileImageError(
                "invalid_crop", "crop x and y must be in [0, 1)"
            )
        if not (0.0 < self.size <= 1.0):
            raise ProfileImageError(
                "invalid_crop", "crop size must be in (0, 1]"
            )
        if self.x + self.size > 1.0 or self.y + self.size > 1.0:
            raise ProfileImageError(
                "invalid_crop", "crop must lie within the image bounds"
            )


@dataclass(frozen=True)
class ProcessedProfileImage:
    """The normalized outputs. Bytes only — no identity, no storage."""

    canonical_webp: bytes
    canonical_mime: str
    compact_webp: bytes
    compact_data_uri: str


def process_profile_image(raw: bytes, crop: object) -> ProcessedProfileImage:
    """Normalize ``raw`` under ``crop`` into the canonical + compact outputs.

    ``crop`` may be a :class:`Crop` or a JSON-decoded ``{x, y, size}`` object.
    Every failure raises :class:`ProfileImageError` before any output is
    produced, so a caller can treat a raised error as "nothing changed".
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise ProfileImageError("undecodable", "input must be bytes")
    raw = bytes(raw)
    if not raw:
        raise ProfileImageError("empty", "the image is empty")
    if len(raw) > MAX_INPUT_BYTES:
        raise ProfileImageError(
            "too_large",
            f"the image is larger than {MAX_INPUT_BYTES // (1024 * 1024)} MiB",
        )

    crop_spec = crop if isinstance(crop, Crop) else Crop.from_obj(crop)
    # A pre-built Crop is re-validated so an out-of-range object can never slip
    # past by being constructed directly.
    crop_spec.validate()

    try:
        img = Image.open(io.BytesIO(raw))
    except Image.DecompressionBombError:
        raise ProfileImageError(
            "too_many_pixels",
            f"the image exceeds {_MAX_MEGAPIXELS} megapixels",
        )
    except Exception:
        raise ProfileImageError("undecodable", "the image could not be decoded")

    source_format = (img.format or "").upper()
    if source_format not in ACCEPTED_FORMATS:
        raise ProfileImageError(
            "unsupported_format",
            "only JPEG, PNG, and WebP images are accepted",
        )

    # Pixel-count guard on the declared size BEFORE any full decode, so a
    # decompression bomb is rejected without allocating its pixels.
    width, height = img.size
    if width * height > MAX_PIXELS:
        raise ProfileImageError(
            "too_many_pixels",
            f"the image exceeds {_MAX_MEGAPIXELS} megapixels",
        )

    # Flatten animation to the first frame, then fully decode it.
    try:
        if getattr(img, "n_frames", 1) > 1:
            img.seek(0)
        img.load()
    except Image.DecompressionBombError:
        raise ProfileImageError(
            "too_many_pixels",
            f"the image exceeds {_MAX_MEGAPIXELS} megapixels",
        )
    except Exception:
        raise ProfileImageError("undecodable", "the image could not be decoded")

    icc_profile = img.info.get("icc_profile")

    # EXIF orientation, applied before the crop so normalized coordinates land
    # on the pixels the uploader saw. exif_transpose returns a new image with
    # orientation baked in and the EXIF orientation tag consumed.
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        # A malformed EXIF block is not fatal — proceed with the raw pixels.
        pass

    oriented_width, oriented_height = img.size
    if oriented_width < MIN_SOURCE_DIMENSION or oriented_height < MIN_SOURCE_DIMENSION:
        raise ProfileImageError(
            "too_small",
            f"the image must be at least {MIN_SOURCE_DIMENSION}x"
            f"{MIN_SOURCE_DIMENSION} pixels",
        )

    rgb = _to_srgb_rgb(img, icc_profile)

    # Apply the normalized crop in the oriented pixel space. Rounding is
    # deterministic and clamped to the image bounds so a crop that touches the
    # far edge (x+size == 1) never produces an off-by-one out-of-range box.
    left = int(round(crop_spec.x * oriented_width))
    top = int(round(crop_spec.y * oriented_height))
    right = int(round((crop_spec.x + crop_spec.size) * oriented_width))
    bottom = int(round((crop_spec.y + crop_spec.size) * oriented_height))
    left = max(0, min(left, oriented_width - 1))
    top = max(0, min(top, oriented_height - 1))
    right = max(left + 1, min(right, oriented_width))
    bottom = max(top + 1, min(bottom, oriented_height))
    cropped = rgb.crop((left, top, right, bottom))

    canonical_img = cropped.resize(
        (CANONICAL_SIZE, CANONICAL_SIZE), Image.LANCZOS
    )
    canonical_webp = _encode_within(
        canonical_img, CANONICAL_MAX_BYTES, CANONICAL_SIZE
    )

    compact_img = canonical_img.resize(
        (COMPACT_SIZE, COMPACT_SIZE), Image.LANCZOS
    )
    compact_webp = _encode_within(compact_img, COMPACT_MAX_BYTES, COMPACT_SIZE)

    data_uri = (
        f"data:{COMPACT_MIME};base64,"
        + base64.b64encode(compact_webp).decode("ascii")
    )
    if len(data_uri) > COMPACT_MAX_URI_CHARS:
        raise ProfileImageError(
            "output_too_large",
            "the compact icon could not be encoded within the size bound",
        )

    return ProcessedProfileImage(
        canonical_webp=canonical_webp,
        canonical_mime="image/webp",
        compact_webp=compact_webp,
        compact_data_uri=data_uri,
    )


def _to_srgb_rgb(img: Image.Image, icc_profile: bytes | None) -> Image.Image:
    """Return an ``RGB`` image in sRGB, flattening transparency onto white.

    An embedded ICC profile is converted to sRGB; without one the pixels are
    assumed already sRGB (the web default). Transparency is composited onto a
    white background so the square output is opaque and deterministic rather
    than carrying an alpha channel a downstream mask would have to interpret.
    """
    if icc_profile:
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
            dst = ImageCms.createProfile("sRGB")
            mode = "RGBA" if _has_alpha(img) else "RGB"
            img = ImageCms.profileToProfile(img, src, dst, outputMode=mode)
        except Exception:
            # A broken profile must not fail the upload; fall back to a plain
            # mode conversion, which treats the pixels as sRGB.
            pass

    if _has_alpha(img):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        return background
    return img.convert("RGB")


def _has_alpha(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info
    )


def _encode_within(img: Image.Image, max_bytes: int, expect_side: int) -> bytes:
    """Encode ``img`` as WebP within ``max_bytes``, quality-laddering down.

    Metadata is stripped: no ``exif``/``icc_profile`` is passed to ``save``, so
    the output carries only pixels. The dimension is asserted so a bug that
    changed the resize can never ship an off-size derivative silently. Raises
    :class:`ProfileImageError` (``output_too_large``) if even the lowest
    quality exceeds the bound.
    """
    assert img.size == (expect_side, expect_side), img.size
    for quality in _QUALITY_LADDER:
        buffer = io.BytesIO()
        img.save(
            buffer,
            format="WEBP",
            quality=quality,
            method=_WEBP_METHOD,
            exif=b"",
            icc_profile=None,
        )
        data = buffer.getvalue()
        if len(data) <= max_bytes:
            return data
    raise ProfileImageError(
        "output_too_large",
        "the normalized image could not be encoded within the size bound",
    )
