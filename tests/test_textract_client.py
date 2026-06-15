"""
test_textract_client.py

Tests for textract_client.py — the Textract escalation engine (twin of
vlm_client.py). Mirrors test_vlm_client.py:
  - Task Group 1 (offline): _blocks_to_elements against a saved AnalyzeDocument
    (LAYOUT + TABLES) fixture, with NO network.
  - Task Group 2 (offline): analyze_page with boto3 mocked via sys.modules.
  - Task Group 5 (live): a single ``pytest -m live`` test hitting real Textract.

boto3 is imported inside analyze_page() at call time, so tests patch it via
sys.modules (the same pattern as test_vlm_client.py).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from parser_service.textract_client import (
    _blocks_to_elements,
    analyze_page,
    get_textract_call_count,
    reset_textract_call_count,
)

FIXTURES = Path(__file__).parent / "fixtures"
BLOCKS_FIXTURE = FIXTURES / "textract_analyze_blocks.json"

SAMPLE_IMAGE = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # minimal fake PNG bytes


def _load_blocks() -> list[dict[str, Any]]:
    """Load the offline AnalyzeDocument fixture's ``Blocks`` list."""
    data = json.loads(BLOCKS_FIXTURE.read_text())
    return data["Blocks"]  # type: ignore[no-any-return]


# ===========================================================================
# Task Group 1 — _blocks_to_elements offline mapping (no network)
# ===========================================================================


def test_heading_levels() -> None:
    """LAYOUT_TITLE -> heading level 1; LAYOUT_SECTION_HEADER -> heading level 2."""
    elements = _blocks_to_elements(_load_blocks())

    titles = [e for e in elements if e["type"] == "heading" and e["text"] == "Quarterly Financial Report"]
    sections = [e for e in elements if e["type"] == "heading" and e["text"] == "Overview"]

    assert len(titles) == 1, elements
    assert titles[0]["level"] == 1
    assert len(sections) == 1
    assert sections[0]["level"] == 2


def test_paragraph_mapping() -> None:
    """LAYOUT_TEXT -> paragraph carrying its joined line text."""
    elements = _blocks_to_elements(_load_blocks())
    paragraphs = [e for e in elements if e["type"] == "paragraph"]
    assert len(paragraphs) == 1, elements
    # Two LINEs joined with a newline.
    assert paragraphs[0]["text"] == "Revenue grew steadily.\nCosts fell."


def test_list_mapping() -> None:
    """LAYOUT_LIST -> exactly one list element with both items preserved."""
    elements = _blocks_to_elements(_load_blocks())
    lists = [e for e in elements if e["type"] == "list"]
    assert len(lists) == 1, elements
    assert "First item" in lists[0]["text"]
    assert "Second item" in lists[0]["text"]


def test_table_zero_indexed_cells() -> None:
    """LAYOUT_TABLE/TABLE -> table object with 0-indexed cells (Textract is 1-indexed)."""
    elements = _blocks_to_elements(_load_blocks())
    tables = [e for e in elements if e["type"] == "table"]
    assert len(tables) == 1, elements

    table = tables[0]["table"]
    assert table["rows"] == 2
    assert table["cols"] == 2
    assert table["header_rows"] == 1  # one COLUMN_HEADER row

    cells = {(c["row"], c["col"]): c for c in table["cells"]}
    # Textract RowIndex=1/ColumnIndex=1 -> schema row=0/col=0
    assert (0, 0) in cells
    assert cells[(0, 0)]["text"] == "Metric"
    assert cells[(0, 1)]["text"] == "Value"
    # Textract RowIndex=2/ColumnIndex=1 -> schema row=1/col=0; SELECTION_ELEMENT joined
    assert cells[(1, 0)]["text"] == "Approved [X]"
    assert cells[(1, 1)]["text"] == "42"
    # spans default to 1
    assert all(c["row_span"] == 1 and c["col_span"] == 1 for c in table["cells"])


def test_furniture_mapping() -> None:
    """LAYOUT_HEADER/FOOTER/PAGE_NUMBER/FIGURE map to header/footer/page_number/figure."""
    elements = _blocks_to_elements(_load_blocks())
    by_type = {e["type"]: e for e in elements}

    assert by_type["header"]["text"] == "Confidential Draft"
    assert by_type["footer"]["text"] == "Company Inc."
    assert by_type["page_number"]["text"] == "1"
    assert by_type["figure"]["text"] == "Figure 1"


def test_reading_order_follows_page_child_relationship() -> None:
    """Elements come back in Textract reading order (PAGE->CHILD), not array/geometry order.

    The fixture lists ``lay-title`` before ``lay-header`` in the PAGE's CHILD
    relationship, even though the header is geometrically above the title and appears
    earlier in the Blocks array. Reading order must follow the relationship.
    """
    elements = _blocks_to_elements(_load_blocks())
    types = [e["type"] for e in elements]
    assert types == [
        "heading",  # title
        "header",
        "heading",  # section
        "paragraph",
        "list",
        "table",
        "figure",
        "page_number",
        "footer",
    ], types


def test_reading_order_geometry_fallback() -> None:
    """With no PAGE block, LAYOUT blocks sort by geometry top-then-left."""
    blocks = [
        {
            "BlockType": "LAYOUT_TEXT",
            "Id": "b-bottom",
            "Geometry": {"BoundingBox": {"Top": 0.8, "Left": 0.1}},
        },
        {
            "BlockType": "LAYOUT_TITLE",
            "Id": "b-top",
            "Geometry": {"BoundingBox": {"Top": 0.1, "Left": 0.1}},
        },
    ]
    elements = _blocks_to_elements(blocks)
    assert [e["type"] for e in elements] == ["heading", "paragraph"]


def test_blocks_to_elements_is_pure(monkeypatch: pytest.MonkeyPatch) -> None:
    """_blocks_to_elements does not import or call boto3."""
    mock_boto3 = MagicMock()
    monkeypatch.setitem(sys.modules, "boto3", mock_boto3)

    _blocks_to_elements(_load_blocks())

    mock_boto3.client.assert_not_called()


# ===========================================================================
# Task Group 2 — analyze_page with mocked boto3 (no network)
# ===========================================================================


def _make_boto3_mock(blocks: list[dict[str, Any]]) -> MagicMock:
    """Build a boto3 module mock whose analyze_document returns {"Blocks": blocks}."""
    mock_client = MagicMock()
    mock_client.analyze_document.return_value = {"Blocks": blocks}
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_client
    return mock_boto3


def test_analyze_page_valid_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """analyze_document returning Blocks -> analyze_page returns {"elements": [...]}."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    mock_boto3 = _make_boto3_mock(_load_blocks())
    monkeypatch.setitem(sys.modules, "boto3", mock_boto3)

    reset_textract_call_count()
    result = analyze_page(SAMPLE_IMAGE)

    assert "error" not in result, result
    assert isinstance(result["elements"], list)
    assert result["elements"]  # at least one element
    assert any(e["type"] == "table" for e in result["elements"])


def test_analyze_page_uses_sync_analyze_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """analyze_page makes a single synchronous analyze_document call with the right args."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    mock_boto3 = _make_boto3_mock([])
    monkeypatch.setitem(sys.modules, "boto3", mock_boto3)

    reset_textract_call_count()
    analyze_page(SAMPLE_IMAGE)

    mock_client = mock_boto3.client.return_value
    mock_client.analyze_document.assert_called_once()
    _, kwargs = mock_client.analyze_document.call_args
    assert kwargs["Document"] == {"Bytes": SAMPLE_IMAGE}
    assert kwargs["FeatureTypes"] == ["LAYOUT", "TABLES"]
    # The client is built for textract — no S3, no async API touched.
    assert mock_boto3.client.call_args.args[0] == "textract"


def test_analyze_page_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """analyze_document raising -> analyze_page returns {"error": ...} and does not re-raise."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value.analyze_document.side_effect = Exception("network error")
    monkeypatch.setitem(sys.modules, "boto3", mock_boto3)

    reset_textract_call_count()
    result = analyze_page(SAMPLE_IMAGE)

    assert "error" in result
    assert "network error" in result["error"]
    # A failed call must not increment the counter.
    assert get_textract_call_count() == 0


def test_textract_call_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The counter increments only on success and resets to zero."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    mock_boto3 = _make_boto3_mock([])
    monkeypatch.setitem(sys.modules, "boto3", mock_boto3)

    reset_textract_call_count()
    assert get_textract_call_count() == 0
    analyze_page(SAMPLE_IMAGE)
    assert get_textract_call_count() == 1
    analyze_page(SAMPLE_IMAGE)
    assert get_textract_call_count() == 2
    reset_textract_call_count()
    assert get_textract_call_count() == 0


# ===========================================================================
# Task Group 5 — live integration tier (real Textract; skipped by default)
# ===========================================================================


@pytest.mark.live
def test_analyze_page_live_real_textract() -> None:
    """Send a real rendered page to live Textract and assert the element-JSON shape.

    The only test in Groups 1-5 that touches real AWS. Skipped unless ``-m live``
    is passed (the ``live`` marker is registered in pyproject.toml). Requires
    AWS_REGION + instance-role/Textract access (verified in ap-southeast-2).
    """
    from parser_service.render import render_page

    pdf = FIXTURES / "digital_simple.pdf"
    image_bytes = render_page(pdf, 0)

    reset_textract_call_count()
    result = analyze_page(image_bytes)

    assert "error" not in result, result
    assert isinstance(result["elements"], list)
    assert result["elements"], "live Textract returned no elements"
    assert all("type" in e for e in result["elements"])
    assert get_textract_call_count() == 1
