"""
parser_service.py

Routes a file to the correct parsing path and assembles a dict conforming
to eval-harness parser_output.schema.json v1.0.0.

# Element type enum (from schema):
#   heading, paragraph, list, list_item, table, figure, caption, footnote,
#   header, footer, page_number, code_block, equation

Routing rules:
  - PDF (digital — text-bearing pages):
        Docling for all items; VLM crop for each TableItem; scanned-page
        fallback for pages with zero Docling elements.
  - PDF (scanned — image-only pages):
        Docling yields nothing; whole-page VLM fallback on those pages.
  - Image (PNG/JPEG/TIFF):
        VLM once in page mode on raw file bytes; no Docling.
  - DOCX / XLSX / HTML:
        Docling only; VLM never called; one logical page emitted.
  - Unknown:
        unsupported_type warning; empty output skeleton returned.

Failure modes go into warnings[] — parse() never raises.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from parser_service.parsing.elements import (
    append_warning,
    bbox_of,
    classify_by_extension,
    docling_item_to_element,
    emit_vlm_elements,
    empty_output,
    input_size_error,
)
from parser_service.parsing.engines.vlm import (
    call_vlm,
    reset_vlm_call_count,
)

logger = logging.getLogger(__name__)


class ParserOutputModel(BaseModel):
    schema_version: str
    parser_version: str
    parsed_at: str
    source: dict[str, Any]
    pages: list[dict[str, Any]]
    elements: list[dict[str, Any]]
    warnings: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse(file_path: Path) -> dict[str, Any]:
    """Parse a document and return a parser_output.schema.json-conformant dict.

    Never raises. All failures are captured in warnings[].

    Args:
        file_path: Path to the document to parse.

    Returns:
        Dict conforming to parser_output.schema.json v1.0.0.
    """
    file_path = Path(file_path).resolve()
    reset_vlm_call_count()

    mime = mimetypes.guess_type(str(file_path))[0] or ""
    kind = classify_by_extension(file_path, mime)

    # Reject oversized input before any Docling/render work. empty_output streams
    # the hash (~1 MiB RAM), so building the skeleton for a rejected file is cheap;
    # the guard's value is short-circuiting the ~1.6 GB Docling working set.
    size_error = input_size_error(file_path)
    out = empty_output(file_path, mime)
    if size_error is not None:
        append_warning(out, code="input_too_large", message=size_error, scope="document")
        return out

    # Running char_span counter — initialized here, passed through all paths.
    char_offset = 0

    try:
        if kind == "unknown":
            append_warning(
                out,
                code="unsupported_type",
                message=f"Unsupported file type: extension={file_path.suffix!r}, mime={mime!r}",
                scope="document",
            )
            return out

        if kind == "pdf":
            out, char_offset = _parse_pdf(file_path, out, char_offset)
        elif kind == "image":
            out, char_offset = _parse_image(file_path, out, char_offset)
        elif kind in ("docx", "xlsx", "html"):
            out, char_offset = _parse_office_or_html(file_path, out, kind, char_offset)

    except Exception as exc:
        logger.exception("Unexpected failure parsing %s", file_path)
        append_warning(
            out,
            code="unhandled_exception",
            message=str(exc),
            scope="document",
        )
        return out

    # Order elements by (page, reading order) and renumber char spans so mixed
    # Docling/VLM multi-page documents read in page order.
    _reorder_elements(out)

    # Validate with Pydantic before returning.
    try:
        ParserOutputModel(**out)
    except Exception as exc:
        logger.warning("Output validation failed for %s: %s", file_path.name, exc)
        append_warning(
            out,
            code="unhandled_exception",
            message=f"Output validation error: {exc}",
            scope="document",
        )

    return out


def _reorder_elements(out: dict[str, Any]) -> None:
    """Order elements by (page_index, reading order) and renumber char spans.

    Docling items arrive in reading order, but VLM / scanned-page elements are
    appended *after* all Docling-kept elements — so a mixed multi-page document
    would place its VLM pages at the end instead of in page position. Sorting by
    ``(page_index, char_span start)`` restores page order (page_index is the
    primary key because char_span reflects append order, not page order); spans
    are then renumbered to stay contiguous and monotonic.
    """
    elements = out.get("elements")
    if not elements:
        return
    elements.sort(key=lambda e: (e.get("page_index", 0), (e.get("char_span") or [0])[0]))
    offset = 0
    for e in elements:
        text = e.get("text", "") or ""
        e["char_span"] = [offset, offset + len(text)]
        offset += len(text)


# ---------------------------------------------------------------------------
# PDF routing path
# ---------------------------------------------------------------------------


def _parse_pdf(path: Path, out: dict[str, Any], char_offset: int) -> tuple[dict[str, Any], int]:
    """Digital + scanned PDF routing path.

    Runs Docling, maps elements, crops and VLM-processes each TableItem,
    then runs the scanned-page fallback loop on pages with zero elements.
    """
    from docling.document_converter import DocumentConverter

    from parser_service.parsing.quality_gate import evaluate_page
    from parser_service.parsing.render import render_page, render_region, text_layer_tokens

    converter = DocumentConverter()
    try:
        result = converter.convert(str(path))
    except Exception as exc:
        logger.warning("Docling conversion failed for %s: %s", path.name, exc)
        append_warning(out, "docling_failed", str(exc), scope="document")
        return out, char_offset

    doc = result.document

    # Build pages list from Docling page metadata.
    max_pages = int(__import__("os").environ.get("PARSER_MAX_PAGES", "2000"))
    for page_no, page in enumerate(getattr(doc, "pages", {}).values()):
        if page_no >= max_pages:
            append_warning(
                out,
                "page_unparseable",
                f"Page {page_no} exceeds PARSER_MAX_PAGES={max_pages}",
                scope="page",
                page_index=page_no,
            )
            break
        size = getattr(page, "size", None)
        out["pages"].append(
            {
                "page_index": page_no,
                "width": float(getattr(size, "width", 0)) if size else 0.0,
                "height": float(getattr(size, "height", 0)) if size else 0.0,
                "rotation": 0,
            }
        )

    pages_with_text: set[int] = set()

    # Walk all items in reading order.
    # traverse_pictures=True: text items nested inside PictureItem containers
    # (common for image-based pages) are returned.
    # FURNITURE layer: includes page headers/footers Docling separates from body.
    from docling_core.types.doc.document import ContentLayer as _CL

    _layers = {_CL.BODY, _CL.FURNITURE}
    for item, _level in doc.iterate_items(traverse_pictures=True, included_content_layers=_layers):
        elem, char_offset = docling_item_to_element(item, char_offset, out)
        if elem is None:
            continue

        page_idx = elem["page_index"]

        # For tables: try VLM crop first; fall back to Docling extraction on failure.
        if elem["type"] == "table":
            bbox = bbox_of(item)
            if bbox is not None:
                try:
                    crop_bytes = render_region(path, page_idx, bbox)
                    vlm_result = call_vlm(crop_bytes, mode="table")
                    if "error" not in vlm_result and _looks_like_table(vlm_result):
                        elem["content"] = {
                            "kind": "table",
                            "rows": vlm_result["rows"],
                            "cols": vlm_result["cols"],
                            "header_rows": vlm_result.get("header_rows", 1),
                            "cells": vlm_result.get("cells", []),
                        }
                    else:
                        append_warning(
                            out,
                            "vlm_table_fallback",
                            f"VLM failed for table on page {page_idx}; using Docling extraction",
                            scope="element",
                            element_id=elem["element_id"],
                        )
                except Exception as exc:
                    logger.warning("Table VLM crop failed: %s", exc)
                    append_warning(
                        out,
                        "vlm_table_fallback",
                        f"Render/VLM error for table on page {page_idx}: {exc}",
                        scope="element",
                        element_id=elem["element_id"],
                    )
            else:
                # No bbox available — keep Docling's extraction silently.
                pass

        out["elements"].append(elem)
        pages_with_text.add(page_idx)

    # Quality-gate pass: pages that Docling parsed but with low-quality text
    # are promoted to VLM (Layer 1 or Layer 2 fires). Pages with zero elements
    # fall through to the scanned-page loop below unchanged.
    # Per-page embedded-text token counts (for the gate's coverage check).
    try:
        raw_tokens = text_layer_tokens(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("text_layer_tokens failed for %s: %s", path.name, exc)
        raw_tokens = {}

    pages_to_promote: set[int] = set()
    for page in out["pages"]:
        page_idx = page["page_index"]
        if page_idx not in pages_with_text:
            continue  # handled by scanned-page loop
        page_elems = [e for e in out["elements"] if e.get("page_index") == page_idx]
        decision = evaluate_page(
            page_idx,
            result,
            page_elems,
            page_text_layer_tokens=raw_tokens.get(page_idx),
        )
        if decision.action == "promote_to_vlm":
            pages_to_promote.add(page_idx)
            append_warning(
                out,
                "vlm_promoted",
                f"Layer {decision.layer}: {decision.reason}",
                scope="page",
                page_index=page_idx,
            )

    # Strip Docling elements for promoted pages and remove from pages_with_text
    # so they fall into the VLM loop below. Keep the stripped Docling elements so
    # we can fall back to them if the VLM fails or returns nothing.
    promoted_docling: dict[int, list[dict[str, Any]]] = {}
    if pages_to_promote:
        for idx in pages_to_promote:
            promoted_docling[idx] = [e for e in out["elements"] if e.get("page_index") == idx]
        out["elements"] = [
            e for e in out["elements"] if e.get("page_index") not in pages_to_promote
        ]
        pages_with_text -= pages_to_promote

    # Scanned-page fallback + promoted pages: render whole page and call VLM.
    for page in out["pages"]:
        page_idx = page["page_index"]
        if page_idx in pages_with_text:
            continue

        try:
            page_bytes = render_page(path, page_idx)
        except Exception as exc:
            logger.warning("render_page failed for page %d: %s", page_idx, exc)
            append_warning(
                out,
                "page_unparseable",
                f"Could not render page {page_idx}: {exc}",
                scope="page",
                page_index=page_idx,
            )
            continue

        # Fall back to the page's Docling elements (if any) when the VLM fails or
        # returns nothing — better a Docling parse than an empty page.
        fallback = promoted_docling.get(page_idx)

        def _keep_docling(
            code: str,
            msg: str,
            fallback: list[dict[str, Any]] | None = fallback,
            page_idx: int = page_idx,
        ) -> None:
            if fallback:
                out["elements"].extend(fallback)
                append_warning(
                    out,
                    "vlm_fallback_docling",
                    f"VLM {msg} on page {page_idx}; kept Docling output",
                    scope="page",
                    page_index=page_idx,
                )
            else:
                append_warning(out, code, msg, scope="page", page_index=page_idx)

        vlm_result = call_vlm(page_bytes, mode="page")

        if "error" in vlm_result:
            _keep_docling("page_unparseable", vlm_result["error"])
            continue

        elements_raw = vlm_result.get("elements")
        if not isinstance(elements_raw, list):
            _keep_docling("vlm_invalid_shape", "returned non-list 'elements'")
            continue

        # Buffer VLM elements so we can discard them and fall back if empty.
        buf: dict[str, Any] = {"elements": []}
        local_offset = char_offset
        for i, raw_elem in enumerate(elements_raw):
            if not isinstance(raw_elem, dict):
                continue
            local_offset = emit_vlm_elements(buf, raw_elem, page_idx, i, local_offset)

        if buf["elements"]:
            out["elements"].extend(buf["elements"])
            char_offset = local_offset
        else:
            _keep_docling("page_unparseable", "returned no usable elements")

    return out, char_offset


# ---------------------------------------------------------------------------
# Image routing path
# ---------------------------------------------------------------------------


def _parse_image(path: Path, out: dict[str, Any], char_offset: int) -> tuple[dict[str, Any], int]:
    """Image (PNG/JPEG/TIFF) routing path.

    1. Docling OCR extracts elements.
    2. Two-layer quality gate (quality_gate.py) decides whether to keep or promote to VLM.
    3. VLM replaces Docling output only when a layer fires (or Docling extracted nothing).
    """
    from parser_service.parsing.quality_gate import evaluate_page

    out["pages"].append({"page_index": 0, "width": 0, "height": 0, "rotation": 0})

    try:
        from docling.document_converter import DocumentConverter

        converter = DocumentConverter()
        conversion_result = converter.convert(str(path))
        doc = conversion_result.document
    except Exception as exc:
        append_warning(out, "docling_failed", str(exc), scope="document")
        return out, char_offset

    before = len(out["elements"])
    from docling_core.types.doc.document import ContentLayer as _CL

    _layers = {_CL.BODY, _CL.FURNITURE}
    for item, _ in doc.iterate_items(traverse_pictures=True, included_content_layers=_layers):
        elem, char_offset = docling_item_to_element(item, char_offset, out)
        if elem is not None:
            out["elements"].append(elem)

    page_elements = out["elements"][before:]
    decision = evaluate_page(0, conversion_result, page_elements)

    if decision.action == "keep":
        return out, char_offset

    # Promote to VLM — strip Docling's elements for this page first
    del out["elements"][before:]
    char_offset -= sum(len(e.get("text", "")) for e in page_elements)

    append_warning(
        out,
        "vlm_promoted",
        f"Layer {decision.layer}: {decision.reason}",
        scope="page",
        page_index=0,
    )

    image_bytes = path.read_bytes()
    vlm_result = call_vlm(image_bytes, mode="page")

    if "error" in vlm_result:
        append_warning(out, "image_unparseable", vlm_result["error"], scope="page", page_index=0)
        return out, char_offset

    elements_raw = vlm_result.get("elements")
    if not isinstance(elements_raw, list):
        append_warning(
            out,
            "vlm_invalid_shape",
            "VLM image response 'elements' is not a list",
            scope="page",
            page_index=0,
        )
        return out, char_offset

    for i, raw_elem in enumerate(elements_raw):
        if not isinstance(raw_elem, dict):
            continue
        char_offset = emit_vlm_elements(out, raw_elem, 0, i, char_offset)

    return out, char_offset


# ---------------------------------------------------------------------------
# DOCX / XLSX / HTML routing path
# ---------------------------------------------------------------------------


def _parse_office_or_html(
    path: Path, out: dict[str, Any], kind: str, char_offset: int
) -> tuple[dict[str, Any], int]:
    """DOCX/XLSX/HTML routing path. Docling only, one logical page, no VLM."""
    from docling.document_converter import DocumentConverter

    # Emit exactly one logical page for these formats.
    out["pages"].append({"page_index": 0, "width": 0, "height": 0, "rotation": 0})

    converter = DocumentConverter()
    try:
        result = converter.convert(str(path))
    except Exception as exc:
        logger.warning("Docling conversion failed for %s: %s", path.name, exc)
        append_warning(out, "docling_failed", str(exc), scope="document")
        return out, char_offset

    doc = result.document

    from docling_core.types.doc.document import ContentLayer as _CL

    _layers = {_CL.BODY, _CL.FURNITURE}
    for item, _level in doc.iterate_items(traverse_pictures=True, included_content_layers=_layers):
        elem, char_offset = docling_item_to_element(item, char_offset, out)
        if elem is not None:
            out["elements"].append(elem)

    return out, char_offset


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _looks_like_table(d: Any) -> bool:
    """Return True if d has the minimum required table fields."""
    return (
        isinstance(d, dict)
        and isinstance(d.get("rows"), int)
        and isinstance(d.get("cols"), int)
        and isinstance(d.get("cells"), list)
    )
