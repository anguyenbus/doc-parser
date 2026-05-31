"""
test_render_pixel_budget.py

Tests for the render-time megapixel guard (issue #2: PDF rasterizer OOM bomb).

Layers:
  A. Pure helper unit tests — _clamp_scale_to_budget, no allocation.
  B. Integration via env — render_page/render_region honor PARSER_MAX_RENDER_MP
     end-to-end on the real fixture (small, safe allocations).
  C. Spy test — a huge declared MediaBox is clamped without allocating it.
  D. PIL guard — Image.MAX_IMAGE_PIXELS is set explicitly at import.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from parser_service import render
from parser_service.render import (
    _clamp_scale_to_budget,
    _max_render_pixels,
    render_page,
    render_region,
)

FIXTURES = Path(__file__).parent / "fixtures"
DIGITAL_PDF = FIXTURES / "digital_simple.pdf"


# ---------------------------------------------------------------------------
# A. Pure helper unit tests (no allocation)
# ---------------------------------------------------------------------------


def test_clamp_under_budget_passes_through() -> None:
    """A Letter page at scale 2 (~2.4 MP) is well under a 40 MP budget."""
    scale, clamped = _clamp_scale_to_budget(612.0, 792.0, 2.0, 40_000_000)
    assert clamped is False
    assert scale == 2.0


def test_clamp_over_budget_reduces_to_ceiling() -> None:
    """An over-budget render is clamped so output area <= max_pixels."""
    w, h, max_px = 5000.0, 5000.0, 40_000_000
    scale, clamped = _clamp_scale_to_budget(w, h, 4.0, max_px)
    assert clamped is True
    # Output pixels must not exceed the budget (allow tiny float rounding).
    assert (w * scale) * (h * scale) <= max_px * 1.0001


def test_clamp_gigapixel_mediabox_no_overflow() -> None:
    """An adversarial 200k x 200k pt MediaBox clamps to a tiny scale, area <= budget."""
    w = h = 200_000.0
    max_px = 40_000_000
    scale, clamped = _clamp_scale_to_budget(w, h, 2.0, max_px)
    assert clamped is True
    assert scale > 0.0
    assert (w * scale) * (h * scale) <= max_px * 1.0001


@pytest.mark.parametrize(
    "w,h,max_px",
    [
        (0.0, 792.0, 40_000_000),  # zero width
        (612.0, 0.0, 40_000_000),  # zero height
        (-1.0, 792.0, 40_000_000),  # negative dim
        (612.0, 792.0, 0),  # zero budget
    ],
)
def test_clamp_degenerate_inputs_pass_through(w: float, h: float, max_px: int) -> None:
    """Degenerate inputs return the scale unchanged without dividing by zero."""
    scale, clamped = _clamp_scale_to_budget(w, h, 1.5, max_px)
    assert scale == 1.5
    assert clamped is False


def test_clamp_monotonic_in_budget() -> None:
    """Doubling the budget never decreases the effective scale."""
    w, h = 5000.0, 5000.0
    s_small, _ = _clamp_scale_to_budget(w, h, 4.0, 20_000_000)
    s_large, _ = _clamp_scale_to_budget(w, h, 4.0, 40_000_000)
    assert s_large >= s_small


def test_max_render_pixels_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_max_render_pixels converts PARSER_MAX_RENDER_MP (megapixels) to pixels."""
    monkeypatch.setenv("PARSER_MAX_RENDER_MP", "10")
    assert _max_render_pixels() == 10_000_000
    monkeypatch.delenv("PARSER_MAX_RENDER_MP", raising=False)
    assert _max_render_pixels() == 40_000_000  # default


# ---------------------------------------------------------------------------
# B. Integration: real render path honors the budget end-to-end
# ---------------------------------------------------------------------------


def _png_dimensions(png_bytes: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(png_bytes)).size


def test_render_page_respects_low_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A very low budget forces render_page to downscale the real fixture."""
    monkeypatch.setenv("PARSER_MAX_RENDER_MP", "0.05")  # 50k pixels
    png = render_page(DIGITAL_PDF, page_no=0)
    w, h = _png_dimensions(png)
    assert w * h <= 50_000 * 1.05  # within budget (+ rounding slack)
    assert png[:4] == b"\x89PNG"


def test_render_region_respects_low_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """render_region clamps the full-page raster to the budget and still crops."""
    monkeypatch.setenv("PARSER_MAX_RENDER_MP", "0.05")
    bbox = {"x0": 100.0, "y0": 300.0, "x1": 512.0, "y1": 500.0}
    png = render_region(DIGITAL_PDF, page_no=0, bbox=bbox)
    assert png[:4] == b"\x89PNG"
    w, h = _png_dimensions(png)
    # The crop is a fraction of the (already-clamped) page, so it is well under.
    assert w * h <= 50_000 * 1.05


def test_render_page_unaffected_at_default_budget() -> None:
    """At the 40 MP default the small fixture renders at full requested scale."""
    png = render_page(DIGITAL_PDF, page_no=0)
    w, h = _png_dimensions(png)
    # Letter @ 144 DPI = 612*2 x 792*2 = 1224 x 1584.
    assert (w, h) == (1224, 1584)


# ---------------------------------------------------------------------------
# C. Spy test: huge declared MediaBox is clamped without allocating it
# ---------------------------------------------------------------------------


def test_huge_mediabox_clamped_via_spy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A page reporting huge point dims gets a reduced scale before render().

    We monkeypatch _clamp_render_scale's view of the page by faking get_width/
    get_height to enormous values, and assert the scale passed to the budget
    helper is reduced — proving the guard fires on declared size, not output.
    """
    captured: dict[str, float] = {}
    real_clamp = render._clamp_scale_to_budget

    def spy_clamp(w: float, h: float, scale: float, max_px: int) -> tuple[float, bool]:
        result = real_clamp(w, h, scale, max_px)
        captured["in_scale"] = scale
        captured["out_scale"] = result[0]
        captured["clamped"] = float(result[1])
        captured["width_pts"] = w
        return result

    monkeypatch.setattr(render, "_clamp_scale_to_budget", spy_clamp)

    class _FakePage:
        def get_width(self) -> float:
            return 200_000.0

        def get_height(self) -> float:
            return 200_000.0

    scale = render._clamp_render_scale(_FakePage(), 2.0, DIGITAL_PDF, 0)
    assert captured["width_pts"] == 200_000.0
    assert captured["clamped"] == 1.0
    assert scale < 2.0
    assert scale == captured["out_scale"]


# ---------------------------------------------------------------------------
# D. PIL decompression-bomb guard set explicitly at package import
# ---------------------------------------------------------------------------


def test_pil_max_image_pixels_set_explicitly() -> None:
    """Importing parser_service sets a deliberate PIL bomb ceiling."""
    import parser_service  # noqa: F401  (import triggers the assignment)

    assert Image.MAX_IMAGE_PIXELS == 128_000_000
