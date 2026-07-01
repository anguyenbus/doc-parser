"""
render.py

PDF page rendering using pypdfium2.

Public API:
  render_page(pdf_path, page_no, dpi=144) -> bytes    # full page PNG
  render_region(pdf_path, page_no, bbox, dpi=144) -> bytes  # cropped PNG
  text_layer_tokens(pdf_path) -> dict[int, int]       # per-page embedded-text token counts
  page_has_images(page) -> bool                        # pypdf has-images probe (no decode)
  classify_for_fastpath(pdf_path) -> dict              # all-scanned fast-path classifier

Coordinate system:
  PDF uses bottom-left origin in points (1/72 inch).
  pypdfium2 renders to images with top-left origin in pixels.
  render_region performs the conversion; see inline comments for the formula.

# Coordinate conversion verified visually on 2026-05-28 with digital_simple.pdf
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Sentinel value: when caller passes dpi=144 (the default), read from env var.
_DEFAULT_DPI = 144

# Megapixel ceiling for any rasterized page. pypdfium2 allocates the bitmap
# itself (PIL's MAX_IMAGE_PIXELS bomb check does NOT cover a buffer wrapped by
# .to_pil()), so we must clamp the render `scale` BEFORE rendering. Overridable
# via PARSER_MAX_RENDER_MP. Default 40 MP leaves Letter/A4/A1 untouched while
# downscaling pathological or large-format pages (A0 @ 144 DPI ~= 320 MP).
_DEFAULT_MAX_RENDER_MP = 40.0


def _max_render_pixels() -> int:
    """Return the max rendered-bitmap pixel budget (reads PARSER_MAX_RENDER_MP, in MP)."""
    mp = float(os.environ.get("PARSER_MAX_RENDER_MP", str(_DEFAULT_MAX_RENDER_MP)))
    return int(mp * 1_000_000)


def _clamp_scale_to_budget(
    width_pts: float, height_pts: float, scale: float, max_pixels: int
) -> tuple[float, bool]:
    """Reduce ``scale`` so ``width_pts * height_pts * scale**2 <= max_pixels``.

    Returns ``(effective_scale, was_clamped)``. Pure function — performs NO
    allocation, so it is safe to call with adversarially large page dimensions.
    Degenerate inputs (non-positive area or budget) pass the scale through
    unchanged rather than dividing by zero.
    """
    area_pts = width_pts * height_pts
    if area_pts <= 0 or max_pixels <= 0:
        return scale, False
    projected = area_pts * scale * scale
    if projected <= max_pixels:
        return scale, False
    return scale * (max_pixels / projected) ** 0.5, True


def _clamp_render_scale(page: Any, scale: float, pdf_path: Path, page_no: int) -> float:
    """Clamp ``scale`` to the megapixel budget using the page's point dimensions.

    Reads the page size and reduces ``scale`` BEFORE pdfium allocates the bitmap.
    Logs a warning when a clamp occurs so operators can see downscaled pages.
    """
    max_pixels = _max_render_pixels()
    width_pts = float(page.get_width())
    height_pts = float(page.get_height())
    effective_scale, clamped = _clamp_scale_to_budget(width_pts, height_pts, scale, max_pixels)
    if clamped:
        logger.warning(
            "Downscaled render of %s page %d: %.0fx%.0f pts at scale %.4f exceeds "
            "%d px budget; rendering at scale %.4f",
            pdf_path.name,
            page_no,
            width_pts,
            height_pts,
            scale,
            max_pixels,
            effective_scale,
        )
    return effective_scale


def text_layer_tokens(pdf_path: Path) -> dict[int, int]:
    """Return {page_index: whitespace-token count} of the PDF's embedded text
    layer. Used by the quality gate's coverage check to detect Docling
    under-extraction. Pages with no text layer (scanned/image) return ~0 and are
    handled by the Docling-confidence layer instead. Never raises."""
    import pypdfium2 as pdfium  # local import; only needed for PDFs

    counts: dict[int, int] = {}
    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("text_layer_tokens: could not open %s: %s", pdf_path, exc)
        return counts
    try:
        for i in range(len(pdf)):
            try:
                text = pdf[i].get_textpage().get_text_range()
            except Exception:  # noqa: BLE001
                text = ""
            counts[i] = len(text.split())
    finally:
        pdf.close()
    return counts


def page_has_images(page: Any) -> bool:
    """Return True iff a pypdf page has any embedded raster image.

    A resource-level probe: walks the page's ``/Resources /XObject`` dict for an
    entry whose ``/Subtype`` is ``/Image``, RECURSING into ``/Form`` XObjects
    (a form has its own ``/Resources`` and can nest image XObjects). Returns a
    boolean and NEVER decodes pixels — ``page.images`` decodes and is unnecessary
    for a has-images check.

    pypdf (BSD-3) is used deliberately: PyMuPDF's ``get_images`` is the correct
    resource-level enumeration but is AGPL (banned here), and pypdfium2's
    ``get_objects`` walks *painted content* objects, MISSING resource-declared
    images — it reports 0 on ``tests/fixtures/scanned.pdf``, which in fact has two
    ``/XObject /Image`` entries pypdf finds (verified 2026-07-01). Never raises;
    a malformed resource tree yields False.
    """
    try:
        return _resources_have_image(page.get("/Resources"), depth=0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("page_has_images: resource walk failed: %s", exc)
        return False


# Bound the /Form recursion so a self-referential/cyclic resource tree cannot loop
# forever. Real nesting is shallow; anything deeper is pathological.
_MAX_XOBJECT_DEPTH = 32


def _resources_have_image(resources: Any, depth: int) -> bool:
    """Recursively test whether a ``/Resources`` dict declares an image XObject.

    Checks each ``/XObject`` entry's ``/Subtype``: ``/Image`` short-circuits True;
    ``/Form`` recurses into the form's own ``/Resources``. Bounded by
    ``_MAX_XOBJECT_DEPTH`` against cyclic references.
    """
    if resources is None or depth > _MAX_XOBJECT_DEPTH:
        return False
    resources = resources.get_object()
    xobjects = resources.get("/XObject")
    if xobjects is None:
        return False
    xobjects = xobjects.get_object()
    for key in xobjects:
        obj = xobjects[key].get_object()
        subtype = str(obj.get("/Subtype"))
        if subtype == "/Image":
            return True
        if subtype == "/Form" and _resources_have_image(obj.get("/Resources"), depth + 1):
            return True
    return False


def classify_for_fastpath(pdf_path: Path) -> dict[str, Any]:
    """Classify a PDF for the all-scanned Docling fast-path (PURE — no convert).

    A page QUALIFIES for the fast-path iff its embedded-text token count is zero
    AND it has an embedded image: ``text_layer_tokens[page] == 0 AND
    page_has_images(page)``. This is the exact, non-relaxable qualify rule — the
    ambiguous middle (any embedded text at all) must keep Docling + its fallback,
    and a truly-empty page (no text, no images) has nothing for escalation to
    recover so it does NOT qualify.

    Returns ``{"skip_docling": bool, "page_qualifies": dict[int, bool]}``. The
    document skips ``DocumentConverter.convert()`` iff EVERY page qualifies (the
    Docling API is whole-document, so only an all-scanned doc can skip convert).

    Pure: reuses ``text_layer_tokens`` and the ``page_has_images`` pypdf probe;
    it does NOT construct Docling, run the gate, or call the escalation engine.
    Never raises — a probe failure degrades to ``skip_docling=False`` (normal path).
    """
    from pypdf import PdfReader  # local import; only needed for PDFs

    tokens = text_layer_tokens(pdf_path)

    page_qualifies: dict[int, bool] = {}
    try:
        reader = PdfReader(str(pdf_path))
        for idx, page in enumerate(reader.pages):
            page_qualifies[idx] = tokens.get(idx, 0) == 0 and page_has_images(page)
    except Exception as exc:  # noqa: BLE001
        logger.warning("classify_for_fastpath: could not classify %s: %s", pdf_path, exc)
        return {"skip_docling": False, "page_qualifies": {}}

    skip_docling = bool(page_qualifies) and all(page_qualifies.values())
    return {"skip_docling": skip_docling, "page_qualifies": page_qualifies}


def render_page(pdf_path: Path, page_no: int, dpi: int = _DEFAULT_DPI) -> bytes:
    """Render a full PDF page to PNG bytes at the given DPI.

    Args:
        pdf_path: Absolute path to the PDF file.
        page_no: Zero-indexed page number.
        dpi: Rasterization resolution; reads PARSER_RENDER_DPI env var when
             the caller passes the default sentinel (144).

    Returns:
        PNG bytes of the full page.
    """
    import pypdfium2 as pdfium

    effective_dpi = (
        int(os.environ.get("PARSER_RENDER_DPI", str(dpi))) if dpi == _DEFAULT_DPI else dpi
    )
    scale = effective_dpi / 72.0

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page = pdf[page_no]
        scale = _clamp_render_scale(page, scale, pdf_path, page_no)
        pil = page.render(scale=scale).to_pil()
    finally:
        pdf.close()

    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    return buf.getvalue()


def render_region(
    pdf_path: Path,
    page_no: int,
    bbox: dict[str, Any],
    dpi: int = _DEFAULT_DPI,
) -> bytes:
    """Render a cropped region of a PDF page to PNG bytes.

    # Coordinate conversion verified visually on 2026-05-28 with digital_simple.pdf

    Args:
        pdf_path: Absolute path to the PDF file.
        page_no: Zero-indexed page number.
        bbox: {"x0": float, "y0": float, "x1": float, "y1": float} in PDF
              coordinate space (bottom-left origin, units = points).
        dpi: Rasterization resolution.

    Returns:
        PNG bytes of the cropped region.

    Raises:
        ValueError: If the resulting crop has zero width or height.

    Coordinate conversion (PDF bottom-left → image top-left):
        s = dpi / 72.0
        img_left   = x0 * s
        img_right  = x1 * s
        img_top    = (page_height_pts - y1) * s   # PDF y1 (top of box) → img top
        img_bottom = (page_height_pts - y0) * s   # PDF y0 (bottom of box) → img bottom
    """
    import pypdfium2 as pdfium

    effective_dpi = (
        int(os.environ.get("PARSER_RENDER_DPI", str(dpi))) if dpi == _DEFAULT_DPI else dpi
    )
    scale = effective_dpi / 72.0

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page = pdf[page_no]
        page_height_pts = page.get_height()
        # Clamp BEFORE rendering; the clamped scale is reused below for the
        # PDF→image coordinate conversion so the crop stays correct (just lower
        # resolution on a downscaled page).
        scale = _clamp_render_scale(page, scale, pdf_path, page_no)
        pil = page.render(scale=scale).to_pil()
    finally:
        pdf.close()

    rendered_width_px, rendered_height_px = pil.size

    # Convert PDF bottom-left origin coords to image top-left origin pixels.
    img_left = bbox["x0"] * scale
    img_right = bbox["x1"] * scale
    img_top = (page_height_pts - bbox["y1"]) * scale
    img_bottom = (page_height_pts - bbox["y0"]) * scale

    # Clamp to rendered image bounds.
    img_left = max(0.0, min(img_left, rendered_width_px))
    img_right = max(0.0, min(img_right, rendered_width_px))
    img_top = max(0.0, min(img_top, rendered_height_px))
    img_bottom = max(0.0, min(img_bottom, rendered_height_px))

    if img_right <= img_left or img_bottom <= img_top:
        raise ValueError(
            f"zero-dimension crop after coordinate conversion: "
            f"left={img_left}, right={img_right}, top={img_top}, bottom={img_bottom} "
            f"(page_height_pts={page_height_pts}, bbox={bbox})"
        )

    cropped = pil.crop((int(img_left), int(img_top), int(img_right), int(img_bottom)))
    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return buf.getvalue()
