"""
route_stats.py

Derive per-document routing telemetry from the markdown-first pipeline's
``page_routes`` (``markdown_pipeline.parse_to_markdown`` output).

Each ``parse_to_markdown`` result carries a ``page_routes`` list — one entry per
logical page — of the form ``{page_index, route, reason, ...}``. The ``route``
vocabulary is EXACTLY what ``markdown_pipeline`` emits:

  - "docling-kept"         the gate kept Docling's page markdown (no VLM)
  - "vlm"                  VLM markdown replaced this page's slice
  - "vlm-fallback-docling" the page was promoted to the VLM but the VLM produced
                           garbage, so Docling's slice was kept
  - "*-rejected-kept-docling"  arbitration (PARSER_ESCALATION_ARBITRATION)
                           rejected the engine output as low-quality and kept
                           Docling's clean slice — a Docling-output page

This module turns those per-page routes into a tidy per-document record plus a
CSV writer, so a batch run (or an ad-hoc scan of an output directory) yields a
routing breakdown:

    doc_id  route  pages  vlm_pages  routes  reason

``route`` (the per-DOCUMENT roll-up column) is one of:
  - "vlm"          at least one page used the VLM (``vlm`` route present)
  - "vlm-failed"   the VLM was reached but every promoted page fell back
  - "docling-kept" no VLM page; Docling output was kept throughout
  - "error"        the document failed before producing any page routes

CRITICAL (route-vocabulary invariant): the per-page route counts MUST sum to the
page count (the number of ``page_routes`` entries). A vocabulary mismatch — e.g.
counting an old ``vlm_p…`` element-ID prefix that no longer exists — would
silently report zeros without failing, surfacing only as misleading numbers in
the benchmark. ``route_record`` asserts the sum invariant so a mismatch fails
loudly.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

# The exact per-page ``route`` vocabulary emitted by ``markdown_pipeline``.
# There are two escalation engines (selected by ``PARSER_ESCALATION_ENGINE``); a
# single run uses exactly one of them, so a page is escalated via either the VLM
# or Textract, never both.
ROUTE_DOCLING_KEPT = "docling-kept"
ROUTE_VLM = "vlm"
ROUTE_VLM_FALLBACK = "vlm-fallback-docling"
ROUTE_TEXTRACT = "textract"
ROUTE_TEXTRACT_FALLBACK = "textract-fallback-docling"
# Arbitration rejected the engine output; the clean Docling rendering shipped.
# A rejected-kept-docling page is a DOCLING-output page (NOT an escalation
# success): it counts toward the sum invariant but is excluded from the
# vlm/textract roll-up and from vlm_pages.
ROUTE_VLM_REJECTED = "vlm-rejected-kept-docling"
ROUTE_TEXTRACT_REJECTED = "textract-rejected-kept-docling"

# Routes where an escalation engine produced the page vs fell back to Docling.
_ENGINE_ROUTES = (ROUTE_VLM, ROUTE_TEXTRACT)
_ENGINE_FALLBACK_ROUTES = (ROUTE_VLM_FALLBACK, ROUTE_TEXTRACT_FALLBACK)

# CSV column order. ``doc_id`` and ``route`` are the columns ``aggregate_benchmark.py``
# consumes (it reads only those two), and are kept first to preserve that contract.
FIELDNAMES = ["doc_id", "route", "pages", "vlm_pages", "routes", "reason"]


def route_record(page_routes: list[dict[str, Any]], doc_id: str) -> dict[str, Any]:
    """Build a one-row routing record from a ``page_routes`` list.

    Args:
        page_routes: The ``page_routes`` list from ``parse_to_markdown`` — a list
            of ``{page_index, route, reason, ...}`` dicts.
        doc_id: The document identifier (e.g. the input filename stem).

    Returns:
        Dict with keys matching ``FIELDNAMES``.

    Raises:
        AssertionError: if the per-page route counts do not sum to the number of
            ``page_routes`` entries (catches a route-vocabulary mismatch loudly).
    """
    page_count = len(page_routes)

    docling_kept = sum(1 for r in page_routes if r.get("route") == ROUTE_DOCLING_KEPT)
    vlm = sum(1 for r in page_routes if r.get("route") == ROUTE_VLM)
    vlm_fallback = sum(1 for r in page_routes if r.get("route") == ROUTE_VLM_FALLBACK)
    textract = sum(1 for r in page_routes if r.get("route") == ROUTE_TEXTRACT)
    textract_fallback = sum(
        1 for r in page_routes if r.get("route") == ROUTE_TEXTRACT_FALLBACK
    )
    # Arbitration rejected the engine output; Docling shipped. These are
    # Docling-output pages — counted for the sum invariant, but EXCLUDED from the
    # vlm/textract roll-up and from vlm_pages below.
    vlm_rejected = sum(1 for r in page_routes if r.get("route") == ROUTE_VLM_REJECTED)
    textract_rejected = sum(
        1 for r in page_routes if r.get("route") == ROUTE_TEXTRACT_REJECTED
    )

    # INVARIANT: every page's route is one of the known vocabulary values, so the
    # counts (both escalation engines + Docling) must sum to the page count. If this
    # fires, the route vocabulary emitted by markdown_pipeline drifted from what we
    # count here.
    counted = (
        docling_kept
        + vlm
        + vlm_fallback
        + textract
        + textract_fallback
        + vlm_rejected
        + textract_rejected
    )
    assert counted == page_count, (
        f"route-count invariant violated for {doc_id!r}: counted {counted} "
        f"(docling-kept={docling_kept}, vlm={vlm}, vlm-fallback-docling={vlm_fallback}, "
        f"textract={textract}, textract-fallback-docling={textract_fallback}, "
        f"vlm-rejected-kept-docling={vlm_rejected}, "
        f"textract-rejected-kept-docling={textract_rejected}) "
        f"!= {page_count} page_routes; the route vocabulary drifted "
        f"(saw routes: {sorted(str(r.get('route')) for r in page_routes)})"
    )

    # Pages that reached an escalation engine AND shipped its output (or fell back
    # on garbage). A run uses one engine, so this is the VLM count or the Textract
    # count. Rejected-kept-docling pages are DELIBERATELY excluded: arbitration
    # rejected the engine output, so Docling shipped — it is not an escalation
    # success and must not inflate vlm_pages or the vlm/textract roll-up below.
    vlm_pages = vlm + vlm_fallback + textract + textract_fallback

    if vlm > 0:
        route = "vlm"
    elif textract > 0:
        route = "textract"
    elif vlm_fallback > 0:
        # The VLM was reached on every promoted page but produced nothing usable.
        route = "vlm-failed"
    elif textract_fallback > 0:
        # Textract was reached on every promoted page but produced nothing usable.
        route = "textract-failed"
    else:
        route = "docling-kept"

    # Surface the first non-empty page reason (gate Decision.reason) for context.
    reason = ""
    for r in page_routes:
        if r.get("reason"):
            reason = str(r["reason"])
            break

    return {
        "doc_id": doc_id,
        "route": route,
        "pages": page_count,
        "vlm_pages": vlm_pages,
        "routes": ",".join(f"{r.get('route')}" for r in page_routes) or "-",
        "reason": reason,
    }


def error_record(doc_id: str, reason: str) -> dict[str, Any]:
    """Build a record for a document that failed before producing page routes."""
    return {
        "doc_id": doc_id,
        "route": "error",
        "pages": 0,
        "vlm_pages": 0,
        "routes": "-",
        "reason": reason[:200],
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, int]:
    """Return aggregate counts over a list of route records."""
    total = len(records)
    used_vlm = sum(1 for r in records if r["route"] == "vlm")
    vlm_failed = sum(1 for r in records if r["route"] == "vlm-failed")
    used_textract = sum(1 for r in records if r["route"] == "textract")
    textract_failed = sum(1 for r in records if r["route"] == "textract-failed")
    docling_kept = sum(1 for r in records if r["route"] == "docling-kept")
    errors = sum(1 for r in records if r["route"] == "error")
    total_pages = sum(int(r.get("pages", 0)) for r in records)
    vlm_pages = sum(int(r.get("vlm_pages", 0)) for r in records)
    return {
        "total": total,
        "used_vlm": used_vlm,
        "vlm_failed": vlm_failed,
        "used_textract": used_textract,
        "textract_failed": textract_failed,
        "docling_kept": docling_kept,
        "errors": errors,
        "total_pages": total_pages,
        "vlm_pages": vlm_pages,
    }


def write_route_csv(records: list[dict[str, Any]], path: Path) -> None:
    """Write route records to a CSV at `path` (creates parent dirs)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)


def format_table(records: list[dict[str, Any]]) -> str:
    """Render records as an aligned text table with a totals footer."""
    lines = [f"{'doc_id':36} {'route':12} {'pages':>5} {'vlm_pg':>6} {'routes':22} reason"]
    for r in records:
        lines.append(
            f"{str(r['doc_id'])[:36]:36} {r['route']:12} {r['pages']:>5} "
            f"{r['vlm_pages']:>6} {str(r['routes'])[:22]:22} {str(r['reason'])[:60]}"
        )
    s = summarize(records)
    lines.append("")
    lines.append(
        f"Total: {s['total']} docs / {s['total_pages']} pages | "
        f"used VLM: {s['used_vlm']} | vlm-failed: {s['vlm_failed']} | "
        f"used Textract: {s['used_textract']} | textract-failed: {s['textract_failed']} | "
        f"docling-kept: {s['docling_kept']} | errors: {s['errors']} | "
        f"escalated pages: {s['vlm_pages']}"
    )
    return "\n".join(lines)
