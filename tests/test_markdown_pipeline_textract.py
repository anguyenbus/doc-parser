"""
test_markdown_pipeline_textract.py — Task Group 3 tests for the
``PARSER_ESCALATION_ENGINE`` switch at the escalation seam (``_vlm_page_markdown``).

These mirror the VLM seam tests in ``test_markdown_pipeline.py`` but exercise the
engine switch with ``textract_client.analyze_page`` mocked (NO AWS):

  - default is ``vlm``: with the env var unset, a promoted page calls ``call_vlm``
    (NOT Textract) — current behavior reproduced bit-for-bit.
  - ``textract`` routes: with ``PARSER_ESCALATION_ENGINE=textract``, a promoted page
    calls ``analyze_page``; its element-JSON flows through the unchanged
    ``_emit_vlm_elements`` → ``render_markdown`` path; route recorded as ``textract``.
  - Textract fallback: all four garbage shapes → page kept Docling, route
    ``textract-fallback-docling``.
  - quality signal only: ``_measure_text_quality`` recorded on the ``textract`` route
    but never flips the route.

Run ONLY these tests (task 3.4):
    uv run pytest tests/test_markdown_pipeline_textract.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
DIGITAL = FIXTURES / "digital_simple.pdf"


def _force_promote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the gate to promote every page (the only pages an engine touches)."""
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("promote_to_vlm", "forced_for_test", layer=1),
    )


def _patch_textract(monkeypatch: pytest.MonkeyPatch, response: Any) -> None:
    """Patch ``analyze_page`` where markdown_pipeline imports it."""

    def _mock(image_bytes: bytes) -> Any:
        return response

    monkeypatch.setattr("parser_service.markdown_pipeline.analyze_page", _mock)


def _patch_vlm(monkeypatch: pytest.MonkeyPatch, response: Any) -> None:
    """Patch ``call_vlm`` where markdown_pipeline imports it."""

    def _mock(image_bytes: bytes, mode: str) -> Any:
        return response

    monkeypatch.setattr("parser_service.markdown_pipeline.call_vlm", _mock)


# ---------------------------------------------------------------------------
# default is vlm: env unset -> call_vlm runs, Textract is never touched.
# ---------------------------------------------------------------------------


def test_default_engine_uses_vlm_not_textract(monkeypatch: pytest.MonkeyPatch) -> None:
    """With PARSER_ESCALATION_ENGINE unset, a promoted page calls the VLM, not Textract."""
    from parser_service import markdown_pipeline

    monkeypatch.delenv("PARSER_ESCALATION_ENGINE", raising=False)
    _force_promote(monkeypatch)
    _patch_vlm(monkeypatch, {"elements": [{"type": "paragraph", "text": "VLM_TEXT"}]})
    # If Textract were called here it would be a bug; make it loudly wrong.
    _patch_textract(monkeypatch, {"error": "textract must not be called when engine=vlm"})

    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    assert "VLM_TEXT" in result["markdown"]
    routes = result["page_routes"]
    assert routes, routes
    assert all(r["route"] == "vlm" for r in routes), routes


# ---------------------------------------------------------------------------
# textract routes: promoted page -> analyze_page -> markdown; route = textract.
# ---------------------------------------------------------------------------


def test_textract_engine_routes_to_analyze_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """engine=textract sends promoted pages through analyze_page and the shared renderer."""
    from parser_service import markdown_pipeline

    monkeypatch.setenv("PARSER_ESCALATION_ENGINE", "textract")
    _force_promote(monkeypatch)
    # call_vlm must NOT run on the textract path.
    _patch_vlm(monkeypatch, {"error": "vlm must not be called when engine=textract"})
    _patch_textract(
        monkeypatch,
        {"elements": [{"type": "heading", "text": "TEXTRACT_HEADING", "level": 1}]},
    )

    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    assert "TEXTRACT_HEADING" in result["markdown"]
    # Flowed through render_markdown (heading level 1 -> "# ").
    assert "# TEXTRACT_HEADING" in result["markdown"]
    routes = result["page_routes"]
    assert routes, routes
    assert all(r["route"] == "textract" for r in routes), routes
    assert all(r["reason"] == "forced_for_test" for r in routes)


# ---------------------------------------------------------------------------
# textract fallback: all four garbage shapes -> textract-fallback-docling.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_response",
    [
        {"error": "boom"},
        {"elements": "not-a-list"},
        {"elements": []},
        {"elements": [{"type": "paragraph", "text": "   "}]},  # whitespace-only md
    ],
)
def test_textract_garbage_falls_back_to_docling(
    monkeypatch: pytest.MonkeyPatch, bad_response: Any
) -> None:
    """All four Textract-garbage shapes keep the page's Docling slice, route textract-fallback-docling."""
    from parser_service import markdown_pipeline

    monkeypatch.setenv("PARSER_ESCALATION_ENGINE", "textract")
    _force_promote(monkeypatch)
    _patch_textract(monkeypatch, bad_response)

    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    # Docling text survives because Textract produced nothing usable.
    assert "page one" in result["markdown"].lower()
    routes = result["page_routes"]
    assert routes, routes
    assert all(r["route"] == "textract-fallback-docling" for r in routes), routes


# ---------------------------------------------------------------------------
# quality signal only: recorded on the textract route, never flips it.
# ---------------------------------------------------------------------------


def test_textract_quality_signal_recorded_but_never_flips_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_measure_text_quality is recorded on the textract route and does not change it."""
    from parser_service import markdown_pipeline

    monkeypatch.setenv("PARSER_ESCALATION_ENGINE", "textract")
    _force_promote(monkeypatch)
    _patch_textract(
        monkeypatch,
        {"elements": [{"type": "paragraph", "text": "Some clean textract content."}]},
    )

    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    routes = result["page_routes"]
    assert routes, routes
    for r in routes:
        assert r["route"] == "textract"
        # signal fields recorded regardless of pass/fail
        assert "vlm_quality_passes" in r
        assert "vlm_quality_failing_signals" in r


# ---------------------------------------------------------------------------
# counter parity: textract counter resets per run on the vlm default path.
# ---------------------------------------------------------------------------


def test_textract_counter_not_incremented_on_vlm_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The vlm default path makes no Textract calls (counter stays at 0)."""
    from parser_service import markdown_pipeline
    from parser_service.textract_client import get_textract_call_count

    monkeypatch.delenv("PARSER_ESCALATION_ENGINE", raising=False)
    _force_promote(monkeypatch)
    _patch_vlm(monkeypatch, {"elements": [{"type": "paragraph", "text": "VLM_TEXT"}]})

    markdown_pipeline.parse_to_markdown(DIGITAL)

    assert get_textract_call_count() == 0


# ===========================================================================
# Task Group 5 — strategic gap fill: mixed keep + promote in one textract run.
# ===========================================================================


def test_textract_end_to_end_mixed_keep_and_promote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """engine=textract: a promoted page routes ``textract`` while a kept page stays
    ``docling-kept`` in the same run (the integration point 3.1 does not cover)."""
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.setenv("PARSER_ESCALATION_ENGINE", "textract")

    # Promote only page 0; keep page 1.
    def _decide(page_idx: int, *a: object, **k: object) -> Decision:
        if page_idx == 0:
            return Decision("promote_to_vlm", "forced_for_test", layer=1)
        return Decision("keep", None, None)

    monkeypatch.setattr(markdown_pipeline, "evaluate_page", _decide)
    _patch_textract(
        monkeypatch,
        {"elements": [{"type": "paragraph", "text": "TEXTRACT_PAGE_ZERO"}]},
    )

    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    routes = {r["page_index"]: r["route"] for r in result["page_routes"]}
    assert routes.get(0) == "textract", result["page_routes"]
    assert routes.get(1) == "docling-kept", result["page_routes"]
    # Page 0 replaced by Textract markdown; page 1 retains its Docling text.
    md = result["markdown"]
    assert "TEXTRACT_PAGE_ZERO" in md
    assert "page two" in md.lower()
