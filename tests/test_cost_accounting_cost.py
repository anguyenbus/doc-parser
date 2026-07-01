"""
test_cost_accounting_cost.py — Group 2 (Part B) tests for the both-engine cost
model and the pre-flight estimate.

Offline: synthetic per-file result lists with known ``call_counts``; no real
parsing, no AWS. Exercises the pure batch helpers ``_compute_cost_summary`` and
``_preflight_estimate`` and the ``TEXTRACT_PRICE_PER_PAGE`` constant.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_SPEC = importlib.util.spec_from_file_location(
    "parse_batch", Path(__file__).parent.parent / "scripts" / "parse_batch.py"
)
assert _SPEC and _SPEC.loader
parse_batch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(parse_batch)


def _result(vlm: int, textract: int, success: bool = True) -> dict[str, Any]:
    return {
        "filename": "f",
        "success": success,
        "vlm_calls": vlm,
        "textract_calls": textract,
    }


# ---------------------------------------------------------------------------
# Textract price constant exists and is positive.
# ---------------------------------------------------------------------------


def test_textract_price_constant_positive() -> None:
    assert parse_batch.TEXTRACT_PRICE_PER_PAGE > 0


# ---------------------------------------------------------------------------
# Textract-only batch reports NON-ZERO cost (regression on today's $0).
# ---------------------------------------------------------------------------


def test_textract_only_batch_nonzero_cost() -> None:
    results = [_result(vlm=0, textract=3), _result(vlm=0, textract=2)]
    cost = parse_batch._compute_cost_summary(results)

    assert cost["total_textract_calls"] == 5
    assert cost["total_vlm_calls"] == 0
    assert cost["textract_cost_usd"] > 0
    assert cost["bedrock_cost_usd"] == 0
    # Combined total is non-zero — the whole point of the regression.
    assert cost["estimated_cost_usd"] > 0
    assert cost["estimated_cost_usd"] == cost["textract_cost_usd"]


# ---------------------------------------------------------------------------
# Mixed batch sums both engines.
# ---------------------------------------------------------------------------


def test_mixed_batch_sums_both_engines() -> None:
    results = [_result(vlm=4, textract=0), _result(vlm=1, textract=6)]
    cost = parse_batch._compute_cost_summary(results)

    assert cost["total_vlm_calls"] == 5
    assert cost["total_textract_calls"] == 6

    expected_bedrock = 5 * parse_batch._AVG_COST_PER_CALL
    expected_textract = 6 * parse_batch.TEXTRACT_PRICE_PER_PAGE

    assert cost["bedrock_cost_usd"] == round(expected_bedrock, 6)
    assert cost["textract_cost_usd"] == round(expected_textract, 6)
    # estimated_cost_usd is the combined total (== bedrock + textract, unrounded
    # sum then rounded).
    assert cost["estimated_cost_usd"] == round(expected_bedrock + expected_textract, 6)


# ---------------------------------------------------------------------------
# Summary exposes per-engine breakdown fields.
# ---------------------------------------------------------------------------


def test_summary_exposes_breakdown_fields() -> None:
    cost = parse_batch._compute_cost_summary([_result(vlm=2, textract=3)])
    for field in (
        "estimated_cost_usd",
        "bedrock_cost_usd",
        "textract_cost_usd",
        "total_vlm_calls",
        "total_textract_calls",
    ):
        assert field in cost, field


# ---------------------------------------------------------------------------
# Pre-flight estimate: Σ page_count × per-engine cost (worst-case bound).
# ---------------------------------------------------------------------------


def test_preflight_estimate_worst_case_bound() -> None:
    page_counts = [3, 5, 2]
    total_pages = sum(page_counts)

    # Worst case: every page escalates. Provide the per-engine worst-case bounds.
    vlm_bound = parse_batch._preflight_estimate(page_counts, engine="vlm")
    textract_bound = parse_batch._preflight_estimate(page_counts, engine="textract")

    assert vlm_bound == round(total_pages * parse_batch._AVG_COST_PER_CALL, 6)
    assert textract_bound == round(total_pages * parse_batch.TEXTRACT_PRICE_PER_PAGE, 6)


def test_preflight_estimate_empty() -> None:
    assert parse_batch._preflight_estimate([], engine="vlm") == 0.0
    assert parse_batch._preflight_estimate([], engine="textract") == 0.0
