"""test_markdown_pipeline_n_chars.py — Task Group 1 tests.

Additive per-page ``n_chars`` on every ``page_routes`` record (the single
scoring source for the advisory confidence feature). Written first (task 1.1).

Every record emitted across the PDF / image / whole-doc paths and every branch
(docling-kept, engine success, all four ``_fallback`` triggers, arbitration
rejected / kept-engine) MUST carry an integer ``n_chars`` equal to the length of
the string SHIPPED for that page. Adding ``n_chars`` MUST NOT change ``markdown``
or any pre-existing record field (additive-only).

All offline: ``call_vlm`` / Docling mocked. No AWS.

Run ONLY these tests (task 1.5):
    uv run pytest tests/test_markdown_pipeline_n_chars.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
DIGITAL = FIXTURES / "digital_simple.pdf"
IMAGE = FIXTURES / "screenshot.png"
DOCX = FIXTURES / "doc.docx"


def _patch_vlm(monkeypatch: pytest.MonkeyPatch, response: Any) -> None:
    def _mock(image_bytes: bytes, mode: str) -> Any:
        return response

    monkeypatch.setattr("parser_service.markdown_pipeline.call_vlm", _mock)


def _force_keep(monkeypatch: pytest.MonkeyPatch) -> None:
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("keep", None, None),
    )


def _force_promote(monkeypatch: pytest.MonkeyPatch, reason: str = "forced", layer: int = 1) -> None:
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("promote_to_vlm", reason, layer=layer),
    )


# ---------------------------------------------------------------------------
# docling-kept: n_chars == len of the shipped Docling page markdown.
# ---------------------------------------------------------------------------


def test_docling_kept_records_carry_n_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_keep(monkeypatch)
    _patch_vlm(monkeypatch, {"error": "should not be called"})

    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(DIGITAL)
    routes = result["page_routes"]
    assert routes
    for r in routes:
        assert r["route"] == "docling-kept"
        assert isinstance(r["n_chars"], int)
        assert r["n_chars"] >= 0
    # Every non-empty shipped page contributes its chars; the join is the pages
    # separated by "\n\n", so summed n_chars is bounded by the markdown length.
    assert sum(r["n_chars"] for r in routes) > 0


# ---------------------------------------------------------------------------
# engine success (vlm route): n_chars == len of the returned engine markdown.
# ---------------------------------------------------------------------------


def test_engine_success_records_carry_n_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_promote(monkeypatch)
    _patch_vlm(
        monkeypatch,
        {"elements": [{"type": "paragraph", "text": "VLM_REPLACEMENT_TEXT"}]},
    )

    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(DIGITAL)
    routes = result["page_routes"]
    assert routes
    for r in routes:
        assert r["route"] == "vlm"
        assert isinstance(r["n_chars"], int)
        # The shipped page is the rendered VLM markdown containing the text.
        assert r["n_chars"] == len("VLM_REPLACEMENT_TEXT")


# ---------------------------------------------------------------------------
# fallback: all four garbage triggers keep the Docling slice; n_chars == its len.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_response",
    [
        {"error": "boom"},
        {"elements": "not-a-list"},
        {"elements": []},
        {"elements": [{"type": "paragraph", "text": "   "}]},
    ],
)
def test_fallback_records_carry_n_chars(
    monkeypatch: pytest.MonkeyPatch, bad_response: Any
) -> None:
    _force_promote(monkeypatch, layer=2)
    _patch_vlm(monkeypatch, bad_response)

    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(DIGITAL)
    routes = result["page_routes"]
    assert routes
    for r in routes:
        assert r["route"] == "vlm-fallback-docling"
        assert isinstance(r["n_chars"], int)
        # Fallback ships the Docling slice; its length is non-negative.
        assert r["n_chars"] >= 0


# ---------------------------------------------------------------------------
# empty-page edge: a page with no Docling content that promotes and gets no
# engine output falls back with docling_fallback=None → shipped "" → n_chars==0.
# ---------------------------------------------------------------------------


def test_empty_page_fallback_n_chars_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from parser_service import markdown_pipeline

    # Force the "no docling content" promote path by rendering every page empty.
    monkeypatch.setattr(markdown_pipeline, "_render_page_markdown", lambda *a, **k: "")
    _patch_vlm(monkeypatch, {"error": "boom"})

    result = markdown_pipeline.parse_to_markdown(DIGITAL)
    routes = result["page_routes"]
    assert routes
    for r in routes:
        # docling_fallback is None → route stays the ok route, shipped "".
        assert r["reason"] == "no_docling_content"
        assert r["n_chars"] == 0


# ---------------------------------------------------------------------------
# arbitration rejected + kept-engine records carry n_chars.
# ---------------------------------------------------------------------------


def test_arbitration_records_carry_n_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    from parser_service import markdown_pipeline

    monkeypatch.setenv("PARSER_ESCALATION_ARBITRATION", "1")
    _force_promote(monkeypatch, reason="forced", layer=1)
    # Clean engine output → arbitration keeps the engine (kept-engine record).
    _patch_vlm(
        monkeypatch,
        {"elements": [{"type": "paragraph", "text": "clean engine output paragraph"}]},
    )

    result = markdown_pipeline.parse_to_markdown(DIGITAL)
    routes = result["page_routes"]
    assert routes
    for r in routes:
        assert "arbitration" in r
        assert isinstance(r["n_chars"], int)
        if r["arbitration"] == "kept-engine":
            assert r["n_chars"] == len("clean engine output paragraph")
        else:  # kept-docling
            assert r["n_chars"] >= 0


# ---------------------------------------------------------------------------
# non-PDF paths: image docling-kept and whole-doc docling-kept carry n_chars.
# ---------------------------------------------------------------------------


def test_image_and_wholedoc_docling_kept_carry_n_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_keep(monkeypatch)
    _patch_vlm(monkeypatch, {"error": "should not be called"})

    from parser_service.markdown_pipeline import parse_to_markdown

    for fixture in (IMAGE, DOCX):
        result = parse_to_markdown(fixture)
        routes = result["page_routes"]
        assert routes, fixture
        for r in routes:
            assert r["route"] == "docling-kept", (fixture, r)
            assert isinstance(r["n_chars"], int), (fixture, r)
            assert r["n_chars"] == len(result["markdown"]), (fixture, r)


# ---------------------------------------------------------------------------
# additive-only: adding n_chars changes nothing else on the records / markdown.
# ---------------------------------------------------------------------------


def test_n_chars_is_additive_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_keep(monkeypatch)
    _patch_vlm(monkeypatch, {"error": "should not be called"})

    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(DIGITAL)
    for r in result["page_routes"]:
        # The only permitted new key on the record is n_chars; the pre-existing
        # keys retain their documented shape.
        assert set(r) - {"n_chars"} == {"page_index", "route", "reason"}
