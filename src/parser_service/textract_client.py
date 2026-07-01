"""
textract_client.py

All AWS Textract API communication for the parser service — the Textract twin of
``vlm_client.py``. Textract is a SELECTABLE escalation engine (behind
``PARSER_ESCALATION_ENGINE``) that replaces the Bedrock VLM on gate-promoted pages
only; it emits the SAME element-JSON shape the VLM emits, so the downstream
``_emit_vlm_elements`` -> ``render_markdown`` path is reused unchanged.

Public API:
  analyze_page(image_bytes) -> dict   # never raises; {"error": ...} on failure
  _blocks_to_elements(blocks) -> list  # pure function (no boto3 / env / I/O)
  get_textract_call_count() -> int
  reset_textract_call_count() -> None

The single synchronous call is ``textract.analyze_document(Document={"Bytes": ...},
FeatureTypes=["LAYOUT", "TABLES"])`` — NO S3, NO ``StartDocumentAnalysis``, NO polling.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# LAYOUT block type -> element-JSON mapping
# -------------------------------------------------------------------------
# Each LAYOUT_* block maps to an element ``type`` the existing renderer
# (``parser_service._emit_vlm_elements`` + ``markdown.render_markdown``) already
# consumes. Headings additionally carry a ``level``; tables carry a structured
# ``table`` object. This mirrors the shape ``vlm_client.PAGE_PROMPT`` defines.
_LAYOUT_TYPE_MAP: dict[str, str] = {
    "LAYOUT_TITLE": "heading",
    "LAYOUT_SECTION_HEADER": "heading",
    "LAYOUT_TEXT": "paragraph",
    "LAYOUT_LIST": "list",
    "LAYOUT_TABLE": "table",
    "LAYOUT_HEADER": "header",
    "LAYOUT_FOOTER": "footer",
    "LAYOUT_PAGE_NUMBER": "page_number",
    "LAYOUT_FIGURE": "figure",
}
_HEADING_LEVELS: dict[str, int] = {
    "LAYOUT_TITLE": 1,
    "LAYOUT_SECTION_HEADER": 2,
}

# -------------------------------------------------------------------------
# Textract call counter (per-worker-thread) — parity with vlm_client
# -------------------------------------------------------------------------
# Backed by threading.local() so reset / increment / read are isolated per
# worker thread (same rationale as vlm_client: one parse per pool thread, no
# threads/asyncio spawned inside parse_to_markdown). The public get_/reset_
# API shape is unchanged; single-threaded main-thread callers are unaffected.
_textract_counter = threading.local()


def get_textract_call_count() -> int:
    """Return the number of successful Textract calls on THIS thread since last reset."""
    return getattr(_textract_counter, "count", 0)


def reset_textract_call_count() -> None:
    """Reset THIS thread's Textract call counter to zero (called at the start of each parse)."""
    _textract_counter.count = 0


def _increment_textract_call_count() -> None:
    """Increment THIS thread's Textract call counter by one (on a successful call)."""
    _textract_counter.count = getattr(_textract_counter, "count", 0) + 1


# -------------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------------


def analyze_page(image_bytes: bytes) -> dict[str, Any]:
    """Send a single page image to AWS Textract and return parsed element-JSON.

    Mirrors ``vlm_client.call_vlm``'s contract: never raises; returns
    ``{"error": "<reason>"}`` on any failure; returns ``{"elements": [...]}`` on
    success (the SAME shape the VLM returns, so the renderer is reused unchanged).

    Uses the SYNCHRONOUS ``analyze_document`` API with ``Document={"Bytes": ...}``
    and ``FeatureTypes=["LAYOUT", "TABLES"]`` — no S3, no ``StartDocumentAnalysis``,
    no polling. One call per promoted page.

    Args:
        image_bytes: Raw PNG/JPEG bytes of a single rendered page (<=10 MB).

    Returns:
        ``{"elements": [...]}`` on success; ``{"error": "<reason>"}`` on failure.
        Never raises.
    """
    try:
        import boto3  # AWS SDK — only imported at call time (mirrors call_vlm)

        region = os.environ.get("AWS_REGION", "us-east-1")
        client = boto3.client("textract", region_name=region)

        resp = client.analyze_document(
            Document={"Bytes": image_bytes},
            FeatureTypes=["LAYOUT", "TABLES"],
        )

        elements = _blocks_to_elements(resp.get("Blocks", []))
        _increment_textract_call_count()
        return {"elements": elements}

    except Exception as exc:
        logger.warning("Textract call failed: %s", exc)
        return {"error": str(exc)}


# -------------------------------------------------------------------------
# Pure mapping — Textract block graph -> element-JSON (no boto3 / env / I/O)
# -------------------------------------------------------------------------


def _blocks_to_elements(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map a Textract ``AnalyzeDocument`` block graph to the element-JSON list.

    Pure, side-effect-free function (parallel to ``vlm_client._build_bedrock_request``):
    accepts the ``Blocks`` list from an ``AnalyzeDocument`` (LAYOUT + TABLES) response
    and returns the ``elements`` list in the EXACT shape ``parser_service._emit_vlm_elements``
    + ``markdown.render_markdown`` already consume — no schema change, no renderer change.

    LAYOUT block types map per ``_LAYOUT_TYPE_MAP``; ``LAYOUT_TITLE``/``LAYOUT_SECTION_HEADER``
    additionally carry a heading ``level`` (1 / 2). ``LAYOUT_TABLE`` and bare ``TABLE`` blocks
    build a 0-indexed table object from their CELL grid (Textract is 1-indexed). Reading order
    follows the PAGE block's CHILD relationship (Textract's reading order); when that ordering
    is absent, LAYOUT blocks are sorted by geometry top-then-left.

    Args:
        blocks: The ``Blocks`` list from an ``AnalyzeDocument`` response.

    Returns:
        The ``elements`` list (each a dict with ``type``/``text`` and, where relevant,
        ``level`` or ``table``).
    """
    by_id = {b["Id"]: b for b in blocks if "Id" in b}

    layout_blocks = _ordered_layout_blocks(blocks, by_id)

    # Track TABLE blocks reached via a LAYOUT_TABLE so we don't double-emit a bare
    # TABLE block that has no enclosing LAYOUT_TABLE.
    consumed_table_ids: set[str] = set()

    elements: list[dict[str, Any]] = []
    for block in layout_blocks:
        block_type = block.get("BlockType", "")
        elem_type = _LAYOUT_TYPE_MAP.get(block_type)
        if elem_type is None:
            continue

        if elem_type == "table":
            table_block = _table_block_for_layout(block, by_id)
            if table_block is not None:
                consumed_table_ids.add(table_block["Id"])
                elements.append(_make_table_element(table_block, by_id))
            continue

        text = _layout_text(block, by_id)
        elem: dict[str, Any] = {"type": elem_type, "text": text}
        if block_type in _HEADING_LEVELS:
            elem["level"] = _HEADING_LEVELS[block_type]
        elements.append(elem)

    # Emit any standalone TABLE blocks not already pulled in via a LAYOUT_TABLE
    # (defensive: some responses expose TABLE without a wrapping LAYOUT_TABLE).
    for block in blocks:
        if block.get("BlockType") == "TABLE" and block.get("Id") not in consumed_table_ids:
            elements.append(_make_table_element(block, by_id))

    return elements


def _ordered_layout_blocks(
    blocks: list[dict[str, Any]], by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return LAYOUT_* blocks in Textract reading order.

    Reading order is the order of LAYOUT children under the PAGE block(s). When no
    PAGE->CHILD ordering is available, fall back to sorting LAYOUT blocks by geometry
    (top, then left).
    """
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in blocks:
        if page.get("BlockType") != "PAGE":
            continue
        for child in _child_blocks(page, by_id):
            cid = child.get("Id", "")
            if str(child.get("BlockType", "")).startswith("LAYOUT_") and cid not in seen:
                ordered.append(child)
                seen.add(cid)

    if ordered:
        return ordered

    # No PAGE ordering — sort by geometry top-then-left.
    layout = [b for b in blocks if str(b.get("BlockType", "")).startswith("LAYOUT_")]

    def _geo_key(b: dict[str, Any]) -> tuple[float, float]:
        bbox = b.get("Geometry", {}).get("BoundingBox", {})
        return (bbox.get("Top", 0.0), bbox.get("Left", 0.0))

    return sorted(layout, key=_geo_key)


def _child_blocks(
    block: dict[str, Any], by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Resolve a block's CHILD relationship Ids to their block dicts (in order)."""
    out: list[dict[str, Any]] = []
    for rel in block.get("Relationships", []):
        if rel.get("Type") == "CHILD":
            for cid in rel.get("Ids", []):
                child = by_id.get(cid)
                if child is not None:
                    out.append(child)
    return out


def _child_text(block: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str:
    """Join the text of a block's CHILD WORD / SELECTION_ELEMENT blocks.

    Ported from ``run_textract_analyze.py`` / ``add_textract_tab.py`` (the block ->
    text logic only, not the async/FORMS plumbing). SELECTION_ELEMENTs render as
    ``[X]`` (selected) / ``[ ]`` (unselected).
    """
    words: list[str] = []
    for child in _child_blocks(block, by_id):
        bt = child.get("BlockType")
        if bt == "WORD":
            words.append(child.get("Text", ""))
        elif bt == "SELECTION_ELEMENT":
            words.append("[X]" if child.get("SelectionStatus") == "SELECTED" else "[ ]")
    return " ".join(words)


def _layout_text(block: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str:
    """Collect the text under a LAYOUT block.

    LAYOUT blocks reference LINE children (which in turn reference WORD children);
    each LINE carries its own ``Text``. Falls back to a WORD/SELECTION_ELEMENT join
    when LINEs are absent.
    """
    lines: list[str] = []
    has_line = False
    for child in _child_blocks(block, by_id):
        bt = child.get("BlockType")
        if bt == "LINE":
            has_line = True
            text = child.get("Text")
            if not text:
                text = _child_text(child, by_id)
            if text:
                lines.append(text)
    if has_line:
        return "\n".join(lines)
    return _child_text(block, by_id)


def _table_block_for_layout(
    layout_block: dict[str, Any], by_id: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """Find the TABLE block referenced by a LAYOUT_TABLE block, if any."""
    for child in _child_blocks(layout_block, by_id):
        if child.get("BlockType") == "TABLE":
            return child
    return None


def _make_table_element(
    table_block: dict[str, Any], by_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Build a table element from a TABLE block's CELL grid.

    Reconstructs the grid from CELL ``RowIndex``/``ColumnIndex`` (ported from the
    repo-root prior-art scripts) and converts Textract's 1-indexed positions to the
    schema's 0-indexed ``row``/``col``. Carries ``row_span``/``col_span`` (default 1)
    and counts header rows from cells tagged ``COLUMN_HEADER``. Emits the exact table
    object ``vlm_client.TABLE_PROMPT`` defines and ``_emit_vlm_elements`` consumes.
    """
    cells_blocks = [
        c for c in _child_blocks(table_block, by_id) if c.get("BlockType") == "CELL"
    ]

    cells: list[dict[str, Any]] = []
    max_row = 0
    max_col = 0
    header_rows_1indexed: set[int] = set()

    for c in cells_blocks:
        row_index = c.get("RowIndex", 1)
        col_index = c.get("ColumnIndex", 1)
        row_span = c.get("RowSpan", 1)
        col_span = c.get("ColumnSpan", 1)
        text = _child_text(c, by_id)

        if "COLUMN_HEADER" in c.get("EntityTypes", []):
            header_rows_1indexed.add(row_index)

        cells.append(
            {
                # 1-indexed (Textract) -> 0-indexed (schema)
                "row": row_index - 1,
                "col": col_index - 1,
                "text": text,
                "row_span": row_span,
                "col_span": col_span,
            }
        )
        max_row = max(max_row, row_index + row_span - 1)
        max_col = max(max_col, col_index + col_span - 1)

    # header_rows = count of leading rows flagged COLUMN_HEADER (default 1 when none
    # are tagged but the table is non-empty, matching the renderer's expectation).
    header_rows = len(header_rows_1indexed) if header_rows_1indexed else (1 if cells else 0)

    return {
        "type": "table",
        "text": "",
        "table": {
            "rows": max_row,
            "cols": max_col,
            "header_rows": header_rows,
            "cells": cells,
        },
    }
