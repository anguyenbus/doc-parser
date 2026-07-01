"""test_arbitration.py — Task Group 2 tests for reason-aware arbitration in
``_vlm_page_markdown``.

Post-escalation chooser (``PARSER_ESCALATION_ARBITRATION``, default OFF): after a
successful, non-empty engine rendering, keep Docling when the engine output is
detectably low-quality AND Docling's is clean AND the promotion reason is not a
coverage promotion.

All offline — ``call_vlm`` / ``analyze_page`` and ``_measure_text_quality`` are
mocked. No AWS. Asserted for BOTH the VLM and Textract engines.

Run ONLY these tests (task 2.5):
    uv run pytest tests/test_arbitration.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
DIGITAL = FIXTURES / "digital_simple.pdf"

# A sentinel the engine markdown carries so the mocked quality proxy and the
# assertions can distinguish engine output from the Docling fallback.
ENGINE_SENTINEL = "ENGINE_SENTINEL_TEXT"


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _patch_vlm(monkeypatch: pytest.MonkeyPatch, response: Any) -> None:
    def _mock(image_bytes: bytes, mode: str) -> Any:
        return response

    monkeypatch.setattr("parser_service.markdown_pipeline.call_vlm", _mock)


def _patch_textract(monkeypatch: pytest.MonkeyPatch, response: Any) -> None:
    def _mock(image_bytes: bytes) -> Any:
        return response

    monkeypatch.setattr("parser_service.markdown_pipeline.analyze_page", _mock)


def _patch_engine(monkeypatch: pytest.MonkeyPatch, is_textract: bool, response: Any) -> None:
    if is_textract:
        monkeypatch.setenv("PARSER_ESCALATION_ENGINE", "textract")
        _patch_textract(monkeypatch, response)
    else:
        monkeypatch.delenv("PARSER_ESCALATION_ENGINE", raising=False)
        _patch_vlm(monkeypatch, response)


def _force_promote(monkeypatch: pytest.MonkeyPatch, reason: str, layer: int) -> None:
    """Force the gate to promote every page with a fixed reason/layer."""
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("promote_to_vlm", reason, layer=layer),
    )


def _patch_quality(
    monkeypatch: pytest.MonkeyPatch, engine_passes: bool, docling_passes: bool
) -> None:
    """Deterministic ``_measure_text_quality``: keyed on the engine sentinel.

    Engine markdown (carries ENGINE_SENTINEL) is graded by ``engine_passes``;
    all other text (Docling) by ``docling_passes``.
    """
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import QualitySignals

    def _mock(text: str) -> QualitySignals:
        if ENGINE_SENTINEL in text:
            return QualitySignals(
                failing_signals=[] if engine_passes else ["garbled_ratio"]
            )
        return QualitySignals(
            failing_signals=[] if docling_passes else ["garbled_ratio"]
        )

    monkeypatch.setattr(markdown_pipeline, "_measure_text_quality", _mock)


def _engine_response() -> dict[str, Any]:
    return {"elements": [{"type": "paragraph", "text": ENGINE_SENTINEL}]}


ENGINES = [
    pytest.param(False, "vlm", "vlm-rejected-kept-docling", id="vlm"),
    pytest.param(True, "textract", "textract-rejected-kept-docling", id="textract"),
]


# ---------------------------------------------------------------------------
# Fires (Layer-1): engine fails + clean Docling + docling_low_grade reason.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("is_textract,ok_route,rejected_route", ENGINES)
def test_fires_layer1_reverts_to_docling(
    monkeypatch: pytest.MonkeyPatch,
    is_textract: bool,
    ok_route: str,
    rejected_route: str,
) -> None:
    monkeypatch.setenv("PARSER_ESCALATION_ARBITRATION", "1")
    _force_promote(monkeypatch, "docling_low_grade=POOR", layer=1)
    _patch_engine(monkeypatch, is_textract, _engine_response())
    _patch_quality(monkeypatch, engine_passes=False, docling_passes=True)

    result = markdown_pipeline_result()

    routes = result["page_routes"]
    assert routes, routes
    r = routes[0]
    assert r["route"] == rejected_route
    assert r["arbitration"] == "kept-docling"
    # Docling shipped — engine sentinel is NOT in the output.
    assert ENGINE_SENTINEL not in result["markdown"]
    assert "page one" in result["markdown"].lower()


# ---------------------------------------------------------------------------
# Fires (Layer-2 garble): engine fails + (test-forced) clean Docling + reason
# heuristic_failed. Asserts behaviour, not frequency.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("is_textract,ok_route,rejected_route", ENGINES)
def test_fires_layer2_garble_reverts_to_docling(
    monkeypatch: pytest.MonkeyPatch,
    is_textract: bool,
    ok_route: str,
    rejected_route: str,
) -> None:
    monkeypatch.setenv("PARSER_ESCALATION_ARBITRATION", "1")
    _force_promote(monkeypatch, "heuristic_failed: garbled_ratio", layer=2)
    _patch_engine(monkeypatch, is_textract, _engine_response())
    # Test forces clean Docling so condition (2) holds even though a real
    # Layer-2 garble promotion implies Docling was garbled.
    _patch_quality(monkeypatch, engine_passes=False, docling_passes=True)

    result = markdown_pipeline_result()

    r = result["page_routes"][0]
    assert r["route"] == rejected_route
    assert r["arbitration"] == "kept-docling"
    assert ENGINE_SENTINEL not in result["markdown"]


# ---------------------------------------------------------------------------
# Coverage NOT reverted: low_coverage reason keeps engine_md.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("is_textract,ok_route,rejected_route", ENGINES)
def test_coverage_promotion_not_reverted(
    monkeypatch: pytest.MonkeyPatch,
    is_textract: bool,
    ok_route: str,
    rejected_route: str,
) -> None:
    monkeypatch.setenv("PARSER_ESCALATION_ARBITRATION", "1")
    _force_promote(
        monkeypatch, "low_coverage: extracted 3 of 100 text-layer tokens", layer=2
    )
    _patch_engine(monkeypatch, is_textract, _engine_response())
    # Engine fails, Docling clean — but coverage reason forbids reverting.
    _patch_quality(monkeypatch, engine_passes=False, docling_passes=True)

    result = markdown_pipeline_result()

    r = result["page_routes"][0]
    assert r["route"] == ok_route
    assert r["arbitration"] == "kept-engine"
    # Engine markdown shipped despite failing the proxy.
    assert ENGINE_SENTINEL in result["markdown"]


# ---------------------------------------------------------------------------
# Passes proxy unchanged: engine passes -> engine kept, normal route.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("is_textract,ok_route,rejected_route", ENGINES)
def test_engine_passes_proxy_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    is_textract: bool,
    ok_route: str,
    rejected_route: str,
) -> None:
    monkeypatch.setenv("PARSER_ESCALATION_ARBITRATION", "1")
    _force_promote(monkeypatch, "docling_low_grade=POOR", layer=1)
    _patch_engine(monkeypatch, is_textract, _engine_response())
    _patch_quality(monkeypatch, engine_passes=True, docling_passes=True)

    result = markdown_pipeline_result()

    r = result["page_routes"][0]
    assert r["route"] == ok_route
    assert r["arbitration"] == "kept-engine"
    assert ENGINE_SENTINEL in result["markdown"]


# ---------------------------------------------------------------------------
# docling_fallback=None no-op: arbitration never fires; docling_quality_* absent.
# ---------------------------------------------------------------------------


def test_docling_fallback_none_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A no-Docling-content page (docling_fallback=None) never arbitrates.

    Exercised directly against ``_vlm_page_markdown`` because the whole-document
    path only passes ``docling_fallback=None`` on scan/image-only pages.
    """
    from parser_service import markdown_pipeline

    monkeypatch.setenv("PARSER_ESCALATION_ARBITRATION", "1")
    monkeypatch.delenv("PARSER_ESCALATION_ENGINE", raising=False)
    _patch_vlm(monkeypatch, _engine_response())
    # Engine fails the proxy — but there is no Docling to revert to.
    _patch_quality(monkeypatch, engine_passes=False, docling_passes=True)

    page_routes: list[dict[str, Any]] = []
    container: dict[str, Any] = {"warnings": [], "elements": []}
    monkeypatch.setattr(
        markdown_pipeline, "render_page", lambda *a, **k: b"fakeimg"
    )

    md = markdown_pipeline._vlm_page_markdown(
        Path("/nonexistent.pdf"),
        0,
        container,
        page_routes,
        docling_fallback=None,
        reason="no_docling_content",
        layer=None,
    )

    assert ENGINE_SENTINEL in md  # engine output kept — nothing to revert to
    r = page_routes[0]
    assert r["route"] == "vlm"
    assert r["arbitration"] == "kept-engine"
    # docling_quality_* absent or None when docling_fallback is None.
    assert r.get("docling_quality_passes") is None
    assert r.get("docling_quality_failing_signals") is None


# ---------------------------------------------------------------------------
# Flag-off byte-identical regression: engine fails, Docling clean, flag UNSET ->
# returns engine_md (today's behavior) + signal-only record identical to today's.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("is_textract,ok_route,rejected_route", ENGINES)
def test_flag_off_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
    is_textract: bool,
    ok_route: str,
    rejected_route: str,
) -> None:
    from parser_service import markdown_pipeline

    monkeypatch.delenv("PARSER_ESCALATION_ARBITRATION", raising=False)
    _force_promote(monkeypatch, "docling_low_grade=POOR", layer=1)
    _patch_engine(monkeypatch, is_textract, _engine_response())
    # Engine fails, Docling clean — arbitration WOULD fire if the flag were on.
    _patch_quality(monkeypatch, engine_passes=False, docling_passes=True)

    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    r = result["page_routes"][0]
    # Today's behavior: engine kept, normal route, signal-only record.
    assert r["route"] == ok_route
    assert ENGINE_SENTINEL in result["markdown"]
    # Signal-only record — today's keys plus the additive `n_chars` (from the
    # confidence feature), and NONE of the arbitration keys.
    assert set(r.keys()) == {
        "page_index",
        "route",
        "reason",
        "vlm_quality_passes",
        "vlm_quality_failing_signals",
        "n_chars",
    }
    assert "arbitration" not in r
    assert "engine_quality_passes" not in r
    assert "docling_quality_passes" not in r


# ---------------------------------------------------------------------------
# Shared invocation helper (default vlm engine unless a test set textract).
# ---------------------------------------------------------------------------


def markdown_pipeline_result() -> dict[str, Any]:
    from parser_service import markdown_pipeline

    return markdown_pipeline.parse_to_markdown(DIGITAL)
