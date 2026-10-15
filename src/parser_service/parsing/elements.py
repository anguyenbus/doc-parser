"""
elements.py — shared helpers of the element-JSON contract.

Map Docling items to eval-harness elements (``docling_item_to_element``), build the
output skeleton (``empty_output``), record warnings, classify a file by extension and
MIME type, and bound the raw input size. Used by the markdown pipeline
(``parsing/document.py``) and the legacy element-JSON parser
(``legacy/element_json.py``).
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from parser_service.version import PARSER_VERSION

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION: str = "1.0.0"

# Raw-input size cap. A file larger than this is rejected (stat-based, before any
# whole-file read or Docling/render) with an ``input_too_large`` warning.
# Overridable via PARSER_MAX_INPUT_MB. NOTE: this bounds the RAW input size only;
# it does NOT bound post-decompression expansion of zip-based Office formats
# (DOCX/XLSX) inside Docling, nor Docling's model working set (~1.6 GB/worker,
# the dominant memory term — see tech-stack.md). It is a sanity bound, not the
# pod memory budget.
_DEFAULT_MAX_INPUT_MB = 200.0


def max_input_bytes() -> int:
    """Return the raw-input byte cap (reads PARSER_MAX_INPUT_MB, in MB)."""
    mb = float(os.environ.get("PARSER_MAX_INPUT_MB", str(_DEFAULT_MAX_INPUT_MB)))
    return int(mb * 1_000_000)


def input_size_error(path: Path) -> str | None:
    """Return an error message if ``path`` exceeds the input-size cap, else None.

    Stat-based (no read), so an oversized file is rejected before any whole-file
    read or Docling/render work. A non-positive cap disables the check. Returns
    None when the size cannot be determined (let the normal parse path surface
    that failure instead of masking it here).
    """
    cap = max_input_bytes()
    if cap <= 0:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > cap:
        return (
            f"Input file is {size / 1_000_000:.1f} MB, exceeds "
            f"PARSER_MAX_INPUT_MB cap ({cap / 1_000_000:.0f} MB)"
        )
    return None


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """Stream a file's SHA-256 in ``chunk_size`` blocks (default 1 MiB).

    Avoids holding the whole file resident in memory just to hash it.
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


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


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_by_extension(path: Path, mime: str) -> str:
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


def empty_output(path: Path, mime: str) -> dict[str, Any]:
    """Build the schema skeleton with sha256, parsed_at, and empty arrays."""
    sha256 = sha256_file(path)
    return {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "parsed_at": datetime.now(tz=UTC).isoformat(),
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


def append_warning(
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


def emit_vlm_elements(
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
# Docling item → eval-harness element mapping
# ---------------------------------------------------------------------------


def docling_item_to_element(
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
    bbox = bbox_of(item)

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
        elem["content"] = docling_table_to_content(item)

    return elem, char_offset + len(text)


def docling_table_to_content(table_item: Any) -> dict[str, Any]:
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


def bbox_of(item: Any) -> dict[str, float] | None:
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
