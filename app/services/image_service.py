"""Image processing — compression, resizing, and EXIF normalisation.

Why compress before storage?
-----------------------------
* Faster upload / download — smaller payload over the wire.
* Lower storage and egress costs.
* Privacy — EXIF metadata (GPS coordinates, device model, serial number)
  is stripped before the bytes reach object storage.
* Better UX for mobile reporters on low-bandwidth connections.

Processing order
----------------
Compression is intentionally applied *after* pHash computation and Stage 2
content moderation so that both operate on the highest-fidelity source bytes.
Only the compressed output is written to object storage.
"""

from __future__ import annotations

import io
import logging
from typing import Tuple

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# Maximum length of the longest side in pixels.  1920 px covers full-HD
# displays and is more than sufficient for damage-assessment use cases.
_MAX_DIMENSION: int = 1920

# JPEG quality level.  85 is the industry sweet-spot used by Google, Meta,
# and WhatsApp: visually lossless to the human eye while cutting file size
# by 60-80 % compared with an uncompressed original.
_JPEG_QUALITY: int = 85

# 4:4:4 chroma subsampling — best chroma quality at this quality level.
# Switch to 2 (4:2:0) if you need even smaller files and can tolerate
# slight colour fringing on fine edges.
_JPEG_SUBSAMPLING: int = 0


def compress_image(
    image_bytes: bytes,
    max_dimension: int = _MAX_DIMENSION,
    quality: int = _JPEG_QUALITY,
) -> Tuple[bytes, str]:
    """Compress and normalise an uploaded image for storage.

    Steps applied in order:

    1. **Decode** with Pillow (accepts JPEG, PNG, WebP, BMP, TIFF, …).
    2. **EXIF auto-rotate** — corrects orientation so the stored image is
       always upright, regardless of how the device was held at capture time.
    3. **Strip metadata** — Pillow drops all EXIF / IPTC / XMP data when
       saving to a fresh buffer, removing GPS coordinates and device info.
    4. **Resize** — downsample to *max_dimension* on the longest side when
       the image is larger, preserving aspect ratio with LANCZOS resampling.
       Images already within the limit are never upscaled.
    5. **Convert to RGB** — drops alpha channel for JPEG compatibility.
    6. **Re-encode as JPEG** at *quality* with ``optimize=True`` (lossless
       Huffman table optimisation, typically saves an additional 5-15 %).

    Args:
        image_bytes:   Raw bytes of the uploaded image.
        max_dimension: Longest-side pixel cap (default 1920).
        quality:       JPEG quality 0-95 (default 85).

    Returns:
        ``(compressed_bytes, "image/jpeg")``

    Raises:
        ValueError: If *image_bytes* cannot be decoded as a known image format.
    """
    try:
        raw: Image.Image = Image.open(io.BytesIO(image_bytes))
    except Exception as exc:
        raise ValueError(f"Cannot decode image: {exc}") from exc

    # Step 2 — apply EXIF orientation before any spatial operations.
    img: Image.Image = ImageOps.exif_transpose(raw)

    # Step 4 — resize only when necessary; never upscale.
    w, h = img.size
    if max(w, h) > max_dimension:
        scale = max_dimension / max(w, h)
        img = img.resize((round(w * scale), round(h * scale)), Image.Resampling.LANCZOS)

    # Step 5 — JPEG does not support transparency; convert palette/RGBA/etc.
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    # Step 6 — encode; saving to a new buffer implicitly strips all EXIF.
    buf = io.BytesIO()
    img.save(
        buf,
        format="JPEG",
        quality=quality,
        optimize=True,
        subsampling=_JPEG_SUBSAMPLING,
    )

    compressed = buf.getvalue()
    original_kb = len(image_bytes) / 1024
    compressed_kb = len(compressed) / 1024
    ratio = original_kb / compressed_kb if compressed_kb else 0
    logger.info(
        "image_compressed original_kb=%.1f compressed_kb=%.1f ratio=%.1fx",
        original_kb,
        compressed_kb,
        ratio,
    )

    return compressed, "image/jpeg"
