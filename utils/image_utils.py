"""
Server-side image compression for shopkeeper-uploaded product photos.

The client enforces a 10MB cap before upload even starts, but that alone is
not a real size control (a 10MB image is still huge to store/serve). Every
shopkeeper upload is re-encoded here to WebP with a capped max dimension
before it ever touches storage, regardless of what the client sent.
"""
import io
import logging
from PIL import Image

logger = logging.getLogger(__name__)

MAX_DIMENSION = 1600      # full/detail image — long edge capped at this
THUMB_DIMENSION = 320     # list/table thumbnail — long edge capped at this
FULL_QUALITY = 82
THUMB_QUALITY = 72


def _load_rgb(contents: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(contents))
    img = img.convert("RGB")  # normalize (drops alpha/CMYK/palette weirdness)
    return img


def compress_to_webp(contents: bytes, max_dimension: int = MAX_DIMENSION, quality: int = FULL_QUALITY) -> bytes:
    """Re-encodes any input image to WebP, capping the long edge at
    max_dimension. This runs on every shopkeeper upload — the 10MB
    client-side limit is just a first line of defense, not the real control."""
    img = _load_rgb(contents)
    w, h = img.size
    if max(w, h) > max_dimension:
        ratio = max_dimension / max(w, h)
        img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=quality, method=6)
    return buf.getvalue()


def make_thumbnail_webp(contents: bytes, size: int = THUMB_DIMENSION, quality: int = THUMB_QUALITY) -> bytes:
    """Small WebP thumbnail for admin list/table views, so those pages never
    have to pull down the full-size image just to render a row."""
    img = _load_rgb(contents)
    img.thumbnail((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=quality, method=6)
    return buf.getvalue()


def compress_and_thumbnail(contents: bytes) -> tuple[bytes, bytes]:
    """Returns (full_webp_bytes, thumb_webp_bytes) for one uploaded image."""
    full = compress_to_webp(contents)
    thumb = make_thumbnail_webp(contents)
    return full, thumb