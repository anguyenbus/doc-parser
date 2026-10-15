"""
test_pdf.py

Element mapping tests: ``docling_item_to_element`` and ``docling_table_to_content``
(``parsing/elements.py``), which the markdown path uses to turn Docling items into
elements. The PDF paths themselves are tested through ``parse_to_markdown``
(``test_markdown_pipeline*.py``) and the page pipeline (``test_page_pipeline.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

FIXTURES = Path(__file__).parent / "fixtures"


# ===========================================================================
# Phase 3b: Docling item-to-element mapping tests
# ===========================================================================


def _make_mock_item(
    class_name: str,
    text: str = "sample text",
    self_ref: str | None = None,
    level: int | None = None,
    prov: list[Any] | None = None,
) -> MagicMock:
    """Create a mock Docling item with given properties."""
    item = MagicMock()
    item.__class__.__name__ = class_name
    type(item).__name__ = class_name
    item.text = text
    if self_ref is not None:
        item.self_ref = self_ref
    else:
        del item.self_ref  # make hasattr return False
    if level is not None:
        item.level = level
    if prov is not None:
        item.prov = prov
    else:
        item.prov = []
    return item


# ---------------------------------------------------------------------------
# Test 1: TextItem → paragraph with correct char_span
# ---------------------------------------------------------------------------


def test_docling_item_to_element_text_item() -> None:
    """docling_item_to_element maps TextItem to paragraph with correct char_span."""
    from parser_service.parsing.elements import docling_item_to_element

    item = _make_mock_item("TextItem", text="Hello world", self_ref=None)
    out: dict[str, Any] = {"elements": []}
    char_offset = 10

    elem, new_offset = docling_item_to_element(item, char_offset, out)

    assert elem is not None
    assert elem["type"] == "paragraph"
    assert elem["text"] == "Hello world"
    assert elem["char_span"] == [10, 21]
    assert new_offset == 21


# ---------------------------------------------------------------------------
# Test 2: SectionHeaderItem with level=2 → heading with level
# ---------------------------------------------------------------------------


def test_docling_item_to_element_section_header() -> None:
    """docling_item_to_element maps SectionHeaderItem to heading with level."""
    from parser_service.parsing.elements import docling_item_to_element

    item = _make_mock_item("SectionHeaderItem", text="My Section", level=2, self_ref="texts/0")
    out: dict[str, Any] = {"elements": []}

    elem, new_offset = docling_item_to_element(item, 0, out)

    assert elem is not None
    assert elem["type"] == "heading"
    assert elem["level"] == 2
    assert elem["element_id"] == "texts/0"


# ---------------------------------------------------------------------------
# Test 3: Unknown item type → (None, char_offset) unchanged
# ---------------------------------------------------------------------------


def test_docling_item_to_element_unknown_type() -> None:
    """Unknown Docling item types return (None, unchanged_char_offset)."""
    from parser_service.parsing.elements import docling_item_to_element

    item = _make_mock_item("SomeUnknownDoclingItem", text="irrelevant")
    out: dict[str, Any] = {"elements": []}
    char_offset = 42

    elem, new_offset = docling_item_to_element(item, char_offset, out)

    assert elem is None
    assert new_offset == 42  # unchanged


# ---------------------------------------------------------------------------
# Test 4: docling_table_to_content with a merged cell (row_span=2)
# ---------------------------------------------------------------------------


def test_docling_table_to_content_merged_cell() -> None:
    """docling_table_to_content emits merged cell once with correct row_span."""
    from parser_service.parsing.elements import docling_table_to_content

    # Build a mock 3x2 table where cell (0,0) spans 2 rows
    def make_cell(start_r: int, end_r: int, start_c: int, end_c: int, text: str) -> MagicMock:
        cell = MagicMock()
        cell.start_row_offset_idx = start_r
        cell.end_row_offset_idx = end_r
        cell.start_col_offset_idx = start_c
        cell.end_col_offset_idx = end_c
        cell.text = text
        return cell

    grid = [
        # Row 0
        [make_cell(0, 2, 0, 1, "Merged"), make_cell(0, 1, 1, 2, "B0")],
        # Row 1: (1,0) is the continuation of the merged cell — start_row=0 != 1
        [make_cell(0, 2, 0, 1, "Merged"), make_cell(1, 2, 1, 2, "B1")],
    ]

    table_item = MagicMock()
    table_item.data.num_rows = 2
    table_item.data.num_cols = 2
    table_item.data.grid = grid

    content = docling_table_to_content(table_item)

    assert content["kind"] == "table"
    assert content["rows"] == 2
    assert content["cols"] == 2

    cells = content["cells"]
    # The merged cell (0,0) should appear exactly once
    merged_cells = [c for c in cells if c["row"] == 0 and c["col"] == 0]
    assert len(merged_cells) == 1
    assert merged_cells[0]["row_span"] == 2
    assert merged_cells[0]["text"] == "Merged"

    # The continuation at (1,0) should NOT appear
    row1_col0 = [c for c in cells if c["row"] == 1 and c["col"] == 0]
    assert len(row1_col0) == 0
