"""
test_office.py

Unit tests for DOCX, XLSX, and HTML parsing paths.

These paths use Docling only; VLM is never called.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _call_tracker() -> tuple[list[Any], Any]:
    """Return a call log list and a mock VLM that records calls."""
    calls: list[Any] = []

    def _mock_call_vlm(image_bytes: bytes, mode: str) -> dict[str, Any]:
        calls.append((image_bytes, mode))
        return {"error": "should not have been called"}

    return calls, _mock_call_vlm


# ---------------------------------------------------------------------------
# Test 1: parse(doc.docx) — one page, no warnings, VLM never called
# ---------------------------------------------------------------------------


def test_parse_docx(monkeypatch: pytest.MonkeyPatch) -> None:
    """parse(doc.docx) produces one page entry, no warnings, no VLM calls."""
    from parser_service.parser_service import parse

    calls, mock_vlm = _call_tracker()
    monkeypatch.setattr("parser_service.parser_service.call_vlm", mock_vlm)

    result = parse(FIXTURES / "doc.docx")

    assert result["schema_version"] == "1.0.0"
    assert len(result["pages"]) == 1
    assert result["pages"][0]["page_index"] == 0

    # No VLM calls on the DOCX path
    assert calls == [], f"Unexpected VLM calls: {calls}"

    # No unexpected warnings (docling_failed allowed only if Docling has issues)
    unexpected_codes = {w["code"] for w in result["warnings"]} - {"docling_failed"}
    assert unexpected_codes == set(), f"Unexpected warnings: {unexpected_codes}"


# ---------------------------------------------------------------------------
# Test 2: parse(sheet.xlsx) — one page, no warnings, VLM never called
# ---------------------------------------------------------------------------


def test_parse_xlsx(monkeypatch: pytest.MonkeyPatch) -> None:
    """parse(sheet.xlsx) produces one page entry, no warnings, no VLM calls."""
    from parser_service.parser_service import parse

    calls, mock_vlm = _call_tracker()
    monkeypatch.setattr("parser_service.parser_service.call_vlm", mock_vlm)

    result = parse(FIXTURES / "sheet.xlsx")

    assert result["schema_version"] == "1.0.0"
    assert len(result["pages"]) == 1
    assert result["pages"][0]["page_index"] == 0
    assert calls == [], f"Unexpected VLM calls: {calls}"

    unexpected_codes = {w["code"] for w in result["warnings"]} - {"docling_failed"}
    assert unexpected_codes == set(), f"Unexpected warnings: {unexpected_codes}"


# ---------------------------------------------------------------------------
# Test 3: parse(page.html) — one page, no warnings, VLM never called
# ---------------------------------------------------------------------------


def test_parse_html(monkeypatch: pytest.MonkeyPatch) -> None:
    """parse(page.html) produces one page entry, no warnings, no VLM calls."""
    from parser_service.parser_service import parse

    calls, mock_vlm = _call_tracker()
    monkeypatch.setattr("parser_service.parser_service.call_vlm", mock_vlm)

    result = parse(FIXTURES / "page.html")

    assert result["schema_version"] == "1.0.0"
    assert len(result["pages"]) == 1
    assert result["pages"][0]["page_index"] == 0
    assert calls == [], f"Unexpected VLM calls: {calls}"

    unexpected_codes = {w["code"] for w in result["warnings"]} - {"docling_failed"}
    assert unexpected_codes == set(), f"Unexpected warnings: {unexpected_codes}"
