"""test_route_stats_arbitration.py — Task Group 3 tests for ``route_stats``
counting the two rejected-kept-docling routes.

A rejected-kept-docling page is a DOCLING-output page: it must satisfy the
sum invariant with the two new routes present, but it is excluded from the
``vlm``/``textract`` document roll-up and from ``vlm_pages`` (the engine output
was rejected; Docling shipped).

Offline (no network).

Run ONLY these tests (task 3.4):
    uv run pytest tests/test_route_stats_arbitration.py tests/test_route_stats_textract.py
"""

from __future__ import annotations

from parser_service.route_stats import (
    ROUTE_TEXTRACT_REJECTED,
    ROUTE_VLM_REJECTED,
    route_record,
)


def _routes(*routes: str) -> list[dict[str, object]]:
    return [{"page_index": i, "route": r, "reason": None} for i, r in enumerate(routes)]


def test_constants_have_expected_values() -> None:
    assert ROUTE_VLM_REJECTED == "vlm-rejected-kept-docling"
    assert ROUTE_TEXTRACT_REJECTED == "textract-rejected-kept-docling"


def test_mixed_list_with_rejected_routes_passes_sum_invariant() -> None:
    """A mixed page_routes list containing both rejected routes satisfies the
    per-page sum invariant in route_record (no AssertionError)."""
    rec = route_record(
        _routes(
            "docling-kept",
            "vlm",
            "vlm-rejected-kept-docling",
            "textract",
            "textract-rejected-kept-docling",
        ),
        doc_id="mixed",
    )
    assert rec["pages"] == 5


def test_rejected_page_excluded_from_vlm_pages_and_rollup_vlm() -> None:
    """A vlm-rejected-kept-docling page counts as Docling output: not in
    vlm_pages, and (alone with docling-kept) does not label the doc 'vlm'."""
    rec = route_record(
        _routes("docling-kept", "vlm-rejected-kept-docling"), doc_id="d1"
    )
    assert rec["pages"] == 2
    assert rec["vlm_pages"] == 0  # engine output rejected -> not an escalation success
    assert rec["route"] == "docling-kept"


def test_rejected_page_excluded_from_vlm_pages_and_rollup_textract() -> None:
    """Same for the textract rejected route."""
    rec = route_record(
        _routes("docling-kept", "textract-rejected-kept-docling"), doc_id="d2"
    )
    assert rec["pages"] == 2
    assert rec["vlm_pages"] == 0
    assert rec["route"] == "docling-kept"


def test_rejected_route_does_not_override_a_real_engine_page() -> None:
    """A doc with a genuine vlm page + a rejected page still labels 'vlm', and
    vlm_pages counts only the genuine escalation success."""
    rec = route_record(
        _routes("vlm", "vlm-rejected-kept-docling"), doc_id="d3"
    )
    assert rec["route"] == "vlm"
    assert rec["vlm_pages"] == 1  # only the real vlm page, not the rejected one
