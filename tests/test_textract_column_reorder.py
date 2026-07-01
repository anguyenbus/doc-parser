"""
test_textract_column_reorder.py — offline tests for the column-aware
reading-order reconciliation added to ``textract_client.py`` (Engine Hardening
item 1.4).

All pure/offline (no boto3, no AWS). Fixtures are hand-crafted AnalyzeDocument
block graphs with NO PAGE->CHILD reading order, so they exercise ONLY the
geometry-fallback branch:
  - ``textract_two_column_blocks.json``: genuine two-column page.
  - ``textract_single_column_blocks.json``: single-column page.

Groups:
  - Group 1 (this file, first half): column-band detection + reading-order score.
  - Group 2 (this file, second half): _ordered_layout_blocks integration + A/B valve.
  - Group 3 (this file, tail): E2E _blocks_to_elements -> render_markdown ordering.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from parser_service.markdown import render_markdown
from parser_service.textract_client import (
    _blocks_to_elements,
    _column_reordered_blocks,
    _detect_column_bands,
    _ordered_layout_blocks,
    _reading_order_score,
)

FIXTURES = Path(__file__).parent / "fixtures"
TWO_COL = FIXTURES / "textract_two_column_blocks.json"
ONE_COL = FIXTURES / "textract_single_column_blocks.json"


def _load(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text())["Blocks"]  # type: ignore[no-any-return]


def _layout_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in blocks if str(b.get("BlockType", "")).startswith("LAYOUT_")]


def _ids(blocks: list[dict[str, Any]]) -> list[str]:
    return [b.get("Id", "") for b in blocks]


def _geo_key(b: dict[str, Any]) -> tuple[float, float]:
    bbox = b.get("Geometry", {}).get("BoundingBox", {})
    return (bbox.get("Top", 0.0), bbox.get("Left", 0.0))


# ===========================================================================
# Group 1 — column-band detection
# ===========================================================================


def test_two_column_yields_two_bands() -> None:
    """The two-column fixture yields exactly 2 column bands."""
    layout = _layout_blocks(_load(TWO_COL))
    bands, _full_width = _detect_column_bands(layout)
    assert len(bands) == 2, [_ids(b) for b in bands]


def test_two_column_band_membership() -> None:
    """Column-A blocks land in the left band, column-B blocks in the right band."""
    layout = _layout_blocks(_load(TWO_COL))
    bands, _full_width = _detect_column_bands(layout)

    # Bands are ordered left-to-right.
    left_ids = set(_ids(bands[0]))
    right_ids = set(_ids(bands[1]))

    assert {"lay-a1", "lay-a2", "lay-a3"} <= left_ids
    assert {"lay-b1", "lay-b2", "lay-b3"} <= right_ids
    # No cross-contamination.
    assert left_ids.isdisjoint({"lay-b1", "lay-b2", "lay-b3"})
    assert right_ids.isdisjoint({"lay-a1", "lay-a2", "lay-a3"})


def test_full_width_block_not_forced_into_a_column() -> None:
    """The spanning title is treated as full-width, not stuffed into one column band."""
    layout = _layout_blocks(_load(TWO_COL))
    bands, full_width = _detect_column_bands(layout)

    full_ids = set(_ids(full_width))
    assert "lay-title" in full_ids
    for band in bands:
        assert "lay-title" not in set(_ids(band))


def test_single_column_yields_one_band() -> None:
    """The single-column fixture yields exactly 1 column band."""
    layout = _layout_blocks(_load(ONE_COL))
    bands, _full_width = _detect_column_bands(layout)
    assert len(bands) == 1, [_ids(b) for b in bands]
    assert set(_ids(bands[0])) == {"lay-title", "lay-p1", "lay-p2", "lay-p3"}


# ===========================================================================
# Group 1 — reading-order score
# ===========================================================================


def test_reordered_scores_higher_than_interleaved_on_two_column() -> None:
    """A correct column-ordered sequence scores strictly higher than (Top, Left)."""
    layout = _layout_blocks(_load(TWO_COL))

    interleaved = sorted(layout, key=_geo_key)
    reordered = _column_reordered_blocks(layout)

    score_interleaved = _reading_order_score(interleaved)
    score_reordered = _reading_order_score(reordered)

    assert score_reordered > score_interleaved + 0.05, (
        score_interleaved,
        score_reordered,
    )


def test_scores_tie_on_single_column() -> None:
    """On a single-column page the two candidate orders score equal (valve keeps original)."""
    layout = _layout_blocks(_load(ONE_COL))

    interleaved = sorted(layout, key=_geo_key)
    reordered = _column_reordered_blocks(layout)

    # Both candidates are the same sequence -> identical score, no margin cleared.
    assert _ids(interleaved) == _ids(reordered)
    assert _reading_order_score(interleaved) == _reading_order_score(reordered)


# ===========================================================================
# Group 2 — _ordered_layout_blocks integration + A/B valve
# ===========================================================================


def _ordered(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {b["Id"]: b for b in blocks if "Id" in b}
    return _ordered_layout_blocks(blocks, by_id)


def test_geometry_fallback_two_column_reorders_column_a_before_b() -> None:
    """Two-column geometry page: every column-B block sorts after all column-A blocks."""
    ordered_ids = _ids(_ordered(_load(TWO_COL)))

    a_positions = [ordered_ids.index(i) for i in ("lay-a1", "lay-a2", "lay-a3")]
    b_positions = [ordered_ids.index(i) for i in ("lay-b1", "lay-b2", "lay-b3")]

    assert max(a_positions) < min(b_positions), ordered_ids
    # The spanning title still leads the page.
    assert ordered_ids[0] == "lay-title", ordered_ids


def test_geometry_fallback_single_column_is_byte_identical() -> None:
    """Single-column geometry page: order matches the current (Top, Left) sort exactly."""
    blocks = _load(ONE_COL)
    layout = _layout_blocks(blocks)

    expected = _ids(sorted(layout, key=_geo_key))
    assert _ids(_ordered(blocks)) == expected


def test_native_page_child_order_not_reconciled() -> None:
    """When a PAGE->CHILD order exists, reconciliation is NOT applied (native order kept)."""
    # Build a two-column geometry but ALSO give a PAGE block whose CHILD order is
    # deliberately the interleaved order. The native order must be preserved verbatim.
    interleaved_ids = ["lay-title", "lay-a1", "lay-b1", "lay-a2", "lay-b2", "lay-a3", "lay-b3"]
    page = {
        "BlockType": "PAGE",
        "Id": "page-1",
        "Relationships": [{"Type": "CHILD", "Ids": interleaved_ids}],
    }
    blocks = [page] + _load(TWO_COL)
    assert _ids(_ordered(blocks)) == interleaved_ids


def test_ab_valve_keeps_original_when_margin_not_cleared() -> None:
    """A borderline near-single-column page: reordered does not beat original by the
    margin, so the original (Top, Left) order is kept."""
    # x-centers 0.48 and 0.52 — within jitter tolerance, so this reads as ONE band.
    # Reorder == original -> tie -> original kept.
    blocks = [
        {
            "BlockType": "LAYOUT_TEXT",
            "Id": "n1",
            "Geometry": {"BoundingBox": {"Left": 0.10, "Width": 0.76, "Top": 0.10}},
        },
        {
            "BlockType": "LAYOUT_TEXT",
            "Id": "n2",
            "Geometry": {"BoundingBox": {"Left": 0.14, "Width": 0.76, "Top": 0.30}},
        },
        {
            "BlockType": "LAYOUT_TEXT",
            "Id": "n3",
            "Geometry": {"BoundingBox": {"Left": 0.10, "Width": 0.76, "Top": 0.50}},
        },
    ]
    assert _ids(_ordered(blocks)) == ["n1", "n2", "n3"]


# ===========================================================================
# Group 3 — E2E _blocks_to_elements -> render_markdown ordering
# ===========================================================================


def test_e2e_markdown_column_a_before_column_b() -> None:
    """The two-column fixture through _blocks_to_elements -> render_markdown puts all
    column-A text before column-B text."""
    elements = _blocks_to_elements(_load(TWO_COL))
    md = render_markdown({"elements": elements})

    a3 = md.index("COLA three")
    b1 = md.index("COLB one")
    assert a3 < b1, md
    # Title leads.
    assert md.index("Spanning Title") < md.index("COLA one")
