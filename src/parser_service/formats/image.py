"""
image.py — parse one image (a png or jpeg unit) as one page, like a scanned PDF page.

A blank image (almost no pixel darker than near-white) is ``ok`` with route
``blank``, as a blank PDF page is. Any other image goes through
``parse_to_markdown``: Docling OCR, the quality gate, and the escalation engine when
the gate promotes it. ``pdf.classify_page_result`` maps the result to a unit state,
so an image is ``ok``, ``degraded`` or ``failed`` by the same rules as a PDF page.

The page service's image guard (``guards.check_image``) has already refused an
image that declares more than ``PARSER_IMAGE_MAX_MEGAPIXELS``, so decoding it here
is bounded.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from parser_service.formats.pdf import (
    ROUTE_BLANK,
    classify_page_result,
    is_blank_bitmap,
    page_fields,
)
from parser_service.formats.result import STATE_OK
from parser_service.observability import truncate
from parser_service.parsing.document import parse_to_markdown

logger = logging.getLogger(__name__)

# The blank probe's size: the long side of an A4 page at the PDF probe's 36 DPI.
_BLANK_PROBE_SIDE = 420


def _is_blank_image(path: Path) -> bool:
    """True iff the image, flattened onto white, is (almost) entirely white.

    Never raises: any probe failure means "not blank" and the image is parsed.
    """
    try:
        from PIL import Image

        with Image.open(path) as image:
            # A JPEG decodes straight at a fraction of its size; other formats ignore this.
            image.draft("RGB", (_BLANK_PROBE_SIDE, _BLANK_PROBE_SIDE))
            if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
                rgba = image.convert("RGBA")
                flat = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                frame = Image.alpha_composite(flat, rgba)
            else:
                frame = image.convert("RGB")
        frame.thumbnail((_BLANK_PROBE_SIDE, _BLANK_PROBE_SIDE))
        return is_blank_bitmap(frame)
    except Exception as exc:  # noqa: BLE001
        logger.warning("blank-image probe failed for %s: %s", path.name, truncate(str(exc)))
        return False


def parse_image(
    path: Path, page_number: int, escalate: bool
) -> tuple[str, str | None, str, dict[str, Any]]:
    """Probe and parse one image: ``(state, reason, markdown, record fields)``."""
    if _is_blank_image(path):
        return STATE_OK, None, "", {"route": ROUTE_BLANK, "route_reason": "blank_image"}
    result = parse_to_markdown(path, escalate=escalate)
    state, reason = classify_page_result(result)
    return state, reason, str(result.get("markdown", "")), page_fields(result, page_number - 1)
