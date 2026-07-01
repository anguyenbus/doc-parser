"""
test_scan_fastpath_pipeline.py — Task Group 2 tests for the fast-path branch.

The PDF path calls ``classify_for_fastpath`` BEFORE ``DocumentConverter.convert``.
When the doc is all-scanned (every page zero-text-with-images), convert is SKIPPED
and every page is routed straight to the escalation seam with
``docling_fallback=None, reason="scan_fastpath"``. A mixed doc (any text-bearing
page) falls through to the unchanged convert + per-page gate.

The fast-path is OPT-IN via ``PARSER_SCAN_FASTPATH`` (default OFF). With the flag
OFF the pipeline is byte-identical to today — see the parity test below.

Offline: spy on ``DocumentConverter.convert`` (via a converter spy monkeypatched
onto ``markdown_pipeline._document_converter``) and mock ``call_vlm`` at the seam.

Run ONLY these tests (task 2.3):
    uv run pytest tests/test_scan_fastpath_pipeline.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SCANNED = FIXTURES / "scanned.pdf"
MIXED = FIXTURES / "mixed.pdf"


class _ConvertSpy:
    """A converter stand-in that records whether ``convert`` was called and
    delegates to a real Docling converter when it is (mixed-doc path)."""

    def __init__(self) -> None:
        self.calls = 0
        self._real: Any = None

    def convert(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self._real is None:
            from docling.document_converter import DocumentConverter

            self._real = DocumentConverter()
        return self._real.convert(*args, **kwargs)


def _install_spy(monkeypatch: pytest.MonkeyPatch) -> _ConvertSpy:
    """Monkeypatch ``_document_converter`` to return a single shared spy."""
    from parser_service import markdown_pipeline

    spy = _ConvertSpy()
    monkeypatch.setattr(markdown_pipeline, "_document_converter", lambda: spy)
    return spy


def _mock_vlm(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Mock the escalation seam so each page returns distinct VLM markdown."""
    from parser_service import markdown_pipeline

    from parser_service.vlm_client import _increment_vlm_call_count

    calls = {"n": 0}

    def _mock(image_bytes: bytes, mode: str) -> Any:
        calls["n"] += 1
        # Mirror the real ``call_vlm``, which bumps the per-thread counter on a
        # successful call, so ``call_counts.vlm`` is meaningful under the mock.
        _increment_vlm_call_count()
        return {"elements": [{"type": "paragraph", "text": f"SCAN_PAGE_{calls['n']}"}]}

    monkeypatch.setattr(markdown_pipeline, "call_vlm", _mock)
    return calls


# ---------------------------------------------------------------------------
# all-scanned doc with flag ON → convert SKIPPED, every page escalated.
# ---------------------------------------------------------------------------


def test_all_scanned_skips_convert(monkeypatch: pytest.MonkeyPatch) -> None:
    """scanned.pdf with the fast-path ON must NOT call ``convert``; every page
    routes through the escalation seam with ``reason="scan_fastpath"``."""
    monkeypatch.setenv("PARSER_SCAN_FASTPATH", "1")
    spy = _install_spy(monkeypatch)
    _mock_vlm(monkeypatch)

    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(SCANNED)

    assert spy.calls == 0, "convert must be skipped on an all-scanned doc"

    routes = result["page_routes"]
    assert [r["page_index"] for r in routes] == [0, 1]
    assert all(r["route"] == "vlm" for r in routes), routes
    assert all(r["reason"] == "scan_fastpath" for r in routes), routes
    assert "SCAN_PAGE_1" in result["markdown"]
    assert "SCAN_PAGE_2" in result["markdown"]


def test_fastpath_routes_carry_additive_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fast-pathed page_routes must carry the same additive keys the normal
    escalation path produces (``n_chars`` present; the confidence block still
    computes over them) and ``call_counts`` must reflect the escalation calls."""
    monkeypatch.setenv("PARSER_SCAN_FASTPATH", "1")
    _install_spy(monkeypatch)
    _mock_vlm(monkeypatch)

    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(SCANNED)

    for r in result["page_routes"]:
        assert "n_chars" in r and r["n_chars"] > 0, r
    # Two successful VLM pages → call_counts.vlm == 2.
    assert result["call_counts"]["vlm"] == 2
    # Confidence block still computes one entry per page.
    assert [c["page_index"] for c in result["confidence"]["pages"]] == [0, 1]


def test_fastpath_no_docling_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a fast-pathed page the engine has NO Docling fallback: a VLM error
    yields an empty page and the ``vlm`` route (not ``vlm-fallback-docling``,
    which requires a fallback string)."""
    monkeypatch.setenv("PARSER_SCAN_FASTPATH", "1")
    _install_spy(monkeypatch)
    from parser_service import markdown_pipeline

    monkeypatch.setattr(
        markdown_pipeline, "call_vlm", lambda *a, **k: {"error": "boom"}
    )

    result = markdown_pipeline.parse_to_markdown(SCANNED)

    routes = result["page_routes"]
    # docling_fallback is None → the fallback route collapses to the ok route.
    assert all(r["route"] == "vlm" for r in routes), routes
    assert all(r["reason"] == "scan_fastpath" for r in routes), routes
    assert all(r["n_chars"] == 0 for r in routes), routes


# ---------------------------------------------------------------------------
# mixed doc with flag ON → convert RUNS, normal gate, no scan_fastpath reason.
# ---------------------------------------------------------------------------


def test_mixed_doc_runs_convert(monkeypatch: pytest.MonkeyPatch) -> None:
    """mixed.pdf (page 0 has text) is NOT all-scanned, so even with the flag ON
    convert RUNS once and the normal per-page gate applies. No ``scan_fastpath``
    reason appears."""
    monkeypatch.setenv("PARSER_SCAN_FASTPATH", "1")
    spy = _install_spy(monkeypatch)
    _mock_vlm(monkeypatch)

    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(MIXED)

    assert spy.calls == 1, "mixed docs must still convert the whole document"
    reasons = {r.get("reason") for r in result["page_routes"]}
    assert "scan_fastpath" not in reasons, result["page_routes"]
    assert [r["page_index"] for r in result["page_routes"]] == [0, 1]


# ---------------------------------------------------------------------------
# flag OFF → byte-identical to today (regression). scanned.pdf must convert.
# ---------------------------------------------------------------------------


def test_flag_off_still_converts_scanned(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``PARSER_SCAN_FASTPATH`` OFF (default), even an all-scanned doc takes
    the normal path: convert RUNS and no ``scan_fastpath`` reason appears."""
    monkeypatch.delenv("PARSER_SCAN_FASTPATH", raising=False)
    spy = _install_spy(monkeypatch)
    _mock_vlm(monkeypatch)

    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(SCANNED)

    assert spy.calls == 1, "flag OFF must run convert (byte-identical to today)"
    reasons = {r.get("reason") for r in result["page_routes"]}
    assert "scan_fastpath" not in reasons, result["page_routes"]


def test_flag_off_byte_identical_page_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag-OFF page_routes for scanned.pdf are exactly what today's pipeline
    produces: two zero-text pages escalate via the normal ``no_docling_content``
    reason (NOT scan_fastpath)."""
    monkeypatch.delenv("PARSER_SCAN_FASTPATH", raising=False)
    _install_spy(monkeypatch)
    _mock_vlm(monkeypatch)

    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(SCANNED)

    routes = result["page_routes"]
    assert [r["page_index"] for r in routes] == [0, 1]
    assert all(r["reason"] == "no_docling_content" for r in routes), routes
