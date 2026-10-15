"""
test_office.py

DOCX, XLSX and HTML through ``parse_to_markdown``: Docling only, one logical page,
no warnings, and the VLM never called. An unsupported file type is a warning, not
an exception.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from parser_service.parsing.document import parse_to_markdown

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("name", ["doc.docx", "sheet.xlsx", "page.html"])
def test_whole_document_formats_never_call_the_vlm(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []

    def mock_vlm(image_bytes: bytes, mode: str) -> dict[str, Any]:
        calls.append(mode)
        return {"error": "should not have been called"}

    monkeypatch.setattr("parser_service.parsing.document.call_vlm", mock_vlm)
    result = parse_to_markdown(FIXTURES / name)

    assert [r["page_index"] for r in result["page_routes"]] == [0]
    assert result["page_routes"][0]["route"] == "docling-kept"
    assert result["markdown"].strip()
    assert result["warnings"] == []
    assert calls == [] and result["call_counts"] == {"vlm": 0, "textract": 0}


def test_unsupported_type_is_a_warning(tmp_path: Path) -> None:
    unknown_file = tmp_path / "mystery.xyz123"
    unknown_file.write_bytes(b"some content")

    result = parse_to_markdown(unknown_file)

    assert result["markdown"] == ""
    assert [w["code"] for w in result["warnings"]] == ["unsupported_type"]
