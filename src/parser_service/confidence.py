"""confidence.py — advisory, routing-derived document/page confidence heuristic.

Emits a 0-1 confidence per parsed page and a content-weighted document score,
derived ENTIRELY from signals ``parse_to_markdown`` already computes on each
``page_routes`` record (the page ``route``, the quality-gate signal booleans, and
the arbitration outcome). 

IMPORTANT — advisory heuristic, NOT a calibrated probability. This score is a
transparent, hand-set mapping from routing outcomes to a number. It is NOT
validated against gold NED/TEDS and is NOT a probability of correctness. It is
purely advisory: it MUST NEVER gate parsing, routing, the quality gate, the
promote/keep decision, or the shipped markdown. It is computed read-only over the
already-populated ``page_routes`` and emitted only in the ``parse_to_markdown``
return dict.

Both functions are pure and deterministic: same input dict → same output, no
I/O, no environment reads, no randomness.
"""

from __future__ import annotations

from typing import Any

from parser_service.route_stats import (
    ROUTE_DOCLING_KEPT,
    ROUTE_TEXTRACT,
    ROUTE_TEXTRACT_FALLBACK,
    ROUTE_TEXTRACT_REJECTED,
    ROUTE_VLM,
    ROUTE_VLM_FALLBACK,
    ROUTE_VLM_REJECTED,
)

# Per-tier base scores (spec table). Hand-set heuristic — see module docstring.
_SCORE_DOCLING_KEPT = 0.95  # gate kept a clean digital Docling page
_SCORE_ENGINE_PASSING = 0.85  # engine shipped and its quality signal passed
_SCORE_REJECTED_KEPT_DOCLING = 0.70  # promoted, but the clean Docling render shipped
_SCORE_ENGINE_FAILING_KEPT = 0.60  # engine shipped but its quality signal failed
_SCORE_FALLBACK_DOCLING = 0.50  # engine errored/emptied; gate-flagged Docling shipped
_SCORE_ERROR = 0.15  # no Docling and no engine output — empty/unparseable page

# Advisory review threshold. A page is flagged (a ``low_confidence_page`` warning)
# when its score is strictly BELOW this — i.e. the pipeline does not TRUST the
# output it shipped: the engine rendering failed the quality proxy
# (engine-failing-kept, 0.60), the engine failed entirely and gate-flagged Docling
# shipped (fallback-docling, 0.50), or nothing rendered (error, 0.15). The TRUSTED
# tiers are not flagged: rejected-kept-docling (0.70 — arbitration verified the
# Docling render is clean), engine-passing (0.85), and docling-kept (0.95).
#
# It is DERIVED as the midpoint between the highest untrusted tier and the lowest
# trusted tier, so the boundary is an intentional gap BETWEEN tiers — never a magic
# float that happens to equal a tier value (a coincidence would make "does this
# tier warn?" silently flip on any tiny retune of either number). Advisory only —
# like the scores, it never gates parsing.
LOW_CONFIDENCE_THRESHOLD = (_SCORE_ENGINE_FAILING_KEPT + _SCORE_REJECTED_KEPT_DOCLING) / 2  # 0.65

# Route vocabulary — sourced from ``route_stats`` (the single source of truth,
# itself cross-checked byte-for-byte against ``markdown_pipeline._ROUTE_*`` in
# tests). Importing rather than re-declaring the literals means a route rename
# can never silently mis-tier a page here. ``route_stats`` imports nothing from
# this package, so there is no import cycle (``markdown_pipeline`` is the module
# that imports us, not ``route_stats``).
_DOCLING_KEPT = ROUTE_DOCLING_KEPT
_ENGINE_ROUTES = {ROUTE_VLM, ROUTE_TEXTRACT}
_REJECTED_ROUTES = {ROUTE_VLM_REJECTED, ROUTE_TEXTRACT_REJECTED}
_FALLBACK_ROUTES = {ROUTE_VLM_FALLBACK, ROUTE_TEXTRACT_FALLBACK}


def page_confidence(record: dict[str, Any]) -> float:
    """Map one ``page_routes`` record to an advisory ``[0, 1]`` confidence.

    Pure and deterministic — a function of the record's ``route`` plus the
    quality-signal booleans, read defensively with ``.get(...)`` because the
    record shape differs across paths (the arbitration-off record carries only
    ``vlm_quality_passes``; the arbitration-on record carries the full descriptive
    set; ``docling-kept`` / ``*-fallback-docling`` / error records carry no
    booleans at all).

    This is a transparent routing-derived HEURISTIC, NOT a gold-NED/TEDS-calibrated
    probability, and is advisory only — it never gates anything.

    Tier mapping (spec table):
      - ``docling-kept`` → 0.95
      - ``vlm`` / ``textract`` with a passing quality signal → 0.85; with a
        present-and-False signal (kept-but-failing) → 0.60; with NO signal at all
        (the error/empty ``_fallback`` record, ``docling_fallback is None``) → 0.15
      - ``*-rejected-kept-docling`` → 0.70
      - ``*-fallback-docling`` → 0.50
      - any other / unknown route → 0.15 (error tier)
    """
    route = record.get("route")

    if route == _DOCLING_KEPT:
        return _SCORE_DOCLING_KEPT

    if route in _REJECTED_ROUTES:
        return _SCORE_REJECTED_KEPT_DOCLING

    if route in _FALLBACK_ROUTES:
        return _SCORE_FALLBACK_DOCLING

    if route in _ENGINE_ROUTES:
        # The engine route_ok is written by three paths: arbitration-off (only
        # vlm_quality_passes present), arbitration-on kept-engine (engine_* and
        # vlm_* present), and the error/empty _fallback (docling_fallback is None,
        # NO quality boolean at all). Read whichever passing signal is present.
        passes = record.get("engine_quality_passes")
        if passes is None:
            passes = record.get("vlm_quality_passes")
        if passes is None:
            # No quality signal on an engine route → the error/empty fallback.
            return _SCORE_ERROR
        return _SCORE_ENGINE_PASSING if passes else _SCORE_ENGINE_FAILING_KEPT

    # Unknown / unrecorded route — treat as error tier (never above a real tier).
    return _SCORE_ERROR


def document_confidence(page_routes: list[dict[str, Any]]) -> float:
    """Content-weighted mean of the per-page confidences over ``page_routes``.

    ``document = Σ(page_confidence(r) × n_chars(r)) / Σ(n_chars(r))``, weight =
    the page's rendered ``n_chars`` (``>= 0``). Because the weighting is a convex
    combination, the document score stays within ``[min_page, max_page]`` — a long
    clean doc with one tiny bad page stays high, and it can never escape the tier
    band its pages occupy.

    Degenerate cases → ``0.0``: no ``page_routes`` at all, or ``Σ(n_chars) == 0``
    (no rendered characters on any page). Pure and deterministic.

    Advisory heuristic — see ``page_confidence`` / the module docstring.
    """
    if not page_routes:
        return 0.0

    total_weight = 0
    weighted_sum = 0.0
    for record in page_routes:
        weight = record.get("n_chars", 0)
        total_weight += weight
        weighted_sum += page_confidence(record) * weight

    if total_weight == 0:
        return 0.0
    return weighted_sum / total_weight
