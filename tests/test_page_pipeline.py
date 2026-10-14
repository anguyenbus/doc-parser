"""
test_page_pipeline.py — the split-first page pipeline (``parser_service.page_pipeline``).

Covers split + manifest, page states, the fail-closed stitch with page markers,
resume, the attempt limit, the pinned config and the blank-page probe.

Offline and fast: ``parse_to_markdown`` is replaced at the ``page_pipeline`` seam
with a fake keyed by page number, and pages run in-process (``workers=0``), so no
Docling model and no engine is involved. The last test runs the real pool with
real Docling on a 3-page fixture, with escalation off (no AWS).

Run ONLY these tests:
    uv run pytest tests/test_page_pipeline.py
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from parser_service import page_pipeline
from parser_service.page_pipeline import (
    classify_page_result,
    parse_pdf_pages,
    split_pdf,
    strip_page_markers,
    work_dir_for,
)

FIXTURES = Path(__file__).parent / "fixtures"
THREE_PAGE = FIXTURES / "three_page_digital.pdf"


def _result(
    markdown: str, route: str = "docling-kept", reason: str | None = None
) -> dict[str, Any]:
    """A ``parse_to_markdown``-shaped result for a one-page file."""
    return {
        "markdown": markdown,
        "page_routes": [
            {"page_index": 0, "route": route, "reason": reason, "n_chars": len(markdown)}
        ],
        "warnings": [],
        "confidence": {"document": 1.0, "pages": []},
        "call_counts": {"vlm": 1 if route == "vlm" else 0, "textract": 0},
    }


def _install_fake(
    monkeypatch: pytest.MonkeyPatch, by_page: Callable[[int], dict[str, Any]]
) -> list[int]:
    """Replace ``parse_to_markdown`` with ``by_page(page_number)``; return the call log."""
    calls: list[int] = []

    def fake(path: Path, escalate: bool = True) -> dict[str, Any]:
        page_number = int(Path(path).stem)
        calls.append(page_number)
        return by_page(page_number)

    monkeypatch.setattr(page_pipeline, "parse_to_markdown", fake)
    return calls


def test_split_writes_one_page_files_and_manifest_last(tmp_path: Path) -> None:
    from pypdf import PdfReader

    work_dir = work_dir_for(THREE_PAGE, tmp_path)
    work_dir.mkdir(parents=True)
    manifest = split_pdf(THREE_PAGE, work_dir)

    assert manifest["page_count"] == 3
    assert manifest["source_name"] == THREE_PAGE.name
    for name in ("0001.pdf", "0002.pdf", "0003.pdf"):
        assert len(PdfReader(str(work_dir / "pages" / name)).pages) == 1
    # Idempotent: a second call returns the stored manifest unchanged.
    assert split_pdf(THREE_PAGE, work_dir) == manifest


def test_all_pages_ok_stitches_with_page_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))

    result = parse_pdf_pages(THREE_PAGE, tmp_path, workers=0)

    assert result["state"] == "complete"
    assert result["counts"] == {"ok": 3, "degraded": 0, "failed": 0, "pending": 0}
    markdown = Path(result["output_path"]).read_text()
    assert markdown == (
        "==== page 1 ===\n\ntext of page 1\n\n"
        "==== page 2 ===\n\ntext of page 2\n\n"
        "==== page 3 ===\n\ntext of page 3\n"
    )
    assert strip_page_markers(markdown) == "text of page 1\n\ntext of page 2\n\ntext of page 3\n"

    metadata = json.loads((Path(result["out_dir"]) / "output.json").read_text())
    start, end = metadata["pages"][1]["char_span"]
    assert markdown[start:end] == "==== page 2 ===\n\ntext of page 2"


def test_degraded_page_fails_the_file_and_writes_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def by_page(n: int) -> dict[str, Any]:
        if n == 2:
            return _result("docling text", route="vlm-fallback-docling", reason="throttled")
        return _result(f"text of page {n}")

    _install_fake(monkeypatch, by_page)

    result = parse_pdf_pages(THREE_PAGE, tmp_path, workers=0, partial=True)
    out_dir = Path(result["out_dir"])

    assert result["state"] == "failed"
    assert result["output_path"] is None
    assert not (out_dir / "output.md").exists()
    assert [(p["page"], p["state"]) for p in result["not_ok_pages"]] == [(2, "degraded")]
    status = json.loads((out_dir / "status.json").read_text())
    assert status["state"] == "failed"

    partial = Path(result["partial_path"]).read_text()
    assert "==== page 2 (missing: degraded) ===" in partial
    assert "docling text" not in partial
    assert "text of page 3" in partial


def test_rerun_skips_ok_pages_and_retries_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    healthy = {"now": False}

    def by_page(n: int) -> dict[str, Any]:
        if n == 2 and not healthy["now"]:
            raise RuntimeError("boom")
        return _result(f"text of page {n}")

    calls = _install_fake(monkeypatch, by_page)

    first = parse_pdf_pages(THREE_PAGE, tmp_path, workers=0)
    assert first["state"] == "failed"
    assert first["not_ok_pages"][0]["reason"].startswith("exception: RuntimeError: boom")
    assert sorted(calls) == [1, 2, 3]

    calls.clear()
    healthy["now"] = True
    second = parse_pdf_pages(THREE_PAGE, tmp_path, workers=0)
    assert second["state"] == "complete"
    assert calls == [2]


def test_attempt_limit_makes_a_page_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_fake(
        monkeypatch, lambda n: _result("", route="vlm") if n == 3 else _result(f"p{n}")
    )

    for _ in range(4):
        result = parse_pdf_pages(THREE_PAGE, tmp_path, workers=0, max_attempts=2)

    assert calls.count(3) == 2
    assert result["state"] == "failed"
    page = result["not_ok_pages"][0]
    assert (page["page"], page["state"], page["attempts"], page["retryable"]) == (
        3,
        "failed",
        2,
        False,
    )


def test_page_with_no_result_after_all_attempts_is_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page that killed its worker leaves an attempt count and no record."""
    _install_fake(monkeypatch, lambda n: _result(f"p{n}"))
    work_dir = work_dir_for(THREE_PAGE, tmp_path)
    work_dir.mkdir(parents=True)
    split_pdf(THREE_PAGE, work_dir)
    (work_dir / "attempts" / "0001").write_text("3")

    result = parse_pdf_pages(THREE_PAGE, tmp_path, workers=0, max_attempts=3)

    assert result["state"] == "failed"
    assert result["not_ok_pages"][0]["page"] == 1
    assert result["not_ok_pages"][0]["reason"].startswith("no_result_after_3_attempts")


def test_changed_config_parses_into_its_own_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_fake(monkeypatch, lambda n: _result(f"p{n}"))
    monkeypatch.setenv("PARSER_ESCALATION_ENGINE", "vlm")
    first = parse_pdf_pages(THREE_PAGE, tmp_path, workers=0)

    monkeypatch.setenv("PARSER_ESCALATION_ENGINE", "textract")
    second = parse_pdf_pages(THREE_PAGE, tmp_path, workers=0)

    assert first["state"] == second["state"] == "complete"
    assert sorted(calls) == [1, 1, 2, 2, 3, 3]  # nothing reused across configs
    sha, _, config_a = first["work_id"].partition("/")
    assert second["work_id"] == f"{sha}/{second['work_id'].rpartition('/')[2]}"
    assert second["work_id"].rpartition("/")[2] != config_a
    manifest = json.loads((tmp_path / "work" / first["work_id"] / "manifest.json").read_text())
    assert manifest["config"]["escalation_engine"] == "vlm"  # the old work is untouched


def test_blank_page_is_ok_without_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pypdf import PdfReader, PdfWriter

    source = tmp_path / "with_blank.pdf"
    writer = PdfWriter()
    writer.add_page(PdfReader(str(THREE_PAGE)).pages[0])
    writer.add_blank_page(width=595, height=842)
    with source.open("wb") as handle:
        writer.write(handle)
    calls = _install_fake(monkeypatch, lambda n: _result(f"p{n}"))

    result = parse_pdf_pages(source, tmp_path / "work", workers=0)

    assert result["state"] == "complete"
    assert calls == [1]
    assert result["pages"][1]["route"] == "blank"
    assert Path(result["output_path"]).read_text() == "==== page 1 ===\n\np1\n\n==== page 2 ===\n"


@pytest.mark.parametrize(
    ("result", "state"),
    [
        (_result("text"), "ok"),
        (_result("text", route="vlm"), "ok"),
        (_result("text", route="textract-rejected-kept-docling"), "ok"),
        (_result("text", route="textract-fallback-docling"), "degraded"),
        (_result("text", reason="budget_exhausted_no_escalation"), "degraded"),
        (_result("", route="vlm"), "failed"),
        ({"markdown": "", "page_routes": [], "warnings": []}, "failed"),
        (
            {
                "markdown": "",
                "page_routes": [],
                "warnings": [{"code": "docling_failed", "message": "x"}],
            },
            "failed",
        ),
    ],
)
def test_classify_page_result(result: dict[str, Any], state: str) -> None:
    assert classify_page_result(result)[0] == state


def test_docling_version_is_part_of_the_config_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Docling upgrade gets a new work dir instead of resuming onto old pages."""
    before = page_pipeline.config_hash()
    assert page_pipeline.pipeline_config()["docling_version"] == page_pipeline.DOCLING_VERSION
    monkeypatch.setattr(page_pipeline, "DOCLING_VERSION", "0.0.0-other")
    assert page_pipeline.config_hash() != before


def test_non_pdf_input_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="PDFs only"):
        parse_pdf_pages(FIXTURES / "doc.docx", tmp_path)


def test_real_pool_matches_whole_file_page_markdown(tmp_path: Path) -> None:
    """Real Docling in a 2-worker pool: every page gets a record, and each page's
    markdown equals the same page from a whole-file run. Escalation is off, so a
    page the gate promotes is ``degraded`` and the comparison uses the page files."""
    from parser_service.markdown_pipeline import (
        _document_converter,
        _elements_by_page,
        _render_page_markdown,
    )

    result = parse_pdf_pages(THREE_PAGE, tmp_path, workers=2, escalate=False)
    work_dir = Path(result["work_dir"])

    assert result["counts"]["pending"] == 0
    assert result["counts"]["failed"] == 0

    whole = _document_converter().convert(str(THREE_PAGE))
    by_page = _elements_by_page(whole.document, {"warnings": [], "elements": []})
    for index in range(3):
        page_md = (work_dir / "results" / f"{index + 1:04d}.md").read_text()
        assert page_md.strip() == _render_page_markdown(by_page.get(index, []))
