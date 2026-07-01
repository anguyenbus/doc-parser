"""
test_cost_accounting_budget.py — Group 3 (Part C) tests for the document-level
``--budget-usd`` cap.

Offline: the halt decision is a pure batch helper driven by synthetic per-file
spend; the "Docling-only, no engine calls" behavior is exercised via the additive
``escalate=False`` path on ``parse_to_markdown`` with the engines patched to blow
up if called. No AWS.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "parse_batch", Path(__file__).parent.parent / "scripts" / "parse_batch.py"
)
assert _SPEC and _SPEC.loader
parse_batch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(parse_batch)

FIXTURES = Path(__file__).parent / "fixtures"
DIGITAL = FIXTURES / "digital_simple.pdf"


def _result(vlm: int, textract: int) -> dict[str, Any]:
    return {"filename": "f", "success": True, "vlm_calls": vlm, "textract_calls": textract}


# ---------------------------------------------------------------------------
# _file_cost: per-file spend from the Part B cost table.
# ---------------------------------------------------------------------------


def test_file_cost_uses_both_engines() -> None:
    cost = parse_batch._file_cost(_result(vlm=2, textract=3))
    expected = 2 * parse_batch._AVG_COST_PER_CALL + 3 * parse_batch.TEXTRACT_PRICE_PER_PAGE
    assert cost == pytest.approx(expected)


# ---------------------------------------------------------------------------
# BudgetTracker: document-boundary running-spend gate.
# ---------------------------------------------------------------------------


def test_budget_tracker_disabled_when_no_budget() -> None:
    """No budget => escalation always allowed, never exceeded."""
    tracker = parse_batch.BudgetTracker(budget_usd=None)
    assert tracker.allows_escalation() is True
    tracker.record(_result(vlm=1000, textract=1000), file_index=0)
    assert tracker.allows_escalation() is True
    assert tracker.budget_exceeded is False


def test_budget_tracker_trips_at_document_boundary() -> None:
    """Once running spend exceeds the budget, escalation is halted deterministically."""
    # One VLM call ≈ _AVG_COST_PER_CALL. Budget just above one file's spend so the
    # SECOND completed file trips it.
    per_file = parse_batch._AVG_COST_PER_CALL
    tracker = parse_batch.BudgetTracker(budget_usd=per_file * 1.5)

    # Before any spend: escalation allowed.
    assert tracker.allows_escalation() is True

    # File 0 completes (spends one call) -> still under budget.
    tracker.record(_result(vlm=1, textract=0), file_index=0)
    assert tracker.allows_escalation() is True
    assert tracker.budget_exceeded is False

    # File 1 completes -> running spend now exceeds the ceiling.
    tracker.record(_result(vlm=1, textract=0), file_index=1)
    assert tracker.allows_escalation() is False
    assert tracker.budget_exceeded is True
    # The trip index is recorded (the file index after which escalation is off).
    assert tracker.exceeded_at_file_index == 1


def test_budget_tracker_summary_fields() -> None:
    tracker = parse_batch.BudgetTracker(budget_usd=parse_batch._AVG_COST_PER_CALL * 0.5)
    tracker.record(_result(vlm=1, textract=0), file_index=0)
    fields = tracker.summary_fields()
    assert fields["budget_exceeded"] is True
    assert fields["budget_exceeded_at_file_index"] == 0
    assert "running_spend_usd" in fields


def test_budget_tracker_no_trip_when_under() -> None:
    """A budget above total spend never trips; summary reflects not-exceeded."""
    tracker = parse_batch.BudgetTracker(budget_usd=1000.0)
    tracker.record(_result(vlm=2, textract=2), file_index=0)
    tracker.record(_result(vlm=1, textract=1), file_index=1)
    assert tracker.allows_escalation() is True
    fields = tracker.summary_fields()
    assert fields["budget_exceeded"] is False
    assert fields["budget_exceeded_at_file_index"] is None


# ---------------------------------------------------------------------------
# escalate=False path: Docling-only, no engine calls, no crash.
# ---------------------------------------------------------------------------


def test_escalate_false_makes_no_engine_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """With escalate=False every page stays Docling; call_vlm/analyze_page never fire."""
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.delenv("PARSER_ESCALATION_ENGINE", raising=False)
    # Force the gate to WANT to promote every page — escalate=False must still
    # suppress the engine call.
    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("promote_to_vlm", "forced_for_test", layer=1),
    )

    def _boom_vlm(*a: object, **k: object) -> Any:
        raise AssertionError("call_vlm must not be called when escalate=False")

    def _boom_textract(*a: object, **k: object) -> Any:
        raise AssertionError("analyze_page must not be called when escalate=False")

    monkeypatch.setattr("parser_service.markdown_pipeline.call_vlm", _boom_vlm)
    monkeypatch.setattr("parser_service.markdown_pipeline.analyze_page", _boom_textract)

    result = markdown_pipeline.parse_to_markdown(DIGITAL, escalate=False)

    # No engine calls happened.
    assert result["call_counts"]["vlm"] == 0
    assert result["call_counts"]["textract"] == 0
    # Still produced Docling markdown; did not crash.
    assert isinstance(result["markdown"], str)
    assert "page one" in result["markdown"].lower()
    # Every routed page is a Docling route (no vlm/textract routes).
    for r in result["page_routes"]:
        assert r["route"] not in ("vlm", "textract"), r


def test_escalate_true_is_default_and_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default escalate=True still escalates (byte-identical to baseline behavior)."""
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.delenv("PARSER_ESCALATION_ENGINE", raising=False)
    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("promote_to_vlm", "forced_for_test", layer=1),
    )

    def _call_vlm(image_bytes: bytes, mode: str) -> Any:
        from parser_service import vlm_client

        vlm_client._increment_vlm_call_count()
        return {"elements": [{"type": "paragraph", "text": "VLM_TEXT"}]}

    monkeypatch.setattr("parser_service.markdown_pipeline.call_vlm", _call_vlm)

    result = markdown_pipeline.parse_to_markdown(DIGITAL)  # default escalate=True
    assert result["call_counts"]["vlm"] >= 1
    assert "VLM_TEXT" in result["markdown"]
