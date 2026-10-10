"""Backward-compatible import path for the shipped ``classic`` layout.

The renderer itself now lives where every other template lives:
``templates/classic/`` (``layout.py`` + ``template.toml``), discovered by
:func:`cv_generator.layouts.discover_templates`.  This module keeps the old
``cv_generator.render`` import path and the :func:`render_pdf` convenience
wrapper working for callers that only want "the normal CV PDF".
"""

from __future__ import annotations

from typing import Optional

from .layouts import DEFAULT_LAYOUT, LayoutOptions, LayoutOverflowError, render_layout
from .schema import CVData, PhotoCrop

__all__ = ["LayoutOverflowError", "render_pdf"]


def render_pdf(
    cv: CVData,
    photo_path: Optional[str] = None,
    photo_crop: Optional[PhotoCrop] = None,
) -> bytes:
    """Render ``cv`` with the shipped ``classic`` template.

    ``render_layout("classic", cv, options)`` is the registry-based equivalent;
    use it (or a template name of your own) to pick another layout.  The
    photo arguments override ``cv.photo`` and ``cv.photo_crop`` when given.
    """
    return render_layout(
        DEFAULT_LAYOUT,
        cv,
        LayoutOptions(photo_path=photo_path, photo_crop=photo_crop),
    )
