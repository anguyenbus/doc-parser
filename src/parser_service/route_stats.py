"""
route_stats.py

Derive per-document routing telemetry from parser_service output.

Every parser output records how each document was handled:
  - VLM-produced elements get `element_id` values prefixed with `vlm_`.
  - A `vlm_promoted` warning is added when the image quality gate escalates a
    page to the VLM (the warning message carries the gate layer + reason).
  - VLM failures surface as `image_unparseable` / `vlm_invalid_shape` /
    `page_unparseable`; a failed table-crop VLM call leaves `vlm_table_fallback`.

This module turns that into a tidy record per document plus a CSV writer, so a
batch run (or an ad-hoc scan of an output directory) yields a routing breakdown:

    doc_id  route  elems  vlm_el  warn_codes  reason

`route` is one of:
  - "vlm"          VLM produced or replaced this document's content
  - "vlm-failed"   VLM was reached for this document but errored
  - "docling-kept" Docling output was kept; the VLM was not used
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

# Warning codes that mean "the VLM was attempted but failed for this doc/page".
_VLM_FAILED_CODES = {"image_unparseable", "vlm_invalid_shape", "page_unparseable"}
# Warning codes whose message is worth surfacing as the per-doc `reason`.
_REASON_CODES = (
    "vlm_promoted",
    "image_unparseable",
    "vlm_invalid_shape",
    "page_unparseable",
    "vlm_table_fallback",
    "docling_failed",
)

# CSV column order.
FIELDNAMES = ["doc_id", "route", "elems", "vlm_el", "warn_codes", "reason"]


def route_record(output: dict[str, Any], doc_id: str | None = None) -> dict[str, Any]:
    """Build a one-row routing record from a single parser output dict.

    Args:
        output: A parser_service.parse() result (schema-conformant dict).
        doc_id: Optional override; defaults to source.doc_id, then filename.

    Returns:
        Dict with keys matching FIELDNAMES.
    """
    source = output.get("source", {})
    if doc_id is None:
        doc_id = source.get("doc_id") or source.get("filename") or "?"

    elements = output.get("elements", [])
    warnings = output.get("warnings", [])

    vlm_el = sum(1 for e in elements if str(e.get("element_id", "")).startswith("vlm_"))
    codes = sorted({w.get("code") for w in warnings if w.get("code")})
    code_set = set(codes)
    promoted = "vlm_promoted" in code_set
    used_vlm = promoted or vlm_el > 0

    if used_vlm:
        route = "vlm"
    elif code_set & _VLM_FAILED_CODES:
        route = "vlm-failed"
    else:
        route = "docling-kept"

    reason = ""
    for w in warnings:
        if w.get("code") in _REASON_CODES:
            reason = w.get("message", "")
            break

    return {
        "doc_id": doc_id,
        "route": route,
        "elems": len(elements),
        "vlm_el": vlm_el,
        "warn_codes": ",".join(codes) or "-",
        "reason": reason,
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, int]:
    """Return aggregate counts over a list of route records."""
    total = len(records)
    used_vlm = sum(1 for r in records if r["route"] == "vlm")
    vlm_failed = sum(1 for r in records if r["route"] == "vlm-failed")
    docling_kept = sum(1 for r in records if r["route"] == "docling-kept")
    return {
        "total": total,
        "used_vlm": used_vlm,
        "vlm_failed": vlm_failed,
        "docling_kept": docling_kept,
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
    lines = [
        f"{'doc_id':36} {'route':12} {'elems':>5} {'vlm_el':>6} {'warn_codes':22} reason"
    ]
    for r in records:
        lines.append(
            f"{str(r['doc_id'])[:36]:36} {r['route']:12} {r['elems']:>5} "
            f"{r['vlm_el']:>6} {str(r['warn_codes'])[:22]:22} {str(r['reason'])[:60]}"
        )
    s = summarize(records)
    lines.append("")
    lines.append(
        f"Total: {s['total']} | used VLM: {s['used_vlm']} | "
        f"vlm-failed: {s['vlm_failed']} | docling-kept: {s['docling_kept']}"
    )
    return "\n".join(lines)
