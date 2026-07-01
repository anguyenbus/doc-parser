"""
test_scan_fastpath_classify.py — Task Group 1 tests for the all-scanned classifier.

``classify_for_fastpath(path)`` is a PURE probe (no Docling/gate/convert calls):
it returns ``{"skip_docling": bool, "page_qualifies": dict[int, bool]}``. A page
qualifies iff ``text_layer_tokens[page] == 0 AND has_images(page)``; the document
skips Docling iff EVERY page qualifies.

Fixtures (reused, no new fixture):
  - ``scanned.pdf`` — both pages ``tokens == 0`` AND have images → all-scanned
    positive: ``skip_docling == True``, ``page_qualifies == {0: True, 1: True}``.
  - ``mixed.pdf`` — page 0 has a text layer (``tokens > 0``) → does NOT qualify,
    so ``skip_docling == False`` even though page 1 is an image-only scan.

Run ONLY these tests (task 1.3):
    uv run pytest tests/test_scan_fastpath_classify.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from parser_service.render import classify_for_fastpath, page_has_images

FIXTURES = Path(__file__).parent / "fixtures"
SCANNED = FIXTURES / "scanned.pdf"
MIXED = FIXTURES / "mixed.pdf"


# ---------------------------------------------------------------------------
# has-images probe (pypdf resource-level XObject walk, no decode).
# ---------------------------------------------------------------------------


def test_page_has_images_finds_resource_declared_images() -> None:
    """The pypdf probe finds ``/XObject /Image`` entries on both scanned pages
    (pypdfium2's content-walk misses these; verified in the spec)."""
    from pypdf import PdfReader

    reader = PdfReader(str(SCANNED))
    assert [page_has_images(pg) for pg in reader.pages] == [True, True]


def test_page_has_images_false_on_text_only_page() -> None:
    """mixed.pdf page 0 is text-only (no image XObject) → probe returns False;
    page 1 is an image-only scan → probe returns True."""
    from pypdf import PdfReader

    reader = PdfReader(str(MIXED))
    assert [page_has_images(pg) for pg in reader.pages] == [False, True]


# ---------------------------------------------------------------------------
# per-page qualify rule: tokens == 0 AND has_images.
# ---------------------------------------------------------------------------


def test_all_scanned_doc_qualifies_every_page() -> None:
    """scanned.pdf: both pages ``tokens == 0`` AND have images → every page
    qualifies and the whole doc skips Docling."""
    result = classify_for_fastpath(SCANNED)

    assert result["page_qualifies"] == {0: True, 1: True}
    assert result["skip_docling"] is True


def test_text_bearing_page_disqualifies_the_doc() -> None:
    """mixed.pdf: page 0 has a text layer (``tokens > 0``) so it does NOT qualify;
    a single text-bearing page disables the whole-doc fast-path even though page 1
    is an image-only scan that does qualify."""
    result = classify_for_fastpath(MIXED)

    assert result["page_qualifies"] == {0: False, 1: True}
    assert result["skip_docling"] is False


def test_empty_page_no_images_does_not_qualify(monkeypatch: pytest.MonkeyPatch) -> None:
    """A page with ``tokens == 0`` and NO images (a truly empty page) must NOT
    qualify — there is nothing for escalation to recover, so it stays on the
    normal Docling path.

    Simulated by forcing the has-images probe False for scanned.pdf: with images
    gone, its zero-token pages no longer qualify and the doc is not fast-pathed."""
    from parser_service import render

    monkeypatch.setattr(render, "page_has_images", lambda _page: False)

    result = classify_for_fastpath(SCANNED)

    assert result["page_qualifies"] == {0: False, 1: False}
    assert result["skip_docling"] is False


def test_classify_is_pure_no_docling_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """The classifier must not touch Docling — it uses only ``text_layer_tokens``
    and the pypdf has-images probe. Poison ``_document_converter`` so any Docling
    call would blow up; the classifier must still succeed."""
    from parser_service import markdown_pipeline

    def _boom() -> object:
        raise AssertionError("classify_for_fastpath must not construct Docling")

    monkeypatch.setattr(markdown_pipeline, "_document_converter", _boom)

    result = classify_for_fastpath(SCANNED)
    assert result["skip_docling"] is True
