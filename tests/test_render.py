"""
test_render.py

Unit tests for render.py — PDF page rendering and coordinate conversion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from parser_service.render import render_page, render_region

FIXTURES = Path(__file__).parent / "fixtures"
DIGITAL_PDF = FIXTURES / "digital_simple.pdf"


# ---------------------------------------------------------------------------
# Test 1: render_page returns valid PNG bytes
# ---------------------------------------------------------------------------


def test_render_page_returns_png_bytes() -> None:
    """render_page returns bytes starting with the PNG magic header."""
    result = render_page(DIGITAL_PDF, page_no=0)
    assert isinstance(result, bytes)
    assert len(result) > 0
    assert result[:4] == b"\x89PNG", "Expected PNG magic header"


# ---------------------------------------------------------------------------
# Test 2: render_region returns PNG bytes for a 1-inch square crop
# ---------------------------------------------------------------------------


def test_render_region_returns_png_bytes() -> None:
    """render_region on a 72x72 pt (1-inch) crop returns non-empty PNG bytes."""
    # bbox in PDF coordinate space (bottom-left origin, points)
    # A 1-inch square starting at (0, 720) to (72, 792) — upper-left area of page
    bbox = {"x0": 0.0, "y0": 720.0, "x1": 72.0, "y1": 792.0}
    result = render_region(DIGITAL_PDF, page_no=0, bbox=bbox)
    assert isinstance(result, bytes)
    assert len(result) > 0
    assert result[:4] == b"\x89PNG", "Expected PNG magic header"


# ---------------------------------------------------------------------------
# Test 3: render_region with degenerate bbox raises ValueError
# ---------------------------------------------------------------------------


def test_render_region_zero_width_raises_value_error() -> None:
    """render_region raises ValueError when the crop has zero width after conversion."""
    # x0 == x1 → zero-width crop
    bbox = {"x0": 100.0, "y0": 100.0, "x1": 100.0, "y1": 200.0}
    with pytest.raises(ValueError, match="zero-dimension crop"):
        render_region(DIGITAL_PDF, page_no=0, bbox=bbox)


def test_render_region_zero_height_raises_value_error() -> None:
    """render_region raises ValueError when the crop has zero height after conversion."""
    # In PDF space, y0 == y1 → zero-height crop
    bbox = {"x0": 0.0, "y0": 300.0, "x1": 72.0, "y1": 300.0}
    with pytest.raises(ValueError, match="zero-dimension crop"):
        render_region(DIGITAL_PDF, page_no=0, bbox=bbox)


# ---------------------------------------------------------------------------
# Additional: render_page on page 1 also works
# ---------------------------------------------------------------------------


def test_render_page_second_page() -> None:
    """render_page works for the second page (page_no=1)."""
    result = render_page(DIGITAL_PDF, page_no=1)
    assert isinstance(result, bytes)
    assert result[:4] == b"\x89PNG"


# ---------------------------------------------------------------------------
# Additional: render_region with valid crop produces non-zero dimension image
# ---------------------------------------------------------------------------


def test_render_region_center_strip() -> None:
    """render_region on a center strip of the page returns valid PNG."""
    # PDF page is 612 x 792 pts; center horizontal strip
    bbox = {"x0": 100.0, "y0": 300.0, "x1": 512.0, "y1": 500.0}
    result = render_region(DIGITAL_PDF, page_no=0, bbox=bbox)
    assert result[:4] == b"\x89PNG"
    assert len(result) > 100  # must be a real image, not trivially small
