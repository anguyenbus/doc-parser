"""test_arbitration_constants.py — Task Group 1 tests for the arbitration
route constants + page-record telemetry field vocabulary.

Offline (no network / no AWS). These assert:

  - The two new rejected-kept-docling route constants exist and match
    byte-for-byte across ``markdown_pipeline`` and ``route_stats``.
  - A ``page_routes`` entry produced by the seam with arbitration ON (fired)
    carries the new telemetry keys (``engine_quality_*`` / ``docling_quality_*``
    / ``arbitration``) while STILL carrying the back-compat
    ``vlm_quality_*`` keys.

Run ONLY these tests (task 1.5):
    uv run pytest tests/test_arbitration_constants.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
DIGITAL = FIXTURES / "digital_simple.pdf"


# ---------------------------------------------------------------------------
# Constants match byte-for-byte across the two modules.
# ---------------------------------------------------------------------------


def test_rejected_route_constant_values() -> None:
    """The two new route constants have the exact rejected-kept-docling values."""
    from parser_service import markdown_pipeline, route_stats

    assert markdown_pipeline._ROUTE_VLM_REJECTED == "vlm-rejected-kept-docling"
    assert (
        markdown_pipeline._ROUTE_TEXTRACT_REJECTED == "textract-rejected-kept-docling"
    )
    assert route_stats.ROUTE_VLM_REJECTED == "vlm-rejected-kept-docling"
    assert route_stats.ROUTE_TEXTRACT_REJECTED == "textract-rejected-kept-docling"


def test_rejected_route_constants_match_across_modules() -> None:
    """markdown_pipeline and route_stats agree on both new route constants."""
    from parser_service import markdown_pipeline, route_stats

    assert markdown_pipeline._ROUTE_VLM_REJECTED == route_stats.ROUTE_VLM_REJECTED
    assert (
        markdown_pipeline._ROUTE_TEXTRACT_REJECTED
        == route_stats.ROUTE_TEXTRACT_REJECTED
    )


# ---------------------------------------------------------------------------
# Telemetry vocabulary: a fired arbitration record carries new + back-compat keys.
# ---------------------------------------------------------------------------


def _force_promote_layer1(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a Layer-1 (docling_low_grade) promotion on every page."""
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision(
            "promote_to_vlm", "docling_low_grade=POOR", layer=1
        ),
    )


def _patch_vlm(monkeypatch: pytest.MonkeyPatch, response: Any) -> None:
    def _mock(image_bytes: bytes, mode: str) -> Any:
        return response

    monkeypatch.setattr("parser_service.markdown_pipeline.call_vlm", _mock)


def _patch_quality(
    monkeypatch: pytest.MonkeyPatch, engine_passes: bool, docling_passes: bool
) -> None:
    """Force ``_measure_text_quality`` to a deterministic pass/fail by content.

    The engine markdown carries a unique sentinel so the mock can distinguish
    engine vs docling text.
    """
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import QualitySignals

    def _mock(text: str) -> QualitySignals:
        if "ENGINE_SENTINEL" in text:
            return QualitySignals(
                failing_signals=[] if engine_passes else ["garbled_ratio"]
            )
        return QualitySignals(
            failing_signals=[] if docling_passes else ["garbled_ratio"]
        )

    monkeypatch.setattr(markdown_pipeline, "_measure_text_quality", _mock)


def test_fired_record_carries_new_and_backcompat_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arbitration ON + fired: record has new telemetry keys AND back-compat keys."""
    from parser_service import markdown_pipeline

    monkeypatch.setenv("PARSER_ESCALATION_ARBITRATION", "1")
    monkeypatch.delenv("PARSER_ESCALATION_ENGINE", raising=False)
    _force_promote_layer1(monkeypatch)
    _patch_vlm(
        monkeypatch,
        {"elements": [{"type": "paragraph", "text": "ENGINE_SENTINEL text"}]},
    )
    # Engine fails, Docling passes -> arbitration fires (kept-docling).
    _patch_quality(monkeypatch, engine_passes=False, docling_passes=True)

    result = markdown_pipeline.parse_to_markdown(DIGITAL)
    routes = result["page_routes"]
    assert routes, routes
    r = routes[0]

    # New telemetry keys present.
    assert "engine_quality_passes" in r
    assert "engine_quality_failing_signals" in r
    assert "docling_quality_passes" in r
    assert "docling_quality_failing_signals" in r
    assert r["arbitration"] == "kept-docling"

    # Back-compat keys still present and mirror the engine signals.
    assert "vlm_quality_passes" in r
    assert "vlm_quality_failing_signals" in r
    assert r["vlm_quality_passes"] == r["engine_quality_passes"]
    assert r["vlm_quality_failing_signals"] == r["engine_quality_failing_signals"]

    # Fired route is the rejected-kept-docling route.
    assert r["route"] == "vlm-rejected-kept-docling"
