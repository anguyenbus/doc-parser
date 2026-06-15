"""Tests for ``route_stats`` recognising the Textract escalation routes.

Regression for the route-count invariant: before the fix it hardcoded only the
``vlm`` vocabulary, so a ``textract``-routed page tripped the assertion and the
document was dropped from grading.
"""

from __future__ import annotations

from parser_service.route_stats import route_record, summarize


def _routes(*routes: str) -> list[dict[str, object]]:
    return [{"page_index": i, "route": r, "reason": None} for i, r in enumerate(routes)]


def test_textract_route_passes_invariant_and_labels_textract() -> None:
    rec = route_record(_routes("docling-kept", "textract"), doc_id="doc1")
    assert rec["route"] == "textract"
    assert rec["pages"] == 2
    assert rec["vlm_pages"] == 1  # escalated pages (one Textract page)


def test_textract_fallback_route_labels_textract_failed() -> None:
    rec = route_record(_routes("textract-fallback-docling"), doc_id="doc2")
    assert rec["route"] == "textract-failed"
    assert rec["vlm_pages"] == 1


def test_all_textract_pages_do_not_raise() -> None:
    # Was: AssertionError "route vocabulary drifted (saw routes: ['textract'])".
    rec = route_record(_routes("textract", "textract"), doc_id="doc3")
    assert rec["route"] == "textract"
    assert rec["vlm_pages"] == 2


def test_summarize_counts_textract() -> None:
    records = [
        route_record(_routes("docling-kept"), doc_id="a"),
        route_record(_routes("textract"), doc_id="b"),
        route_record(_routes("textract-fallback-docling"), doc_id="c"),
        route_record(_routes("vlm"), doc_id="d"),
    ]
    s = summarize(records)
    assert s["used_textract"] == 1
    assert s["textract_failed"] == 1
    assert s["used_vlm"] == 1
    assert s["docling_kept"] == 1
