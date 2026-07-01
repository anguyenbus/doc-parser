"""
test_scan_fastpath_e2e_parity.py — Task Group 4 E2E parity + latency.

On an all-scanned doc (``scanned.pdf``) Docling produces nothing on every page, so
BOTH the fast-path (ON) and the normal path (OFF) escalate every page to the same
engine and ship the same VLM markdown — only ``reason`` differs
(``scan_fastpath`` vs ``no_docling_content``). This test proves the escalation
OUTPUT is byte-identical whether or not the fast-path is enabled, and records the
cold (OFF) vs warm (ON) wall-clock into this spec's ``planning/`` directory.

Offline: the engine is mocked at the seam and Docling is a real converter for the
OFF path (so we measure the real skipped cost). Run:
    uv run pytest tests/test_scan_fastpath_e2e_parity.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SCANNED = FIXTURES / "scanned.pdf"
PLANNING = (
    Path(__file__).resolve().parents[1]
    / "agent-os"
    / "specs"
    / "2026-06-30-scan-fastpath-skip-docling"
    / "planning"
)


def _mock_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic per-page VLM markdown so ON/OFF produce identical output."""
    from parser_service import markdown_pipeline
    from parser_service.vlm_client import _increment_vlm_call_count

    calls = {"n": 0}

    def _mock(image_bytes: bytes, mode: str) -> Any:
        calls["n"] += 1
        _increment_vlm_call_count()
        return {"elements": [{"type": "paragraph", "text": f"SCAN_PAGE_{calls['n']}"}]}

    monkeypatch.setattr(markdown_pipeline, "call_vlm", _mock)


def test_fastpath_on_off_identical_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fast-path ON vs OFF on scanned.pdf → identical escalation markdown; only
    the per-page ``reason`` differs. Also records cold/warm wall-clock."""
    from parser_service.markdown_pipeline import parse_to_markdown

    # OFF (cold): Docling converts (and finds nothing) → every page escalates via
    # the normal no_docling_content path.
    monkeypatch.delenv("PARSER_SCAN_FASTPATH", raising=False)
    _mock_engine(monkeypatch)
    t0 = time.perf_counter()
    off = parse_to_markdown(SCANNED)
    off_secs = time.perf_counter() - t0

    # ON (warm): classify → skip convert → escalate every page.
    monkeypatch.setenv("PARSER_SCAN_FASTPATH", "1")
    _mock_engine(monkeypatch)
    t0 = time.perf_counter()
    on = parse_to_markdown(SCANNED)
    on_secs = time.perf_counter() - t0

    # PARITY: escalation output is byte-identical.
    assert on["markdown"] == off["markdown"], "fast-path changed the escalation output"
    assert [r["page_index"] for r in on["page_routes"]] == [
        r["page_index"] for r in off["page_routes"]
    ]
    assert [r["route"] for r in on["page_routes"]] == [
        r["route"] for r in off["page_routes"]
    ]
    # Only the reason differs (scan_fastpath vs no_docling_content).
    assert all(r["reason"] == "scan_fastpath" for r in on["page_routes"])
    assert all(r["reason"] == "no_docling_content" for r in off["page_routes"])

    # Record the latency delta for the spec's planning notes.
    PLANNING.mkdir(parents=True, exist_ok=True)
    (PLANNING / "latency.json").write_text(
        json.dumps(
            {
                "fixture": SCANNED.name,
                "off_convert_seconds": round(off_secs, 3),
                "on_fastpath_seconds": round(on_secs, 3),
                "delta_seconds": round(off_secs - on_secs, 3),
                "note": (
                    "OFF runs real Docling convert (finds nothing) then escalates; "
                    "ON skips convert and escalates directly. Engine mocked, so the "
                    "delta is the skipped Docling convert cost on an all-scanned doc."
                ),
            },
            indent=2,
        )
    )
    # The fast-path must not be slower than the convert path on an all-scanned doc.
    assert on_secs <= off_secs
