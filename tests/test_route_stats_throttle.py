"""
test_route_stats_throttle.py — Task Group 4 tests: ``route_stats`` surfaces an
exhausted-throttle count DERIVED FROM ``reason`` (NOT a new route label), while
the per-page route counts still sum to the page count (invariant holds).

Run ONLY these tests (task 4.3):
    uv run pytest tests/test_route_stats_throttle.py tests/test_route_stats_textract.py
"""

from __future__ import annotations

from parser_service.route_stats import route_record, summarize


def _route(route: str, reason: str | None = None) -> dict[str, object]:
    return {"page_index": 0, "route": route, "reason": reason}


def test_throttled_fallback_counted_in_record() -> None:
    page_routes = [
        _route("docling-kept"),
        _route("vlm-fallback-docling", reason="throttled"),
        _route("vlm"),
    ]
    rec = route_record(page_routes, doc_id="doc1")
    # New reason-derived field.
    assert rec["throttled_pages"] == 1
    # Sum invariant still holds (route_record would assert otherwise).
    assert rec["pages"] == 3


def test_no_throttle_reports_zero() -> None:
    page_routes = [
        _route("docling-kept"),
        _route("vlm-fallback-docling", reason="low_coverage: ..."),
    ]
    rec = route_record(page_routes, doc_id="doc2")
    assert rec["throttled_pages"] == 0


def test_summarize_aggregates_throttled_pages() -> None:
    records = [
        route_record(
            [_route("vlm-fallback-docling", reason="throttled"), _route("vlm")],
            doc_id="a",
        ),
        route_record(
            [_route("textract-fallback-docling", reason="throttled")],
            doc_id="b",
        ),
        route_record([_route("docling-kept")], doc_id="c"),
    ]
    s = summarize(records)
    assert s["throttled_pages"] == 2
