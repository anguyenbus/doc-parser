"""test_confidence_document.py — Task Group 3 tests.

Content-weighted ``document_confidence`` aggregation, the ``confidence`` block on
the ``parse_to_markdown`` return, additive ``low_confidence_page`` warnings, and a
byte-identical regression proving the feature is purely additive.

Aggregation tests are pure (hand-built ``page_routes``). The emission /
regression tests exercise a full ``parse_to_markdown`` with ``call_vlm`` + the
gate mocked. Offline. No AWS.

Run ONLY these tests (task 3.5):
    uv run pytest tests/test_confidence_document.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from parser_service.confidence import (
    LOW_CONFIDENCE_THRESHOLD,
    document_confidence,
    page_confidence,
)

FIXTURES = Path(__file__).parent / "fixtures"
DIGITAL = FIXTURES / "digital_simple.pdf"


# ---------------------------------------------------------------------------
# Pure aggregation.
# ---------------------------------------------------------------------------


def _rec(route: str, n_chars: int, **extra: Any) -> dict[str, Any]:
    return {"page_index": 0, "route": route, "reason": None, "n_chars": n_chars, **extra}


def test_all_docling_kept_doc_scores_0_95() -> None:
    routes = [_rec("docling-kept", 100) for _ in range(3)]
    assert document_confidence(routes) == pytest.approx(0.95)


def test_all_fallback_doc_scores_0_50() -> None:
    routes = [_rec("vlm-fallback-docling", 100) for _ in range(3)]
    assert document_confidence(routes) == pytest.approx(0.50)


def test_clean_doc_scores_above_fallback_doc() -> None:
    clean = [_rec("docling-kept", 100) for _ in range(3)]
    bad = [_rec("vlm-fallback-docling", 100) for _ in range(3)]
    assert document_confidence(clean) > document_confidence(bad)


def test_long_clean_doc_with_one_tiny_bad_page_stays_high() -> None:
    routes = [
        _rec("docling-kept", 10_000),
        _rec("docling-kept", 10_000),
        _rec("vlm-fallback-docling", 5),  # tiny bad page
    ]
    score = document_confidence(routes)
    assert score > 0.94  # dominated by the two long clean pages
    # Convex combination stays within the tier band [min_page, max_page].
    assert 0.50 <= score <= 0.95


def test_doc_dominated_by_long_low_tier_page_scores_near_that_tier() -> None:
    routes = [
        _rec("docling-kept", 5),
        _rec("vlm-fallback-docling", 10_000),  # long bad page dominates
    ]
    score = document_confidence(routes)
    assert score < 0.55
    assert score >= 0.50  # never below the worst page's tier


def test_sum_weight_zero_scores_0_0() -> None:
    routes = [_rec("docling-kept", 0), _rec("vlm-fallback-docling", 0)]
    assert document_confidence(routes) == 0.0


def test_empty_page_routes_scores_0_0() -> None:
    assert document_confidence([]) == 0.0


def test_document_within_min_max_page_band() -> None:
    routes = [
        _rec("docling-kept", 300),
        _rec("vlm", 200, engine_quality_passes=True),
        _rec("vlm-fallback-docling", 400),
    ]
    scores = [page_confidence(r) for r in routes]
    doc = document_confidence(routes)
    assert min(scores) <= doc <= max(scores)


# ---------------------------------------------------------------------------
# Emission: parse_to_markdown returns a confidence block; existing keys stay.
# ---------------------------------------------------------------------------


def _patch_vlm(monkeypatch: pytest.MonkeyPatch, response: Any) -> None:
    monkeypatch.setattr(
        "parser_service.markdown_pipeline.call_vlm",
        lambda image_bytes, mode: response,
    )


def _force_keep(monkeypatch: pytest.MonkeyPatch) -> None:
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.setattr(
        markdown_pipeline, "evaluate_page", lambda *a, **k: Decision("keep", None, None)
    )


def _force_promote(monkeypatch: pytest.MonkeyPatch, reason: str, layer: int) -> None:
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("promote_to_vlm", reason, layer=layer),
    )


def test_return_shape_has_confidence_block(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_keep(monkeypatch)
    _patch_vlm(monkeypatch, {"error": "unused"})

    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(DIGITAL)
    assert {"markdown", "page_routes", "warnings", "confidence"} <= set(result)
    conf = result["confidence"]
    assert set(conf) == {"document", "pages"}
    assert isinstance(conf["document"], float)
    assert 0.0 <= conf["document"] <= 1.0
    # One entry per page_routes record, aligned page indices.
    assert len(conf["pages"]) == len(result["page_routes"])
    for pconf, rec in zip(conf["pages"], result["page_routes"], strict=True):
        assert set(pconf) == {"page_index", "confidence"}
        assert pconf["page_index"] == rec["page_index"]
        assert pconf["confidence"] == page_confidence(rec)


def test_all_kept_document_is_docling_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_keep(monkeypatch)
    _patch_vlm(monkeypatch, {"error": "unused"})

    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(DIGITAL)
    assert result["confidence"]["document"] == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# low_confidence_page warning (< LOW_CONFIDENCE_THRESHOLD).
# ---------------------------------------------------------------------------


def test_low_confidence_page_warning_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force every page through the error/empty fallback (score 0.15) by rendering
    # all pages empty and failing the engine.
    from parser_service import markdown_pipeline

    monkeypatch.setattr(markdown_pipeline, "_render_page_markdown", lambda *a, **k: "")
    _patch_vlm(monkeypatch, {"error": "boom"})

    result = markdown_pipeline.parse_to_markdown(DIGITAL)
    low = [w for w in result["warnings"] if w["code"] == "low_confidence_page"]
    assert low, result["warnings"]
    for w in low:
        assert w["scope"] == "page"
        assert isinstance(w["page_index"], int)
        assert "advisory" in w["message"].lower()
    # One low-confidence warning per below-threshold page.
    below = [
        p
        for p in result["confidence"]["pages"]
        if p["confidence"] < LOW_CONFIDENCE_THRESHOLD
    ]
    assert len(low) == len(below)
    assert {w["page_index"] for w in low} == {p["page_index"] for p in below}


def test_fallback_docling_page_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    # Engine down but Docling produced content → vlm-fallback-docling (0.50). The
    # gate flagged the page (promoted) AND the engine failed, so the pipeline does
    # not trust the shipped output — it must surface for review even though Docling
    # text shipped. (0.50 < 0.65 threshold.)
    _force_promote(monkeypatch, reason="docling_low_grade=POOR", layer=1)
    _patch_vlm(monkeypatch, {"error": "engine down"})

    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(DIGITAL)
    assert all(p["confidence"] == 0.50 for p in result["confidence"]["pages"])
    low = [w for w in result["warnings"] if w["code"] == "low_confidence_page"]
    assert len(low) == len(result["confidence"]["pages"])  # every fallback page flagged


def test_no_low_confidence_warning_when_all_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_keep(monkeypatch)  # all docling-kept → 0.95, none below threshold
    _patch_vlm(monkeypatch, {"error": "unused"})

    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(DIGITAL)
    assert all(
        p["confidence"] >= LOW_CONFIDENCE_THRESHOLD
        for p in result["confidence"]["pages"]
    )
    assert not [w for w in result["warnings"] if w["code"] == "low_confidence_page"]


# ---------------------------------------------------------------------------
# Byte-identical regression: markdown + pre-existing page_routes fields + the
# pre-existing warnings are unchanged; only n_chars / confidence / any
# low_confidence_page warning are new.
# ---------------------------------------------------------------------------


# A CLEAN engine rendering (normal English) so it passes the text-quality proxy
# → engine-passing tier 0.85, above the review threshold → no advisory warning.
# (An underscore/uppercase sentinel would FAIL the proxy and score 0.60, which is
# below the 0.65 threshold and would correctly warn — not what this regression
# wants to assert.)
_ENGINE_TEXT = "Quarterly revenue rose twelve percent across every regional business unit."


def test_byte_identical_additive_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_promote(monkeypatch, reason="forced_for_test", layer=1)
    _patch_vlm(
        monkeypatch,
        {"elements": [{"type": "paragraph", "text": _ENGINE_TEXT}]},
    )

    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(DIGITAL)

    # markdown must be unchanged by the additive feature — it is exactly the
    # concatenation the pipeline shipped (engine replacement text present).
    assert _ENGINE_TEXT in result["markdown"]

    # page_routes pre-existing fields unchanged: stripping n_chars yields the
    # pre-feature record shape (page_index / route / reason [+ quality booleans
    # / arbitration where the shape carries them]).
    # Pre-existing engine-success record (arbitration off) shape: page_index /
    # route / reason plus the pre-existing vlm_quality_* signal keys. The ONLY new
    # key the feature may add is n_chars.
    expected_preexisting = {
        "page_index",
        "route",
        "reason",
        "vlm_quality_passes",
        "vlm_quality_failing_signals",
    }
    for rec in result["page_routes"]:
        stripped = {k: v for k, v in rec.items() if k != "n_chars"}
        assert set(stripped) == expected_preexisting
        assert stripped["route"] == "vlm"
        assert stripped["reason"] == "forced_for_test"
        assert rec["n_chars"] == len(_ENGINE_TEXT)

    # No low_confidence_page warnings for an all-passing engine doc (0.85 > 0.65),
    # and every remaining warning is a pre-existing code (not the new advisory one).
    assert not [w for w in result["warnings"] if w["code"] == "low_confidence_page"]
