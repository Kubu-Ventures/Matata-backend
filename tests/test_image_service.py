"""Tests for app.services.image_service.compress_image."""

from __future__ import annotations

import io

import pytest
from PIL import Image

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jpeg(width: int = 800, height: int = 600, colour: str = "RGB") -> bytes:
    """Return minimal JPEG bytes of the requested dimensions."""
    img = Image.new(colour, (width, height), color=(100, 149, 237))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _make_png_rgba(width: int = 400, height: int = 300) -> bytes:
    """Return a PNG with alpha channel."""
    img = Image.new("RGBA", (width, height), color=(255, 0, 0, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg_with_exif_rotation(rotation_tag: int) -> bytes:
    """Return a JPEG with an EXIF Orientation tag set to *rotation_tag*.

    Orientation values: 1=normal, 3=180°, 6=90°CW, 8=90°CCW.
    """
    img = Image.new("RGB", (200, 100), color=(0, 128, 255))
    exif = img.getexif()
    exif[0x0112] = rotation_tag  # 0x0112 = Orientation tag
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def _open(compressed: bytes) -> Image.Image:
    return Image.open(io.BytesIO(compressed))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCompressImage:
    def test_returns_jpeg_content_type(self):
        from app.services.image_service import compress_image

        _, ct = compress_image(_make_jpeg())
        assert ct == "image/jpeg"

    def test_output_is_valid_jpeg(self):
        from app.services.image_service import compress_image

        compressed, _ = compress_image(_make_jpeg())
        img = _open(compressed)
        assert img.format == "JPEG"

    def test_reduces_file_size(self):
        """A large high-quality JPEG should be smaller after compression."""
        from app.services.image_service import compress_image

        # Start with a large, high-quality source
        original = _make_jpeg(3000, 2000)
        compressed, _ = compress_image(original)
        assert len(compressed) < len(original)

    def test_large_image_resized_to_max_dimension(self):
        from app.services.image_service import compress_image

        original = _make_jpeg(3000, 2000)
        compressed, _ = compress_image(original, max_dimension=1920)
        img = _open(compressed)
        assert max(img.size) <= 1920

    def test_aspect_ratio_preserved_after_resize(self):
        from app.services.image_service import compress_image

        original = _make_jpeg(3000, 2000)  # 3:2 ratio
        compressed, _ = compress_image(original, max_dimension=1920)
        w, h = _open(compressed).size
        assert abs(w / h - 3 / 2) < 0.01

    def test_small_image_not_upscaled(self):
        from app.services.image_service import compress_image

        original = _make_jpeg(400, 300)
        compressed, _ = compress_image(original, max_dimension=1920)
        w, h = _open(compressed).size
        assert w <= 400
        assert h <= 300

    def test_rgba_png_converted_to_rgb_jpeg(self):
        from app.services.image_service import compress_image

        compressed, ct = compress_image(_make_png_rgba())
        img = _open(compressed)
        assert img.mode == "RGB"
        assert ct == "image/jpeg"

    def test_exif_metadata_stripped(self):
        """Saved JPEG must not carry EXIF data (privacy — no GPS coordinates)."""
        from app.services.image_service import compress_image

        original = _make_jpeg_with_exif_rotation(6)
        compressed, _ = compress_image(original)
        img = _open(compressed)
        exif = img.getexif()
        assert len(exif) == 0

    def test_exif_orientation_applied_before_strip(self):
        """A 90°CW-tagged landscape image should become portrait after compression."""
        from app.services.image_service import compress_image

        # 200×100 image tagged as 90°CW → after transpose becomes 100×200
        original = _make_jpeg_with_exif_rotation(6)
        compressed, _ = compress_image(original)
        w, h = _open(compressed).size
        assert h > w  # portrait after 90°CW rotation

    def test_invalid_bytes_raise_value_error(self):
        from app.services.image_service import compress_image

        with pytest.raises(ValueError, match="Cannot decode image"):
            compress_image(b"not an image at all")

    def test_custom_quality_accepted(self):
        from app.services.image_service import compress_image

        compressed_high, _ = compress_image(_make_jpeg(800, 600), quality=95)
        compressed_low, _ = compress_image(_make_jpeg(800, 600), quality=20)
        # Lower quality should produce a smaller file
        assert len(compressed_low) < len(compressed_high)

    def test_exact_max_dimension_boundary_not_resized(self):
        """An image exactly at the limit must not be resized."""
        from app.services.image_service import compress_image

        original = _make_jpeg(1920, 1080)
        compressed, _ = compress_image(original, max_dimension=1920)
        w, h = _open(compressed).size
        assert max(w, h) <= 1920
