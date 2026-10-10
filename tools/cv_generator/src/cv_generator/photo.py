"""Photo loading: EXIF orientation + deterministic square crop, in memory only.

The original profile picture is never modified; a cropped copy is produced in
memory for embedding into the PDF.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps

from .schema import PhotoCrop


class PhotoError(ValueError):
    """Raised when the profile photo cannot be loaded or cropped."""


MAX_PHOTO_PX = 1200  # final square side in pixels (~404 DPI at 76 mm); keeps PDFs small


def load_photo(path: str | Path, crop: Optional[PhotoCrop] = None) -> Image.Image:
    """Return an upright, RGB, square photo ready for embedding.

    ``crop`` is a square box in source-pixel coordinates.  When omitted the
    image is cropped to the largest centered square.
    """
    path = Path(path)
    if not path.exists():
        raise PhotoError(f"profile photo not found: {path}")
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)  # apply EXIF orientation
    except Exception as exc:  # pragma: no cover - defensive
        raise PhotoError(f"cannot open profile photo {path}: {exc}") from exc
    img = img.convert("RGB")

    if crop is not None:
        left, top, size = crop.left, crop.top, crop.size
        max_left = max(0, img.width - 1)
        max_top = max(0, img.height - 1)
        if left > max_left or top > max_top:
            raise PhotoError(
                f"photo_crop top-left ({left},{top}) is outside image "
                f"{img.width}x{img.height}"
            )
        if left + size > img.width or top + size > img.height:
            raise PhotoError(
                f"photo_crop box ({left},{top},size={size}) exceeds image "
                f"{img.width}x{img.height}"
            )
        img = img.crop((left, top, left + size, top + size))
    else:
        side = min(img.width, img.height)
        left = (img.width - side) // 2
        top = (img.height - side) // 2
        img = img.crop((left, top, left + side, top + side))

    if max(img.width, img.height) > MAX_PHOTO_PX:
        img = img.resize((MAX_PHOTO_PX, MAX_PHOTO_PX), Image.LANCZOS)

    return img


def to_jpeg_image(img: Image.Image, quality: int = 90) -> Image.Image:
    """Re-encode as JPEG in memory; keeps ``format == "JPEG"`` so ReportLab
    embeds the compressed JPEG stream instead of a huge raw/Flate bitmap."""
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=False)
    buf.seek(0)
    return Image.open(buf)


def photo_to_jpeg_bytes(img: Image.Image, quality: int = 92) -> bytes:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=False)
    return buf.getvalue()


__all__ = ["load_photo", "photo_to_jpeg_bytes", "to_jpeg_image", "PhotoError"]
