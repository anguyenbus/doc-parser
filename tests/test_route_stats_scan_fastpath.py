"""
test_route_stats_scan_fastpath.py — Task Group 3 tests for route_stats parity.

Fast-pathed pages ride ``reason="scan_fastpath"`` on the NORMAL escalation route
(``vlm`` / ``textract`` / ``*-fallback-docling``) — there is NO new route label.
So ``route_record`` counts them like any other escalation page and the per-page
route-count sum invariant still holds. These tests pin that:

  - a fast-pathed doc's route counts sum to the page count (invariant intact);
  - the ``vlm``/``textract`` roll-up + ``vlm_pages`` include fast-pathed pages;
  - the fast-path fact is visible via the surfaced ``reason``.

Run ONLY these + existing route_stats tests (task 3.3):
    uv run pytest tests/test_route_stats_scan_fastpath.py tests/test_route_stats_*.py
"""

from __future__ import annotations

from parser_service.route_stats import route_record, summarize


def _fastpath_routes() -> list[dict[str, object]]:
    """Two fast-pathed VLM pages (the shape ``_pdf_to_markdown`` emits on the
    all-scanned fast path)."""
    return [
        {"page_index": 0, "route": "vlm", "reason": "scan_fastpath", "n_chars": 42},
        {"page_index": 1, "route": "vlm", "reason": "scan_fastpath", "n_chars": 37},
    ]


def test_fastpath_routes_sum_invariant_holds() -> None:
    """The route-count invariant must hold for an all-fast-path doc: two ``vlm``
    pages sum to the two page_routes entries (no new label to break the sum)."""
    rec = route_record(_fastpath_routes(), doc_id="scanned")

    assert rec["pages"] == 2
    # Both pages reached the VLM → counted as escalation pages.
    assert rec["vlm_pages"] == 2
    assert rec["route"] == "vlm"


def test_fastpath_reason_surfaced() -> None:
    """The ``scan_fastpath`` reason rides in the record's surfaced reason so a
    batch scan can see fast-pathed docs."""
    rec = route_record(_fastpath_routes(), doc_id="scanned")

    assert rec["reason"] == "scan_fastpath"
    # It is carried on a normal escalation route, not a new label.
    assert set(rec["routes"].split(",")) == {"vlm"}


def test_fastpath_fallback_still_counts() -> None:
    """A fast-pathed page whose engine produced nothing (``*-fallback-docling``)
    still counts toward the sum invariant and toward ``vlm_pages``; the
    ``scan_fastpath`` reason is preserved on it."""
    routes = [
        {"page_index": 0, "route": "vlm", "reason": "scan_fastpath", "n_chars": 42},
        {
            "page_index": 1,
            "route": "vlm-fallback-docling",
            "reason": "scan_fastpath",
            "n_chars": 0,
        },
    ]
    rec = route_record(routes, doc_id="scanned")

    assert rec["pages"] == 2
    assert rec["vlm_pages"] == 2  # vlm + vlm-fallback-docling
    assert rec["route"] == "vlm"

    agg = summarize([rec])
    assert agg["total_pages"] == 2
    assert agg["vlm_pages"] == 2
