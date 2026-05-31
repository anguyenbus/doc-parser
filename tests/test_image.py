"""
test_image.py

Unit tests for the image (PNG/JPEG/TIFF) parsing path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Test 3 (Phase 3f): parse(screenshot.png) with mock_vlm_page
#   — all elements have vlm_p0_ IDs
# ---------------------------------------------------------------------------


def test_image_all_elements_have_vlm_p0_ids(mock_vlm_page: None) -> None:
    """parse(screenshot.png) with mocked VLM — all element IDs start with vlm_p0_."""
    from parser_service.parser_service import parse

    result = parse(FIXTURES / "screenshot.png")

    assert result["schema_version"] == "1.0.0"
    assert len(result["pages"]) == 1
    assert result["pages"][0]["page_index"] == 0

    elements = result["elements"]
    assert len(elements) > 0, "Expected at least one element from VLM page response"

    for elem in elements:
        assert elem["element_id"].startswith("vlm_p0_"), (
            f"Expected vlm_p0_* ID, got: {elem['element_id']!r}"
        )


# ---------------------------------------------------------------------------
# Test 4 (Phase 3f): parse(screenshot.png) with mock_vlm_error
#   — image_unparseable warning emitted
# ---------------------------------------------------------------------------


def test_image_vlm_error_produces_image_unparseable_warning(mock_vlm_error: None) -> None:
    """parse(screenshot.png) with mocked VLM error → image_unparseable warning."""
    from parser_service.parser_service import parse

    result = parse(FIXTURES / "screenshot.png")

    warning_codes = [w["code"] for w in result["warnings"]]
    assert "image_unparseable" in warning_codes

    # Verify scope and page_index
    image_warns = [w for w in result["warnings"] if w["code"] == "image_unparseable"]
    assert len(image_warns) >= 1
    assert image_warns[0].get("scope") == "page"
    assert image_warns[0].get("page_index") == 0


# ---------------------------------------------------------------------------
# Additional: unsupported type produces unsupported_type warning, no exception
# ---------------------------------------------------------------------------


def test_unsupported_type_warning(tmp_path: Path) -> None:
    """parse() on an unsupported file type returns unsupported_type warning."""
    from parser_service.parser_service import parse

    unknown_file = tmp_path / "mystery.xyz123"
    unknown_file.write_bytes(b"some content")

    result = parse(unknown_file)

    assert "schema_version" in result
    warning_codes = [w["code"] for w in result["warnings"]]
    assert "unsupported_type" in warning_codes


# ---------------------------------------------------------------------------
# Additional: vlm_invalid_shape warning when elements is not a list
# ---------------------------------------------------------------------------


def test_image_vlm_invalid_shape_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """parse(screenshot.png) with VLM returning non-list elements → vlm_invalid_shape."""
    from parser_service.parser_service import parse

    def _mock_call_vlm(image_bytes: bytes, mode: str) -> dict[str, Any]:
        return {"elements": {"key": "not a list"}}

    monkeypatch.setattr("parser_service.parser_service.call_vlm", _mock_call_vlm)

    result = parse(FIXTURES / "screenshot.png")

    warning_codes = [w["code"] for w in result["warnings"]]
    assert "vlm_invalid_shape" in warning_codes
