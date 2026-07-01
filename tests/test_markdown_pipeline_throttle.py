"""
test_markdown_pipeline_throttle.py — Task Group 3 tests: the escalation seam
(``_vlm_page_markdown``) surfaces an exhausted-throttle as a distinct
``reason="throttled"`` in ``page_routes``, for BOTH engines, while leaving every
non-throttle path on the gate reason (byte-for-byte).

Fully offline: ``call_vlm`` / ``analyze_page`` are patched where the pipeline
imports them (NO AWS).

Run ONLY these tests (task 3.3):
    uv run pytest tests/test_markdown_pipeline_throttle.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
DIGITAL = FIXTURES / "digital_simple.pdf"


def _force_promote(monkeypatch: pytest.MonkeyPatch) -> None:
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("promote_to_vlm", "forced_for_test", layer=1),
    )


def _patch_textract(monkeypatch: pytest.MonkeyPatch, response: Any) -> None:
    monkeypatch.setattr(
        "parser_service.markdown_pipeline.analyze_page", lambda image_bytes: response
    )


def _patch_vlm(monkeypatch: pytest.MonkeyPatch, response: Any) -> None:
    monkeypatch.setattr(
        "parser_service.markdown_pipeline.call_vlm",
        lambda image_bytes, mode: response,
    )


# ---------------------------------------------------------------------------
# Throttled engine result -> reason="throttled" on the *-fallback-docling route.
# ---------------------------------------------------------------------------


def test_vlm_throttle_records_throttled_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from parser_service import markdown_pipeline

    monkeypatch.delenv("PARSER_ESCALATION_ENGINE", raising=False)
    _force_promote(monkeypatch)
    _patch_vlm(monkeypatch, {"error": "ThrottlingException: slow down", "error_kind": "throttled"})

    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    routes = result["page_routes"]
    assert routes, routes
    assert all(r["route"] == "vlm-fallback-docling" for r in routes), routes
    assert all(r["reason"] == "throttled" for r in routes), routes


def test_textract_throttle_records_throttled_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from parser_service import markdown_pipeline

    monkeypatch.setenv("PARSER_ESCALATION_ENGINE", "textract")
    _force_promote(monkeypatch)
    _patch_textract(
        monkeypatch,
        {"error": "ThrottlingException: slow down", "error_kind": "throttled"},
    )

    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    routes = result["page_routes"]
    assert routes, routes
    assert all(r["route"] == "textract-fallback-docling" for r in routes), routes
    assert all(r["reason"] == "throttled" for r in routes), routes


# ---------------------------------------------------------------------------
# Non-throttle plain error -> gate reason preserved (current behavior).
# ---------------------------------------------------------------------------


def test_vlm_plain_error_keeps_gate_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from parser_service import markdown_pipeline

    monkeypatch.delenv("PARSER_ESCALATION_ENGINE", raising=False)
    _force_promote(monkeypatch)
    # Plain error (no error_kind) — a permanent/validation failure or garbage.
    _patch_vlm(monkeypatch, {"error": "AccessDeniedException"})

    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    routes = result["page_routes"]
    assert routes, routes
    assert all(r["route"] == "vlm-fallback-docling" for r in routes), routes
    assert all(r["reason"] == "forced_for_test" for r in routes), routes


def test_textract_plain_error_keeps_gate_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from parser_service import markdown_pipeline

    monkeypatch.setenv("PARSER_ESCALATION_ENGINE", "textract")
    _force_promote(monkeypatch)
    _patch_textract(monkeypatch, {"error": "ValidationException"})

    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    routes = result["page_routes"]
    assert routes, routes
    assert all(r["route"] == "textract-fallback-docling" for r in routes), routes
    assert all(r["reason"] == "forced_for_test" for r in routes), routes
