"""
pdf.py — parse one PDF page (a one-page file made by the split).

A blank page (no text layer, renders almost white) is ``ok`` with route ``blank``.
Any other page goes through ``parse_to_markdown`` (Docling, the quality gate, and the
escalation engine when the gate promotes it); ``classify_page_result`` maps its
result to a unit state.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

from parser_service.formats.result import STATE_DEGRADED, STATE_FAILED, STATE_OK
from parser_service.observability import truncate
from parser_service.parsing.document import parse_to_markdown
from parser_service.parsing.render import render_page, text_layer_tokens
from parser_service.parsing.route_stats import (
    ROUTE_DOCLING_KEPT,
    ROUTE_TEXTRACT_FALLBACK,
    ROUTE_VLM_FALLBACK,
)

logger = logging.getLogger(__name__)

_ROUTE_BLANK = "blank"

# parse_to_markdown warning codes that mean the page produced nothing usable.
_FATAL_WARNING_CODES = (
    "docling_failed",
    "unhandled_exception",
    "input_too_large",
    "unsupported_type",
    "page_unparseable",
)

# Blank-page probe: a page with no text layer is rendered small and counted as
# blank when almost no pixel is darker than near-white.
_BLANK_PROBE_DPI = 36
_BLANK_DARK_LEVEL = 235
_BLANK_MAX_DARK_FRACTION = 0.0005


def _is_blank_page(page_pdf: Path) -> bool:
    """True iff the page has no text layer AND renders (almost) entirely white.

    A blank page has nothing for Docling or an engine to recover; without this
    probe it would come back empty and fail the whole file. Never raises — any
    probe failure means "not blank" and the page takes the normal path.
    """
    try:
        if text_layer_tokens(page_pdf).get(0, 0) > 0:
            return False
        from PIL import Image

        image = Image.open(io.BytesIO(render_page(page_pdf, 0, dpi=_BLANK_PROBE_DPI)))
        histogram = image.convert("L").histogram()
        total = sum(histogram)
        dark = sum(histogram[:_BLANK_DARK_LEVEL])
        return total > 0 and dark / total <= _BLANK_MAX_DARK_FRACTION
    except Exception as exc:  # noqa: BLE001
        logger.warning("blank-page probe failed for %s: %s", page_pdf.name, truncate(str(exc)))
        return False


def classify_page_result(result: dict[str, Any]) -> tuple[str, str | None]:
    """Map one page's ``parse_to_markdown`` result to ``(state, reason)``.

    ok        Docling kept by the gate, the engine's output shipped, or arbitration
              kept Docling over a successful engine call.
    degraded  the gate rejected Docling but its text was kept anyway: the engine
              failed (``*-fallback-docling``) or the budget cap suppressed the call.
    failed    nothing usable: Docling failed, the engine failed with no Docling
              text to fall back to, or the page came back empty.
    """
    for warning in result.get("warnings", []):
        code = warning.get("code")
        if code in _FATAL_WARNING_CODES:
            return STATE_FAILED, f"{code}: {warning.get('message', '')}"

    routes = result.get("page_routes", [])
    if not routes:
        return STATE_FAILED, "no_route_recorded"
    route = routes[0].get("route")
    route_reason = routes[0].get("reason")

    if route in (ROUTE_VLM_FALLBACK, ROUTE_TEXTRACT_FALLBACK):
        return STATE_DEGRADED, f"{route}: {route_reason}"
    if route == ROUTE_DOCLING_KEPT and route_reason == "budget_exhausted_no_escalation":
        return STATE_DEGRADED, route_reason
    if not str(result.get("markdown", "")).strip():
        return STATE_FAILED, f"empty_output: route={route}"
    return STATE_OK, None


def parse_page(
    page_pdf: Path, page_number: int, escalate: bool
) -> tuple[str, str | None, str, dict[str, Any]]:
    """Probe and parse one page file: ``(state, reason, markdown, record fields)``."""
    if _is_blank_page(page_pdf):
        return STATE_OK, None, "", {"route": _ROUTE_BLANK, "route_reason": "blank_page"}
    result = parse_to_markdown(page_pdf, escalate=escalate)

    state, reason = classify_page_result(result)
    # The one-page file numbers its only page 0; store the real 0-based index.
    routes = [{**r, "page_index": page_number - 1} for r in result.get("page_routes", [])]
    warnings = [
        {**w, "page_index": page_number - 1} if "page_index" in w else dict(w)
        for w in result.get("warnings", [])
    ]
    return (
        state,
        reason,
        str(result.get("markdown", "")),
        {
            "route": routes[0].get("route") if routes else None,
            "route_reason": routes[0].get("reason") if routes else None,
            "page_routes": routes,
            "warnings": warnings,
            "confidence": result.get("confidence", {}).get("document"),
            "call_counts": result.get("call_counts", {}),
        },
    )
