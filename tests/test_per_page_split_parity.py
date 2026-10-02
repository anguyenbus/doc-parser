"""
test_per_page_split_parity.py — offline guard for the per-page split measurement.

Roadmap item 1 compares one whole-file Docling run against Docling on physically
split one-page PDFs (``scripts/measure_per_page_docling.py``). This module guards
the two things that measurement — and the split-first design after it — rest on:

  - the split yields one file per page, in page order, and each file parses to
    the SAME per-page markdown the whole-file run produces for that page; and
  - evaluated at LOCAL index 0 with its own conversion result and its own
    text-layer token count, a split page gets the same gate outcome as the
    whole-file run gives the original index.

The split function and the per-page evaluation helper are loaded FROM the script
(the ``_load_script`` pattern of ``test_scripts_rewiring.py``), so the test and the
measurement cannot drift apart. Real Docling converter; no Bedrock, no Textract.

Fixture: ``three_page_digital.pdf`` (sentinels ``PAGE1``/Alpha, ``PAGE2``/Bravo,
``PAGE3``/Charlie — see ``test_multipage_regression.py``).

NOTE on the gate comparison: Docling keys ``confidence.pages`` by 1-based page
number, so on a WHOLE-FILE result ``quality_gate``'s ``get(page_no) or
get(page_no + 1)`` reads the previous page's entry for every page after the first,
while a one-page result reads its own. The outcomes are equal on this fixture
because all three pages carry the same Docling grades — that equality is a
property of the fixture, not of the lookup.

Run ONLY this module:
    uv run pytest tests/test_per_page_split_parity.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = Path(__file__).parent / "fixtures"
THREE_PAGE = FIXTURES / "three_page_digital.pdf"

# Escape-free per-page markers and unique body words, as in test_multipage_regression.py.
_MARKERS = (("PAGE1", "Alpha"), ("PAGE2", "Bravo"), ("PAGE3", "Charlie"))


def _load_script(name: str) -> Any:
    """Import scripts/<name>.py as a module (they self-insert src/ on sys.path)."""
    sys.path.insert(0, str(ROOT / "src"))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _require_three_page_fixture() -> None:
    if not THREE_PAGE.exists():
        pytest.skip(
            "three_page_digital.pdf missing — regenerate with "
            "`uv run python tests/fixtures/make_three_page_digital.py`"
        )


@pytest.fixture(scope="module")
def measure() -> Any:
    """The measurement script, loaded once so the test uses ITS split and helper."""
    return _load_script("measure_per_page_docling")


@pytest.fixture(scope="module")
def converted(measure: Any, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Whole-file and one-page conversions from ONE real converter (built once)."""
    _require_three_page_fixture()

    from parser_service.markdown_pipeline import _document_converter
    from parser_service.render import text_layer_tokens

    converter = _document_converter()
    split = measure.split_pdf(THREE_PAGE, tmp_path_factory.mktemp("one_page"))
    return {
        "whole": converter.convert(str(THREE_PAGE)),
        "whole_tokens": text_layer_tokens(THREE_PAGE),
        "split": {idx: converter.convert(str(path)) for idx, path in split.pages.items()},
        "split_tokens": {idx: text_layer_tokens(path) for idx, path in split.pages.items()},
    }


def test_split_yields_one_page_files_in_page_order(measure: Any, tmp_path: Path) -> None:
    """The split writes three one-page files under ``tmp_path``, mapped in page order."""
    _require_three_page_fixture()

    from pypdf import PdfReader

    split = measure.split_pdf(THREE_PAGE, tmp_path)

    assert list(split.pages) == [0, 1, 2]
    names = [path.name for path in split.pages.values()]
    assert names == sorted(names), names
    for idx, (marker, word) in enumerate(_MARKERS):
        path = split.pages[idx]
        assert path.parent == tmp_path
        reader = PdfReader(str(path))
        assert len(reader.pages) == 1
        text = reader.pages[0].extract_text()
        assert marker in text and word in text, f"page index {idx} holds the wrong page\n{text}"
    assert split.library == "pypdf"
    assert split.original_bytes == THREE_PAGE.stat().st_size
    assert split.split_bytes == sum(path.stat().st_size for path in split.pages.values())


def test_split_page_markdown_matches_whole_file_page(
    measure: Any, converted: dict[str, Any]
) -> None:
    """Each one-page file parses to its own page — byte-identical to the whole-file page."""
    for idx, (marker, word) in enumerate(_MARKERS):
        one_page = converted["split"][idx]
        measure.assert_one_page_result(one_page)

        # Local index 0 with the split file's own result; original index for the whole file.
        split_md = measure.evaluate_result_page(
            one_page, 0, converted["split_tokens"][idx].get(0)
        ).markdown
        whole_md = measure.evaluate_result_page(
            converted["whole"], idx, converted["whole_tokens"].get(idx)
        ).markdown

        assert marker in split_md and word in split_md, split_md
        for other_idx, (other_marker, other_word) in enumerate(_MARKERS):
            if other_idx != idx:
                assert other_marker not in split_md, f"neighbor bled into page {idx}\n{split_md}"
                assert other_word not in split_md, f"neighbor bled into page {idx}\n{split_md}"
        assert split_md == whole_md, f"page index {idx} differs from the whole-file page"


def test_split_page_gate_outcome_and_text_layer_match_whole_file(
    measure: Any, converted: dict[str, Any]
) -> None:
    """Gate outcome and text-layer token count at local index 0 equal the whole-file values."""
    for idx in range(len(_MARKERS)):
        split_tokens = converted["split_tokens"][idx].get(0)
        whole_tokens = converted["whole_tokens"].get(idx)
        assert split_tokens == whole_tokens, f"text-layer tokens differ on page index {idx}"

        split_outcome = measure.evaluate_result_page(converted["split"][idx], 0, split_tokens)
        whole_outcome = measure.evaluate_result_page(converted["whole"], idx, whole_tokens)
        assert (split_outcome.action, split_outcome.reason, split_outcome.layer) == (
            whole_outcome.action,
            whole_outcome.reason,
            whole_outcome.layer,
        ), f"gate outcome differs on page index {idx}"

        # The one-page lookup resolves the page's own confidence entry (key 1).
        lookup = measure.confidence_lookup(converted["split"][idx], 0)
        assert lookup["keys"] == [1] and lookup["verdict"] == "own", lookup
