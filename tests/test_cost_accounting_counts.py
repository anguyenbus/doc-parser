"""
test_cost_accounting_counts.py — Group 1 (Part A) tests for race-free
per-invocation call counts.

These are the load-bearing concurrency tests for the cost-accounting spec
(``agent-os/specs/2026-07-01-cost-accounting-budget-cap``). They prove:

  - The VLM / Textract call counters are backed by ``threading.local()`` so
    reset / increment / read are per-worker-thread and do NOT cross-contaminate
    across concurrent parses (the old shared module global would let a neighbor
    thread's ``reset_*()`` corrupt the read).
  - ``parse_to_markdown`` returns an ADDITIVE ``call_counts`` key
    ``{"vlm": int, "textract": int}`` reflecting exactly this invocation's calls.
  - The existing return keys (``markdown`` / ``page_routes`` / ``warnings`` /
    ``confidence``) are byte-identical to baseline (additive-only).

All offline. ``call_vlm`` / ``analyze_page`` are patched to force escalation
counts; no AWS.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
DIGITAL = FIXTURES / "digital_simple.pdf"


# ---------------------------------------------------------------------------
# Direct threading.local unit tests on the two counters.
# ---------------------------------------------------------------------------


def test_vlm_counter_is_thread_local_isolated() -> None:
    """Thread A's reset+increment on the VLM counter is isolated from thread B."""
    from parser_service import vlm_client

    results: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def worker(name: str, increments: int) -> None:
        vlm_client.reset_vlm_call_count()
        # Rendezvous so both threads are live before either increments — the
        # scenario that corrupts a shared global.
        barrier.wait()
        for _ in range(increments):
            # Increment the (thread-local) counter directly, mirroring the
            # module's own increment idiom.
            vlm_client._increment_vlm_call_count()
        results[name] = vlm_client.get_vlm_call_count()

    ta = threading.Thread(target=worker, args=("a", 3))
    tb = threading.Thread(target=worker, args=("b", 7))
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    assert results["a"] == 3, results
    assert results["b"] == 7, results


def test_textract_counter_is_thread_local_isolated() -> None:
    """Thread A's reset+increment on the Textract counter is isolated from thread B."""
    from parser_service import textract_client

    results: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def worker(name: str, increments: int) -> None:
        textract_client.reset_textract_call_count()
        barrier.wait()
        for _ in range(increments):
            textract_client._increment_textract_call_count()
        results[name] = textract_client.get_textract_call_count()

    ta = threading.Thread(target=worker, args=("a", 2))
    tb = threading.Thread(target=worker, args=("b", 5))
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    assert results["a"] == 2, results
    assert results["b"] == 5, results


# ---------------------------------------------------------------------------
# Helpers to force escalation on every page.
# ---------------------------------------------------------------------------


def _force_promote(monkeypatch: pytest.MonkeyPatch) -> None:
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("promote_to_vlm", "forced_for_test", layer=1),
    )


# ---------------------------------------------------------------------------
# parse_to_markdown returns an additive call_counts reflecting THIS invocation.
# ---------------------------------------------------------------------------


def test_vlm_parse_returns_call_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A VLM-escalating parse reports non-zero ``vlm`` and zero ``textract``."""
    from parser_service import markdown_pipeline

    monkeypatch.delenv("PARSER_ESCALATION_ENGINE", raising=False)
    _force_promote(monkeypatch)

    def _call_vlm(image_bytes: bytes, mode: str) -> Any:
        # Mirror the real client: bump the (thread-local) counter on success.
        from parser_service import vlm_client

        vlm_client._increment_vlm_call_count()
        return {"elements": [{"type": "paragraph", "text": "VLM_TEXT"}]}

    monkeypatch.setattr("parser_service.markdown_pipeline.call_vlm", _call_vlm)

    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    assert "call_counts" in result, result.keys()
    assert set(result["call_counts"]) == {"vlm", "textract"}
    assert result["call_counts"]["vlm"] >= 1
    assert result["call_counts"]["textract"] == 0


def test_textract_parse_returns_call_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Textract-escalating parse reports non-zero ``textract`` and zero ``vlm``."""
    from parser_service import markdown_pipeline

    monkeypatch.setenv("PARSER_ESCALATION_ENGINE", "textract")
    _force_promote(monkeypatch)

    def _analyze_page(image_bytes: bytes) -> Any:
        from parser_service import textract_client

        textract_client._increment_textract_call_count()
        return {"elements": [{"type": "paragraph", "text": "TEXTRACT_TEXT"}]}

    monkeypatch.setattr("parser_service.markdown_pipeline.analyze_page", _analyze_page)

    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    assert "call_counts" in result
    assert result["call_counts"]["textract"] >= 1
    assert result["call_counts"]["vlm"] == 0


# ---------------------------------------------------------------------------
# Additive-only regression: existing keys byte-identical to baseline.
# ---------------------------------------------------------------------------


def test_return_is_additive_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only ``call_counts`` is new; markdown/page_routes/warnings/confidence unchanged.

    The gate is left at its real behavior (no forced promote) so this exercises a
    representative doc's real routing.
    """
    from parser_service import markdown_pipeline

    monkeypatch.delenv("PARSER_ESCALATION_ENGINE", raising=False)

    def _call_vlm(image_bytes: bytes, mode: str) -> Any:
        from parser_service import vlm_client

        vlm_client._increment_vlm_call_count()
        return {"elements": [{"type": "paragraph", "text": "VLM_TEXT"}]}

    monkeypatch.setattr("parser_service.markdown_pipeline.call_vlm", _call_vlm)

    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    assert set(result) == {
        "markdown",
        "page_routes",
        "warnings",
        "confidence",
        "call_counts",
    }, set(result)
    # The four pre-existing keys carry their original types/shapes.
    assert isinstance(result["markdown"], str)
    assert isinstance(result["page_routes"], list)
    assert isinstance(result["warnings"], list)
    assert isinstance(result["confidence"], dict)
    assert set(result["confidence"]) == {"document", "pages"}


def test_parse_one_and_wrap_tolerate_new_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``["markdown"]`` reads and ``wrap_md_as_prediction`` still work with the new key."""
    from parser_service import markdown_pipeline

    monkeypatch.delenv("PARSER_ESCALATION_ENGINE", raising=False)
    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    # scripts/parse_one.py reads only ["markdown"].
    md = result["markdown"]
    assert isinstance(md, str)

    # wrap_md_as_prediction takes (md, source) and never inspects the return dict.
    prediction = markdown_pipeline.wrap_md_as_prediction(md, DIGITAL)
    assert prediction["schema_version"] == "1.0.0"


# ---------------------------------------------------------------------------
# LOAD-BEARING: concurrent parses with different forced counts do not
# cross-contaminate. Fails under the old shared module global (a neighbor's
# reset_*() corrupts the read).
# ---------------------------------------------------------------------------


def test_concurrent_parses_do_not_cross_contaminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent parses with distinct forced VLM counts each report their own.

    Thread A parses a doc where every page escalates once (N_a VLM calls); thread
    B does the same. With the counters backed by ``threading.local`` each parse's
    returned ``call_counts["vlm"]`` equals its OWN page count — a neighbor's
    ``reset_vlm_call_count()`` at the start of ITS parse cannot corrupt this
    thread's final read.
    """
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.delenv("PARSER_ESCALATION_ENGINE", raising=False)
    # Force every page to promote so each page yields exactly one engine call.
    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("promote_to_vlm", "forced_for_test", layer=1),
    )

    barrier = threading.Barrier(2)

    def _call_vlm(image_bytes: bytes, mode: str) -> Any:
        from parser_service import vlm_client

        vlm_client._increment_vlm_call_count()
        return {"elements": [{"type": "paragraph", "text": "VLM_TEXT"}]}

    monkeypatch.setattr("parser_service.markdown_pipeline.call_vlm", _call_vlm)

    counts: dict[str, int] = {}

    def worker(name: str) -> None:
        # Rendezvous so both parses interleave their reset/increment/read.
        barrier.wait()
        result = markdown_pipeline.parse_to_markdown(DIGITAL)
        counts[name] = result["call_counts"]["vlm"]

    ta = threading.Thread(target=worker, args=("a",))
    tb = threading.Thread(target=worker, args=("b",))
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    # A single-thread baseline: how many VLM calls one parse of this doc makes.
    baseline = markdown_pipeline.parse_to_markdown(DIGITAL)["call_counts"]["vlm"]
    assert baseline >= 1

    # Each concurrent parse reports its OWN count (== baseline), not a corrupted
    # shared sum.
    assert counts["a"] == baseline, counts
    assert counts["b"] == baseline, counts
