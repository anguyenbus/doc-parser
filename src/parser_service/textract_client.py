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
    is absent, LAYOUT blocks fall back to geometry with column-aware reconciliation
    (see ``_reconcile_geometry_order``).

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

    Reading order is the order of LAYOUT children under the PAGE block(s) — this
    native ordering is usually correct and is returned unchanged. When no
    PAGE->CHILD ordering is available, fall back to geometry: apply column-aware
    reading-order reconciliation (see ``_reconcile_geometry_order``) which keeps
    single-column pages byte-identical to the plain ``(Top, Left)`` sort and only
    reorders multi-column pages when a self-consistency score clears a margin.
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

    # No PAGE ordering — geometry fallback with column-aware reconciliation.
    layout = [b for b in blocks if str(b.get("BlockType", "")).startswith("LAYOUT_")]
    return _reconcile_geometry_order(layout)


# -------------------------------------------------------------------------
# Geometry-fallback reading-order reconciliation (pure; no boto3 / env / I/O)
# -------------------------------------------------------------------------
# When Textract emits no PAGE->CHILD reading order, the legacy behavior sorted
# LAYOUT blocks by raw ``(Top, Left)``. On a two-column page that interleaves the
# columns row-by-row and destroys reading order. We instead detect column bands
# from block geometry, build a column-aware order, and adopt it ONLY when a pure
# self-consistency score beats the original by a fixed margin — an A/B safety
# valve (ported from ``references/pdfmux``'s ``reorder_text_ab``) that guarantees
# single-column pages stay byte-identical to the plain ``(Top, Left)`` sort.

# A block wider than this fraction of the page spans columns (header/footer/title).
_SPAN_WIDTH_RATIO = 0.85
# x-center jitter tolerance when clustering blocks into column bands.
_COLUMN_XCENTER_TOL = 0.10
# The column-reordered candidate must beat the original by this margin to be adopted.
_REORDER_MARGIN = 0.05


def _bbox(block: dict[str, Any]) -> dict[str, float]:
    bbox: dict[str, float] = block.get("Geometry", {}).get("BoundingBox", {})
    return bbox


def _geo_top_left(block: dict[str, Any]) -> tuple[float, float]:
    bbox = _bbox(block)
    return (bbox.get("Top", 0.0), bbox.get("Left", 0.0))


def _x_center(block: dict[str, Any]) -> float:
    bbox = _bbox(block)
    return bbox.get("Left", 0.0) + bbox.get("Width", 0.0) / 2.0


def _detect_column_bands(
    layout: list[dict[str, Any]],
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Cluster LAYOUT blocks into vertical column bands by x-center.

    Full-width blocks (Width > ``_SPAN_WIDTH_RATIO`` of the page) are pulled out as
    spanning furniture and returned separately — they must NOT be forced into a
    single column. The remaining blocks are clustered by x-center with a jitter
    tolerance; bands are returned left-to-right, each band's blocks kept in input
    order. Pure — geometry only.

    Returns:
        ``(bands, full_width)`` where ``bands`` is a left-to-right list of column
        block-lists and ``full_width`` is the spanning blocks.
    """
    full_width: list[dict[str, Any]] = []
    column_blocks: list[dict[str, Any]] = []
    for b in layout:
        if _bbox(b).get("Width", 0.0) > _SPAN_WIDTH_RATIO:
            full_width.append(b)
        else:
            column_blocks.append(b)

    if not column_blocks:
        return [], full_width

    # Cluster x-centers: walk sorted centers, start a new band when the gap to the
    # running band's mean center exceeds the tolerance.
    order = sorted(range(len(column_blocks)), key=lambda i: _x_center(column_blocks[i]))
    bands_idx: list[list[int]] = []
    band_centers: list[float] = []
    for i in order:
        c = _x_center(column_blocks[i])
        if bands_idx and abs(c - band_centers[-1]) <= _COLUMN_XCENTER_TOL:
            bands_idx[-1].append(i)
            n = len(bands_idx[-1])
            band_centers[-1] += (c - band_centers[-1]) / n
        else:
            bands_idx.append([i])
            band_centers.append(c)

    # Rebuild each band preserving the blocks' original input order, ordered L->R.
    bands: list[list[dict[str, Any]]] = [
        [column_blocks[i] for i in sorted(idxs)] for idxs in bands_idx
    ]
    return bands, full_width


def _column_reordered_blocks(layout: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the column-aware reading order for a geometry-only page.

    Full-width spanning blocks act as horizontal dividers: sorted by Top, each one
    LEADS the vertical zone it opens. Within each zone the column bands are emitted
    left-to-right, each band top-to-bottom. With a single band this is exactly the
    ``(Top, Left)`` order, so single-column pages are unchanged. Pure.
    """
    bands, full_width = _detect_column_bands(layout)

    if len(bands) <= 1:
        # Single band (or none): identical to the plain (Top, Left) sort.
        return sorted(layout, key=_geo_top_left)

    dividers = sorted(full_width, key=lambda b: _bbox(b).get("Top", 0.0))
    boundaries = [_bbox(d).get("Top", 0.0) for d in dividers]

    out: list[dict[str, Any]] = []
    prev = float("-inf")
    for i, top in enumerate(boundaries + [float("inf")]):
        # Column blocks whose Top falls in [prev, top): band by band, each top->bottom.
        for band in bands:
            zone = [b for b in band if prev <= _bbox(b).get("Top", 0.0) < top]
            out.extend(sorted(zone, key=_geo_top_left))
        if i < len(dividers):
            out.append(dividers[i])
            prev = top
    return out


def _reading_order_score(ordered: list[dict[str, Any]]) -> float:
    """Pure self-consistency score for an ordered block sequence (0.0..1.0).

    Rewards transitions that stay within one column while progressing down the page
    (or move down within the same row band); gives partial credit to a genuine
    column reset (jump back UP to the top of a column further right); and penalizes
    transitions that thrash horizontally between columns — the tell-tale of a naive
    ``(Top, Left)`` sort interleaving multiple columns row-by-row. Geometry-native
    analogue of pdfmux's ``_score_reading_order``. No text, no model, no I/O.
    """
    if len(ordered) < 2:
        return 0.5

    forward = 0.0
    total = len(ordered) - 1
    for i in range(total):
        t_cur = _bbox(ordered[i]).get("Top", 0.0)
        t_nxt = _bbox(ordered[i + 1]).get("Top", 0.0)
        cx_cur = _x_center(ordered[i])
        cx_nxt = _x_center(ordered[i + 1])
        same_column = abs(cx_nxt - cx_cur) <= _COLUMN_XCENTER_TOL

        if same_column:
            # Same column: reward downward (or same-line) progression; a backward
            # jump within one column is the only within-column penalty.
            forward += 1.0 if t_nxt >= t_cur - 1e-6 else 0.0
        elif t_nxt < t_cur - 1e-6 and cx_nxt > cx_cur:
            # Column switch: jumped UP to a column further right (A->B reset). Clean.
            forward += 1.0
        else:
            # Different column but NOT a clean top-of-next-column reset: horizontal
            # thrash (row-by-row interleaving). No credit.
            forward += 0.0

    return forward / total if total else 0.5


def _reconcile_geometry_order(layout: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A/B valve: adopt the column-aware order only when it clears the margin.

    Scores the original ``(Top, Left)`` order against the column-reordered order and
    returns the reordered one ONLY when it beats the original by ``_REORDER_MARGIN``.
    Single-column pages tie (identical sequences) and keep the original, so their
    output is byte-identical to the legacy geometry sort.
    """
    original = sorted(layout, key=_geo_top_left)
    reordered = _column_reordered_blocks(layout)

    if _reading_order_score(reordered) > _reading_order_score(original) + _REORDER_MARGIN:
        return reordered
    return original


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
