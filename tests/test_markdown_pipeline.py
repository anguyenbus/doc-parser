"""
test_markdown_pipeline.py — Task Group 2 tests for ``parse_to_markdown``.

These are the 4-8 focused tests written first (task 2.1). They cover the
behaviors the markdown-first path must guarantee, with the VLM mocked (no
Bedrock):

  - keep page: a gate-``keep`` page uses its Docling markdown slice.
  - promote page: a gate-``promote_to_vlm`` page renders the page image, calls
    the (mocked) VLM, and the VLM markdown overwrites that page's slice.
  - VLM fallback: when the VLM returns ``{"error": ...}`` / non-list
    ``elements`` / empty ``elements`` / whitespace-only markdown, the page falls
    back to its Docling slice.
  - never-raises: a forced failure (Docling raises) returns a dict with the
    failure captured in ``warnings[]`` / ``page_routes``, not an exception.
  - page join: pages joined with exactly ``\\n\\n``, no inline markers/headers.

Run ONLY these tests (task 2.7):
    uv run pytest tests/test_markdown_pipeline.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

DIGITAL = FIXTURES / "digital_simple.pdf"
SCANNED = FIXTURES / "scanned.pdf"
MIXED = FIXTURES / "mixed.pdf"
DOCX = FIXTURES / "doc.docx"
IMAGE = FIXTURES / "screenshot.png"


# ---------------------------------------------------------------------------
# VLM mock helpers — patch call_vlm where markdown_pipeline imports it.
# ---------------------------------------------------------------------------


def _patch_vlm(monkeypatch: pytest.MonkeyPatch, response: Any) -> None:
    """Patch the VLM call seen by markdown_pipeline with a fixed response."""

    def _mock(image_bytes: bytes, mode: str) -> Any:
        return response

    monkeypatch.setattr("parser_service.markdown_pipeline.call_vlm", _mock)


def _force_keep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the gate to ``keep`` every page (deterministic docling-kept path).

    The real gate's per-page decision depends on Docling's confidence grade for
    the specific fixture (digital_simple.pdf grades FAIR and would promote), so
    the keep path is exercised by stubbing the decision — the routing/slicing
    logic under test is independent of which page the gate happens to promote.
    """
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("keep", None, None),
    )


# ---------------------------------------------------------------------------
# keep: a gate-``keep`` page uses its Docling slice; VLM never called.
# ---------------------------------------------------------------------------


def test_keep_pages_use_docling_slices(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the gate keeps every page, each page uses its Docling markdown slice,
    pages stay in order, and the VLM is never called."""
    _force_keep(monkeypatch)
    # If the VLM were called here it would be a bug; make it loudly wrong.
    _patch_vlm(monkeypatch, {"error": "VLM should not be called on a kept page"})

    from parser_service.markdown_pipeline import parse_to_markdown

    result = parse_to_markdown(DIGITAL)

    assert set(result) == {"markdown", "page_routes", "warnings"}
    assert "page one" in result["markdown"].lower()
    assert "page two" in result["markdown"].lower()
    # Page 1's text comes after page 0's (order preserved).
    assert result["markdown"].lower().index("page one") < result["markdown"].lower().index(
        "page two"
    )

    routes = result["page_routes"]
    assert len(routes) == 2
    assert all(r["route"] == "docling-kept" for r in routes), routes
    assert [r["page_index"] for r in routes] == [0, 1]


# ---------------------------------------------------------------------------
# promote: a gate-promoted page calls the VLM and its markdown overwrites the
# slice. Force promotion by stubbing evaluate_page.
# ---------------------------------------------------------------------------


def test_promoted_page_uses_vlm_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A page the gate promotes is rendered + sent to the (mocked) VLM, whose
    markdown overwrites that page's Docling slice."""
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    # Force every page to promote.
    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("promote_to_vlm", "forced_for_test", layer=1),
    )
    _patch_vlm(
        monkeypatch,
        {"elements": [{"type": "paragraph", "text": "VLM_REPLACEMENT_TEXT"}]},
    )

    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    assert "VLM_REPLACEMENT_TEXT" in result["markdown"]
    # Original Docling page text is replaced by the VLM markdown.
    assert "page one" not in result["markdown"].lower()
    routes = result["page_routes"]
    assert routes, routes
    assert all(r["route"] == "vlm" for r in routes), routes
    assert all(r["reason"] == "forced_for_test" for r in routes)


# ---------------------------------------------------------------------------
# fallback: VLM error → keep the Docling slice; route = vlm-fallback-docling.
# ---------------------------------------------------------------------------


def test_vlm_error_falls_back_to_docling(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the promoted-page VLM returns an error, the page falls back to its
    Docling slice and is routed ``vlm-fallback-docling``."""
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("promote_to_vlm", "forced_for_test", layer=1),
    )
    _patch_vlm(monkeypatch, {"error": "boom"})

    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    # Docling text survives because the VLM failed.
    assert "page one" in result["markdown"].lower()
    assert "page two" in result["markdown"].lower()
    routes = result["page_routes"]
    assert all(r["route"] == "vlm-fallback-docling" for r in routes), routes


@pytest.mark.parametrize(
    "bad_response",
    [
        {"error": "boom"},
        {"elements": "not-a-list"},
        {"elements": []},
        {"elements": [{"type": "paragraph", "text": "   "}]},  # whitespace-only md
    ],
)
def test_vlm_garbage_falls_back_to_docling(
    monkeypatch: pytest.MonkeyPatch, bad_response: Any
) -> None:
    """All four VLM-garbage shapes fall back to the page's Docling slice."""
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("promote_to_vlm", "forced", layer=2),
    )
    _patch_vlm(monkeypatch, bad_response)

    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    assert "page one" in result["markdown"].lower()
    routes = result["page_routes"]
    assert all(r["route"] == "vlm-fallback-docling" for r in routes), routes


# ---------------------------------------------------------------------------
# never-raises: Docling blowing up must NOT raise; failure lands in warnings.
# ---------------------------------------------------------------------------


def test_docling_failure_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Docling conversion failure is captured in warnings[], not raised."""
    from parser_service import markdown_pipeline

    class _Boom:
        def convert(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("docling exploded")

    monkeypatch.setattr(markdown_pipeline, "_document_converter", lambda: _Boom())
    _patch_vlm(monkeypatch, {"elements": []})

    # Must not raise.
    result = markdown_pipeline.parse_to_markdown(DIGITAL)

    assert isinstance(result["markdown"], str)
    codes = [w["code"] for w in result["warnings"]]
    assert "docling_failed" in codes, result["warnings"]
    assert "unhandled_exception" not in codes


def test_unsupported_type_never_raises(tmp_path: Path) -> None:
    """An unsupported file type returns empty markdown + a warning, no raise."""
    from parser_service.markdown_pipeline import parse_to_markdown

    p = tmp_path / "thing.xyz"
    p.write_bytes(b"not a real document")

    result = parse_to_markdown(p)

    assert result["markdown"] == ""
    codes = [w["code"] for w in result["warnings"]]
    assert "unsupported_type" in codes


# ---------------------------------------------------------------------------
# page join: pages joined with exactly \n\n, no inline markers/headers.
# ---------------------------------------------------------------------------


def test_pages_joined_with_double_newline_no_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two pages join with exactly ``\\n\\n`` and no inline page markers."""
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    # Promote both pages and feed deterministic, distinct VLM content so we can
    # assert the exact seam between them.
    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("promote_to_vlm", "forced", layer=1),
    )

    calls = {"n": 0}

    def _mock(image_bytes: bytes, mode: str) -> Any:
        calls["n"] += 1
        return {"elements": [{"type": "paragraph", "text": f"PAGE_{calls['n']}_BODY"}]}

    monkeypatch.setattr(markdown_pipeline, "call_vlm", _mock)

    result = markdown_pipeline.parse_to_markdown(DIGITAL)
    md = result["markdown"]

    # Page order preserved and joined with exactly \n\n.
    assert "PAGE_1_BODY\n\nPAGE_2_BODY" in md
    # No inline page markers / page-number headers injected.
    assert "PARSER_PAGE_BREAK" not in md
    assert "Page 1 of" not in md
    assert "--- page" not in md.lower()


# ---------------------------------------------------------------------------
# image-only / empty Docling page must not misalign the page→slice mapping.
# mixed.pdf: page 0 has digital text, page 1 is image-only (no Docling slice).
# ---------------------------------------------------------------------------


def test_image_only_page_does_not_misalign_slices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An image-only page (no Docling slice) routes to the VLM at the correct
    page index, without shifting the digital page's slice onto the wrong page.

    Page 0 is forced ``keep`` so its Docling text is deterministic; page 1 has no
    Docling content and bypasses the gate straight to the VLM."""
    from parser_service import markdown_pipeline

    _force_keep(monkeypatch)

    def _mock(image_bytes: bytes, mode: str) -> Any:
        return {"elements": [{"type": "paragraph", "text": "VLM_IMAGE_PAGE"}]}

    monkeypatch.setattr(markdown_pipeline, "call_vlm", _mock)

    result = markdown_pipeline.parse_to_markdown(MIXED)

    routes = {r["page_index"]: r["route"] for r in result["page_routes"]}
    # Page 0 (digital) keeps its own Docling slice — its text stays on page 0.
    assert routes.get(0) == "docling-kept"
    assert "digital text" in result["markdown"].lower()
    # Page 1 (image-only, no Docling content) is routed through the VLM.
    assert routes.get(1) == "vlm"
    assert "VLM_IMAGE_PAGE" in result["markdown"]
    # Both pages accounted for, in order.
    assert [r["page_index"] for r in result["page_routes"]] == [0, 1]


# ---------------------------------------------------------------------------
# DOCX whole-doc path: gate skipped, one logical page, no VLM.
# ---------------------------------------------------------------------------


def test_docx_whole_doc_one_page_no_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """DOCX takes the whole-doc export path: one logical page, gate skipped,
    VLM never called."""
    from parser_service import markdown_pipeline

    _patch_vlm(monkeypatch, {"error": "VLM must not run for DOCX"})

    result = markdown_pipeline.parse_to_markdown(DOCX)

    assert isinstance(result["markdown"], str)
    routes = result["page_routes"]
    assert len(routes) == 1
    assert routes[0]["page_index"] == 0
    assert routes[0]["route"] == "docling-kept"
