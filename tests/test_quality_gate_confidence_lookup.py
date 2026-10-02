"""
test_quality_gate_confidence_lookup.py — Layer 1 reads each page's OWN confidence.

``evaluate_page`` takes a 0-based page index; Docling keys ``confidence.pages`` by
1-based page number. The lookup used to try the 0-based index first, so on a
multi-page result every page after the first read the previous page's grade.

The last test runs real Docling on the 3-page fixture and fails if a Docling
upgrade changes how ``confidence.pages`` is keyed.

Run ONLY these tests:
    uv run pytest tests/test_quality_gate_confidence_lookup.py
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from parser_service.quality_gate import _layer1_decision

FIXTURES = Path(__file__).parent / "fixtures"
THREE_PAGE = FIXTURES / "three_page_digital.pdf"


def _result(grades: dict[int, str]) -> SimpleNamespace:
    """A conversion-result stand-in whose ``confidence.pages`` maps key -> low grade."""
    pages = {
        key: SimpleNamespace(low_grade=SimpleNamespace(value=grade), mean_grade=None)
        for key, grade in grades.items()
    }
    return SimpleNamespace(confidence=SimpleNamespace(pages=pages))


@pytest.mark.parametrize(
    ("page_no", "promoted"),
    [(0, False), (1, True), (2, False)],
)
def test_one_based_keys_read_the_pages_own_grade(page_no: int, promoted: bool) -> None:
    # Page 2 (index 1) is the only fair page; its neighbours must not inherit it.
    result = _result({1: "good", 2: "fair", 3: "good"})

    decision = _layer1_decision(result, page_no)

    assert (decision is not None) is promoted


@pytest.mark.parametrize(
    ("page_no", "promoted"),
    [(0, False), (1, True), (2, False)],
)
def test_zero_based_keys_are_still_read_correctly(page_no: int, promoted: bool) -> None:
    result = _result({0: "good", 1: "fair", 2: "good"})

    decision = _layer1_decision(result, page_no)

    assert (decision is not None) is promoted


def test_single_page_result_reads_its_only_entry() -> None:
    assert _layer1_decision(_result({1: "poor"}), 0) is not None
    assert _layer1_decision(_result({1: "good"}), 0) is None


def test_missing_entry_does_not_promote() -> None:
    assert _layer1_decision(_result({1: "fair"}), 5) is None


def test_docling_keys_confidence_pages_from_one() -> None:
    """Guard: the lookup above assumes 1-based keys unless a key 0 is present."""
    from parser_service.markdown_pipeline import _document_converter

    result = _document_converter().convert(str(THREE_PAGE))

    assert sorted(result.confidence.pages.keys()) == [1, 2, 3]
