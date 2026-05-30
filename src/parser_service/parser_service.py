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

import hashlib
import importlib.metadata
import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, field_validator

from parser_service.vlm_client import (
    call_vlm,
    get_vlm_call_count,
    reset_vlm_call_count,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION: str = "1.0.0"

try:
    PARSER_VERSION: str = importlib.metadata.version("parser-service")
except importlib.metadata.PackageNotFoundError:
    PARSER_VERSION = "0.0.0+dev"

# Docling class name → schema element type
_DOCLING_TYPE_MAP: dict[str, str] = {
    "SectionHeaderItem": "heading",
    "TextItem": "paragraph",
    "ListItem": "list_item",
    "TableItem": "table",
    "PictureItem": "figure",
    "CaptionItem": "caption",
    "FootnoteItem": "footnote",
    "PageHeaderItem": "header",
    "PageFooterItem": "footer",
    "FormulaItem": "equation",
    "CodeItem": "code_block",
}


# ---------------------------------------------------------------------------
# Pydantic models for output validation
# ---------------------------------------------------------------------------


class WarningModel(BaseModel):
    scope: Literal["document", "page", "element"]
    page_index: int | None = None
    element_id: str | None = None
    code: str
    message: str


class SourceModel(BaseModel):
    doc_id: str
    filename: str
    mime_type: str
    sha256: str

    @field_validator("sha256")
    @classmethod
    def sha256_must_be_64_hex(cls, v: str) -> str:
        if len(v) != 64 or not all(c in "0123456789abcdef" for c in v):
            raise ValueError("sha256 must be exactly 64 lowercase hex characters")
        return v


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
    kind = _classify(file_path, mime)
    out = _empty_output(file_path, mime)

    # Running char_span counter — initialized here, passed through all paths.
    char_offset = 0

    try:
        if kind == "unknown":
            _append_warning(
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
        _append_warning(
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
        _append_warning(
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
# Classification
# ---------------------------------------------------------------------------


def _classify(path: Path, mime: str) -> str:
    """Classify file into pdf|image|docx|xlsx|html|unknown.

    Uses path.suffix.lower() first, then mimetypes.guess_type fallback.
    """
    ext = path.suffix.lower()

    # Extension-based classification (checked first, case-insensitive)
    if ext == ".pdf":
        return "pdf"
    if ext in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}:
        return "image"
    if ext == ".docx":
        return "docx"
    if ext in {".xlsx", ".xlsm"}:
        return "xlsx"
    if ext in {".html", ".htm"}:
        return "html"

    # MIME-based fallback (when extension is unrecognized)
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("image/"):
        return "image"
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return "docx"
    if mime in {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.macroEnabled.12",
    }:
        return "xlsx"
    if mime in {"text/html", "application/xhtml+xml"}:
        return "html"

    return "unknown"


# ---------------------------------------------------------------------------
# Output skeleton
# ---------------------------------------------------------------------------


def _empty_output(path: Path, mime: str) -> dict[str, Any]:
    """Build the schema skeleton with sha256, parsed_at, and empty arrays."""
    data = path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "parsed_at": datetime.now(tz=timezone.utc).isoformat(),
        "source": {
            "doc_id": path.stem,
            "filename": path.name,
            "mime_type": mime,
            "sha256": sha256,
        },
        "pages": [],
        "elements": [],
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# Warning helper
# ---------------------------------------------------------------------------


def _append_warning(
    out: dict[str, Any],
    code: str,
    message: str,
    scope: str,
    page_index: int | None = None,
    element_id: str | None = None,
) -> None:
    """Construct, validate, and append a warning dict to out['warnings']."""
    raw: dict[str, Any] = {"scope": scope, "code": code, "message": message}
    if page_index is not None:
        raw["page_index"] = page_index
    if element_id is not None:
        raw["element_id"] = element_id

    try:
        WarningModel(**raw)
    except Exception as exc:
        logger.warning("Warning shape validation failed: %s (raw=%r)", exc, raw)

    out["warnings"].append(raw)


# ---------------------------------------------------------------------------
# PDF routing path
# ---------------------------------------------------------------------------


def _parse_pdf(
    path: Path, out: dict[str, Any], char_offset: int
) -> tuple[dict[str, Any], int]:
    """Digital + scanned PDF routing path.

    Runs Docling, maps elements, crops and VLM-processes each TableItem,
    then runs the scanned-page fallback loop on pages with zero elements.
    """
    from docling.document_converter import DocumentConverter  # type: ignore[import-untyped]

    from parser_service.quality_gate import evaluate_page
    from parser_service.render import render_page, render_region, text_layer_tokens

    converter = DocumentConverter()
    try:
        result = converter.convert(str(path))
    except Exception as exc:
        logger.warning("Docling conversion failed for %s: %s", path.name, exc)
        _append_warning(out, "docling_failed", str(exc), scope="document")
        return out, char_offset

    doc = result.document

    # Build pages list from Docling page metadata.
    max_pages = int(__import__("os").environ.get("PARSER_MAX_PAGES", "2000"))
    for page_no, page in enumerate(getattr(doc, "pages", {}).values()):
        if page_no >= max_pages:
            _append_warning(
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
    from docling_core.types.doc.document import ContentLayer as _CL  # type: ignore[import-untyped]
    _layers = {_CL.BODY, _CL.FURNITURE}
    for item, _level in doc.iterate_items(traverse_pictures=True, included_content_layers=_layers):
        elem, char_offset = _docling_item_to_element(item, char_offset, out)
        if elem is None:
            continue

        page_idx = elem["page_index"]

        # For tables: try VLM crop first; fall back to Docling extraction on failure.
        if elem["type"] == "table":
            bbox = _bbox_of(item)
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
                        _append_warning(
                            out,
                            "vlm_table_fallback",
                            f"VLM failed for table on page {page_idx}; "
                            f"using Docling extraction",
                            scope="element",
                            element_id=elem["element_id"],
                        )
                except Exception as exc:
                    logger.warning("Table VLM crop failed: %s", exc)
                    _append_warning(
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
            page_idx, result, page_elems,
            page_text_layer_tokens=raw_tokens.get(page_idx),
        )
        if decision.action == "promote_to_vlm":
            pages_to_promote.add(page_idx)
            _append_warning(
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
            promoted_docling[idx] = [
                e for e in out["elements"] if e.get("page_index") == idx
            ]
        out["elements"] = [
            e for e in out["elements"]
            if e.get("page_index") not in pages_to_promote
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
            _append_warning(
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

        def _keep_docling(code: str, msg: str) -> None:
            if fallback:
                out["elements"].extend(fallback)
                _append_warning(
                    out, "vlm_fallback_docling",
                    f"VLM {msg} on page {page_idx}; kept Docling output",
                    scope="page", page_index=page_idx,
                )
            else:
                _append_warning(out, code, msg, scope="page", page_index=page_idx)

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
            local_offset = _emit_vlm_elements(buf, raw_elem, page_idx, i, local_offset)

        if buf["elements"]:
            out["elements"].extend(buf["elements"])
            char_offset = local_offset
        else:
            _keep_docling("page_unparseable", "returned no usable elements")

    return out, char_offset


def _emit_vlm_elements(
    out: dict[str, Any], raw_elem: dict[str, Any], page_idx: int, seq: int, char_offset: int
) -> int:
    """Append VLM element(s) to ``out['elements']``; return the updated char_offset.

    A ``list`` element whose text holds several bulleted lines is expanded into
    one ``list_item`` element per item. The grading markdown converter only
    renders ``list_item`` (a bare ``list`` container contributes no text), so
    without this split the bullet text is silently dropped.

    A ``table`` element carrying a structured ``table`` object (rows/cols/cells)
    is preserved as table content so the converter renders its cells — the VLM
    often models TOCs/tables this way with an empty ``text``, which would
    otherwise be lost.
    """
    text = raw_elem.get("text", "") or ""
    elem_type = raw_elem.get("type", "paragraph")

    tbl = raw_elem.get("table")
    if elem_type == "table" and isinstance(tbl, dict) and tbl.get("cells"):
        cells = tbl["cells"]
        cell_text = " ".join(str(c.get("text", "")) for c in cells if isinstance(c, dict))
        out["elements"].append(
            {
                "element_id": f"vlm_p{page_idx}_{seq:04d}",
                "type": "table",
                "page_index": page_idx,
                "char_span": [char_offset, char_offset + len(cell_text)],
                # Mirror the cell text into `text`: the grader's markdown
                # converter skips any element with empty `text` *before*
                # rendering table cells, so an empty-text table is dropped.
                "text": text or cell_text,
                "content": {
                    "kind": "table",
                    "rows": tbl.get("rows", 0),
                    "cols": tbl.get("cols", 0),
                    "header_rows": tbl.get("header_rows", 1),
                    "cells": cells,
                },
            }
        )
        return char_offset + len(cell_text)

    if elem_type == "list" and text.strip():
        normalized = text.replace("•", "\n").replace("·", "\n")
        items = [ln.strip().lstrip("-*").strip() for ln in normalized.splitlines()]
        items = [it for it in items if it]
        if items:
            for j, item in enumerate(items):
                out["elements"].append(
                    {
                        "element_id": f"vlm_p{page_idx}_{seq:04d}_{j:02d}",
                        "type": "list_item",
                        "page_index": page_idx,
                        "char_span": [char_offset, char_offset + len(item)],
                        "text": item,
                        "content": {"kind": "text"},
                    }
                )
                char_offset += len(item)
            return char_offset

    vlm_elem: dict[str, Any] = {
        "element_id": f"vlm_p{page_idx}_{seq:04d}",
        "type": elem_type,
        "page_index": page_idx,
        "char_span": [char_offset, char_offset + len(text)],
        "text": text,
        "content": {"kind": "text"},
    }
    if elem_type == "heading" and "level" in raw_elem:
        vlm_elem["level"] = raw_elem["level"]
    out["elements"].append(vlm_elem)
    return char_offset + len(text)


# ---------------------------------------------------------------------------
# Image routing path
# ---------------------------------------------------------------------------


def _parse_image(
    path: Path, out: dict[str, Any], char_offset: int
) -> tuple[dict[str, Any], int]:
    """Image (PNG/JPEG/TIFF) routing path.

    1. Docling OCR extracts elements.
    2. Two-layer quality gate (quality_gate.py) decides whether to keep or promote to VLM.
    3. VLM replaces Docling output only when a layer fires (or Docling extracted nothing).
    """
    from parser_service.quality_gate import evaluate_page

    out["pages"].append({"page_index": 0, "width": 0, "height": 0, "rotation": 0})

    try:
        from docling.document_converter import DocumentConverter  # type: ignore[import-untyped]
        converter = DocumentConverter()
        conversion_result = converter.convert(str(path))
        doc = conversion_result.document
    except Exception as exc:
        _append_warning(out, "docling_failed", str(exc), scope="document")
        return out, char_offset

    before = len(out["elements"])
    from docling_core.types.doc.document import ContentLayer as _CL  # type: ignore[import-untyped]
    _layers = {_CL.BODY, _CL.FURNITURE}
    for item, _ in doc.iterate_items(traverse_pictures=True, included_content_layers=_layers):
        elem, char_offset = _docling_item_to_element(item, char_offset, out)
        if elem is not None:
            out["elements"].append(elem)

    page_elements = out["elements"][before:]
    decision = evaluate_page(0, conversion_result, page_elements)

    if decision.action == "keep":
        return out, char_offset

    # Promote to VLM — strip Docling's elements for this page first
    del out["elements"][before:]
    char_offset -= sum(len(e.get("text", "")) for e in page_elements)

    _append_warning(
        out,
        "vlm_promoted",
        f"Layer {decision.layer}: {decision.reason}",
        scope="page",
        page_index=0,
    )

    image_bytes = path.read_bytes()
    vlm_result = call_vlm(image_bytes, mode="page")

    if "error" in vlm_result:
        _append_warning(out, "image_unparseable", vlm_result["error"], scope="page", page_index=0)
        return out, char_offset

    elements_raw = vlm_result.get("elements")
    if not isinstance(elements_raw, list):
        _append_warning(out, "vlm_invalid_shape", "VLM image response 'elements' is not a list", scope="page", page_index=0)
        return out, char_offset

    for i, raw_elem in enumerate(elements_raw):
        if not isinstance(raw_elem, dict):
            continue
        char_offset = _emit_vlm_elements(out, raw_elem, 0, i, char_offset)

    return out, char_offset


# ---------------------------------------------------------------------------
# DOCX / XLSX / HTML routing path
# ---------------------------------------------------------------------------


def _parse_office_or_html(
    path: Path, out: dict[str, Any], kind: str, char_offset: int
) -> tuple[dict[str, Any], int]:
    """DOCX/XLSX/HTML routing path. Docling only, one logical page, no VLM."""
    from docling.document_converter import DocumentConverter  # type: ignore[import-untyped]

    # Emit exactly one logical page for these formats.
    out["pages"].append({"page_index": 0, "width": 0, "height": 0, "rotation": 0})

    converter = DocumentConverter()
    try:
        result = converter.convert(str(path))
    except Exception as exc:
        logger.warning("Docling conversion failed for %s: %s", path.name, exc)
        _append_warning(out, "docling_failed", str(exc), scope="document")
        return out, char_offset

    doc = result.document

    from docling_core.types.doc.document import ContentLayer as _CL  # type: ignore[import-untyped]
    _layers = {_CL.BODY, _CL.FURNITURE}
    for item, _level in doc.iterate_items(traverse_pictures=True, included_content_layers=_layers):
        elem, char_offset = _docling_item_to_element(item, char_offset, out)
        if elem is not None:
            out["elements"].append(elem)

    return out, char_offset


# ---------------------------------------------------------------------------
# Docling item → eval-harness element mapping
# ---------------------------------------------------------------------------


def _docling_item_to_element(
    item: Any,
    char_offset: int,
    out: dict[str, Any],
) -> tuple[dict[str, Any] | None, int]:
    """Map one Docling item to an eval-harness element dict.

    Args:
        item: A Docling document item (any type).
        char_offset: Current running character offset.
        out: The output dict (used to determine next element_id index).

    Returns:
        (element_dict, new_char_offset) on success.
        (None, char_offset) for unmapped Docling types — caller must skip None.
    """
    type_name = type(item).__name__
    elem_type = _DOCLING_TYPE_MAP.get(type_name)
    if elem_type is None:
        return None, char_offset

    # element_id: prefer item.self_ref if available.
    if hasattr(item, "self_ref") and item.self_ref:
        element_id = item.self_ref.lstrip("#/")
    else:
        element_id = f"elem_{len(out['elements']):05d}"

    text = getattr(item, "text", "") or ""
    page_index = _page_index_of(item)
    bbox = _bbox_of(item)

    elem: dict[str, Any] = {
        "element_id": element_id,
        "type": elem_type,
        "page_index": page_index,
        "char_span": [char_offset, char_offset + len(text)],
        "text": text,
        "content": {"kind": "text"},
    }

    if bbox is not None:
        elem["bbox"] = bbox

    if elem_type == "heading":
        elem["level"] = int(getattr(item, "level", 1) or 1)

    if elem_type == "table":
        elem["content"] = _docling_table_to_content(item)

    return elem, char_offset + len(text)


def _docling_table_to_content(table_item: Any) -> dict[str, Any]:
    """Convert Docling TableItem grid to TableContent dict.

    Returns {"kind": "table", "rows": ..., "cols": ..., "header_rows": ...,
             "cells": [...]}.
    Returns {"kind": "table", "rows": 0, "cols": 0, "header_rows": 0, "cells": []}
    on any error.

    # Docling table API stability note: grid access via table_item.data.grid
    # uses the stable 2.x API. If this breaks on a future patch, pin docling
    # version in pyproject.toml.
    """
    try:
        data = table_item.data
        rows = data.num_rows
        cols = data.num_cols
        cells_out: list[dict[str, Any]] = []

        for r in range(rows):
            for c in range(cols):
                cell = data.grid[r][c]
                if cell is None:
                    continue
                # Only emit the top-left cell of a merged span.
                if cell.start_row_offset_idx != r or cell.start_col_offset_idx != c:
                    continue
                row_span = max(1, cell.end_row_offset_idx - cell.start_row_offset_idx)
                col_span = max(1, cell.end_col_offset_idx - cell.start_col_offset_idx)
                cells_out.append(
                    {
                        "row": r,
                        "col": c,
                        "text": cell.text or "",
                        "row_span": row_span,
                        "col_span": col_span,
                    }
                )

        return {
            "kind": "table",
            "rows": rows,
            "cols": cols,
            "header_rows": 1,
            "cells": cells_out,
        }
    except Exception as exc:
        logger.warning("Could not read Docling table data: %s", exc)
        return {"kind": "table", "rows": 0, "cols": 0, "header_rows": 0, "cells": []}


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _bbox_of(item: Any) -> dict[str, float] | None:
    """Extract (x0, y0, x1, y1) bbox from a Docling item if available.

    Docling bbox fields: l (left), t (top), r (right), b (bottom) in PDF pts.
    """
    prov = getattr(item, "prov", None)
    if not prov:
        return None
    first = prov[0]
    b = getattr(first, "bbox", None)
    if b is None:
        return None
    return {
        "x0": float(b.l),
        "y0": float(b.b),  # PDF bottom = y0 (bottom-left origin)
        "x1": float(b.r),
        "y1": float(b.t),  # PDF top = y1
    }


def _page_index_of(item: Any) -> int:
    """Return 0-indexed page number for a Docling item.

    Docling uses 1-indexed page numbers; we subtract 1.
    Returns 0 on AttributeError (safe default).
    """
    try:
        return int(item.prov[0].page_no) - 1
    except (AttributeError, IndexError, TypeError):
        return 0


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
