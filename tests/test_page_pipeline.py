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

from parser_service.formats import pdf
from parser_service.formats.pdf import classify_page_result
from parser_service.formats.result import Reply
from parser_service.pipeline import api, intake, layout, remote_runner, units
from parser_service.pipeline import config as config_module
from parser_service.pipeline.api import parse_pdf_pages
from parser_service.pipeline.config import strip_page_markers
from parser_service.pipeline.intake import split_pdf, work_dir_for
from parser_service.pipeline.runner import Request

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

    monkeypatch.setattr(pdf, "parse_to_markdown", fake)
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
    before = config_module.config_hash()
    assert config_module.pipeline_config()["docling_version"] == config_module.DOCLING_VERSION
    monkeypatch.setattr(config_module, "DOCLING_VERSION", "0.0.0-other")
    assert config_module.config_hash() != before


def test_non_pdf_input_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A DOCX is routed to docx; with docx switched off it fails alone at intake
    (spec D1; before Group 3a it raised "PDFs only") and writes no work."""
    monkeypatch.setenv("PARSER_ENABLED_KINDS", "pdf")
    result = parse_pdf_pages(FIXTURES / "doc.docx", tmp_path)
    assert (result["state"], result["reason_code"], result["kind"]) == (
        "failed",
        "unsupported_type",
        "docx",
    )
    assert not (tmp_path / "work").exists()


def test_real_pool_matches_whole_file_page_markdown(tmp_path: Path) -> None:
    """Real Docling in a 2-worker pool: every page gets a record, and each page's
    markdown equals the same page from a whole-file run. Escalation is off, so a
    page the gate promotes is ``degraded`` and the comparison uses the page files."""
    from parser_service.parsing.document import (
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


# ---------------------------------------------------------------------------
# Reason codes and the final-record rule (D5)
# ---------------------------------------------------------------------------


def _record_path(tmp_path: Path, page_number: int) -> Path:
    return work_dir_for(THREE_PAGE, tmp_path) / "results" / f"{page_number:04d}.json"


def _attempts(tmp_path: Path, page_number: int) -> str:
    return (work_dir_for(THREE_PAGE, tmp_path) / "attempts" / f"{page_number:04d}").read_text()


def _store_timeout_record(tmp_path: Path, page_number: int, deadline: int) -> dict[str, Any]:
    """Split the fixture and store a ``timeout`` record for one page, as the service would."""
    from parser_service.storage.store import LocalStore

    work_dir = work_dir_for(THREE_PAGE, tmp_path)
    work_dir.mkdir(parents=True)
    split_pdf(THREE_PAGE, work_dir)
    store = LocalStore(tmp_path)
    work_id = intake.work_id_for(THREE_PAGE)
    layout._put_text(store, layout._attempts_key(work_id, page_number), "1")
    return units._write_record(
        store,
        work_id,
        page_number,
        "failed",
        f"timeout: no result after {deadline} s (pdf)",
        "",
        1,
        reason_code="timeout",
        limits={"deadline_s": deadline},
    )


def test_timeout_is_final_on_rerun(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PARSER_DEADLINE_PDF_PAGE_S", "600")
    stored = _store_timeout_record(tmp_path, 2, 600)
    calls = _install_fake(monkeypatch, lambda n: _result(f"p{n}"))

    for _ in range(2):
        result = parse_pdf_pages(THREE_PAGE, tmp_path, workers=0, max_attempts=3)

    assert calls == [1, 3]  # page 2 never runs again
    assert json.loads(_record_path(tmp_path, 2).read_text()) == stored
    assert not list(_record_path(tmp_path, 2).parent.glob("0002.attempt-*.json"))  # no archive
    assert _attempts(tmp_path, 2) == "1"
    page = result["not_ok_pages"][0]
    assert (page["page"], page["reason_code"], page["attempts"], page["retryable"]) == (
        2,
        "timeout",
        1,
        False,
    )


def test_timeout_reruns_when_the_deadline_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store_timeout_record(tmp_path, 2, 600)
    monkeypatch.setenv("PARSER_DEADLINE_PDF_PAGE_S", "900")
    calls = _install_fake(monkeypatch, lambda n: _result(f"p{n}"))

    result = parse_pdf_pages(THREE_PAGE, tmp_path, workers=0, max_attempts=3)

    assert sorted(calls) == [1, 2, 3]
    assert result["state"] == "complete"
    archive = _record_path(tmp_path, 2).parent / "0002.attempt-1.json"
    assert json.loads(archive.read_text())["reason_code"] == "timeout"
    record = json.loads(_record_path(tmp_path, 2).read_text())
    assert (record["state"], record["attempt"]) == ("ok", 2)  # the same counter, not reset
    assert _attempts(tmp_path, 2) == "2"


def test_crash_is_still_retried_under_the_attempt_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def by_page(n: int) -> dict[str, Any]:
        if n == 2:
            raise RuntimeError("boom")
        return _result(f"p{n}")

    calls = _install_fake(monkeypatch, by_page)

    first = parse_pdf_pages(THREE_PAGE, tmp_path, workers=0, max_attempts=2)
    page = first["not_ok_pages"][0]
    assert (page["reason_code"], page["attempts"], page["retryable"]) == ("exception", 1, True)

    second = parse_pdf_pages(THREE_PAGE, tmp_path, workers=0, max_attempts=2)
    assert calls.count(2) == 2  # archived and run again
    assert (_record_path(tmp_path, 2).parent / "0002.attempt-1.json").exists()
    page = second["not_ok_pages"][0]
    assert (page["reason_code"], page["attempts"], page["retryable"]) == ("exception", 2, False)

    parse_pdf_pages(THREE_PAGE, tmp_path, workers=0, max_attempts=2)
    assert calls.count(2) == 2  # at the limit: not run again

    # A page that killed its worker at the limit is finalized as today.
    _record_path(tmp_path, 3).unlink()
    (work_dir_for(THREE_PAGE, tmp_path) / "attempts" / "0003").write_text("2")
    third = parse_pdf_pages(THREE_PAGE, tmp_path, workers=0, max_attempts=2)
    page = next(p for p in third["not_ok_pages"] if p["page"] == 3)
    assert page["reason_code"] == "no_result_after_2_attempts"
    assert page["reason"].startswith("no_result_after_2_attempts: ")
    assert page["retryable"] is False


def test_new_records_carry_reason_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def by_page(n: int) -> dict[str, Any]:
        if n == 1:
            raise ValueError("bad page: really")
        if n == 2:
            return _result("", route="vlm")  # empty output
        return _result("p3")

    _install_fake(monkeypatch, by_page)
    parse_pdf_pages(THREE_PAGE, tmp_path, workers=0)

    records = [json.loads(_record_path(tmp_path, n).read_text()) for n in (1, 2, 3)]
    assert records[0]["reason"] == "exception: ValueError: bad page: really"
    assert records[0]["reason_code"] == "exception"
    assert records[1]["reason"] == "empty_output: route=vlm"
    assert records[1]["reason_code"] == "empty_output"
    assert (records[2]["state"], records[2]["reason"], records[2]["reason_code"]) == (
        "ok",
        None,
        None,
    )
    assert not any("limits" in r for r in records)

    # A record written before reason codes existed keeps today's behaviour: even
    # with a "timeout" reason and limits matching, it is retried, not final.
    old = dict(
        records[0], reason="timeout: no result after 600 s (pdf)", limits={"deadline_s": 600}
    )
    del old["reason_code"]
    _record_path(tmp_path, 1).write_text(json.dumps(old))
    assert not config_module._is_final(old, config_module.current_limits())
    calls = _install_fake(monkeypatch, lambda n: _result(f"p{n}"))
    result = parse_pdf_pages(THREE_PAGE, tmp_path, workers=0)
    assert sorted(calls) == [1, 2]
    assert result["state"] == "complete"


def test_deadline_is_not_in_the_config_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    before_config, before_hash = config_module.pipeline_config(), config_module.config_hash()

    monkeypatch.setenv("PARSER_DEADLINE_PDF_PAGE_S", "5")
    monkeypatch.setenv("PARSER_REMOTE_REQUEST_TIMEOUT_S", "125")
    monkeypatch.setenv("PARSER_WORKER_START_TIMEOUT_S", "7")

    assert config_module.deadline_for("pdf") == 5
    assert config_module.current_limits() == {"deadline_s": 5}
    assert config_module.pipeline_config() == before_config
    assert config_module.config_hash() == before_hash
    assert not {"deadline_s", "deadline_pdf_page_s", "request_timeout"} & set(before_config)


# ---------------------------------------------------------------------------
# Kinds, intake, store layout, per-kind config and routing (Group 3a)
# ---------------------------------------------------------------------------

DOCX = FIXTURES / "doc.docx"
XLSX = FIXTURES / "sheet.xlsx"


def _enable_test_kind(
    monkeypatch: pytest.MonkeyPatch, kind: str, enabled: str = "pdf,{kind}"
) -> list[tuple[str, int]]:
    """Enable ``kind`` with an in-process test handler and a deadline; return its call log.

    Until Groups 4-6 land, no non-PDF kind has a handler in the image, so routing is
    exercised with this one (the local executor parses in this process).
    """
    from parser_service import settings
    from parser_service.formats import handlers as unit_handlers

    calls: list[tuple[str, int]] = []

    def handler(path: Path, page_number: int, escalate: bool) -> Any:
        calls.append((Path(path).name, page_number))
        return "ok", None, None, f"text of the {kind} file", {"route": "test-handler"}

    monkeypatch.setitem(unit_handlers.HANDLERS, kind, handler)
    monkeypatch.setitem(settings.DEADLINE_ENV, kind, (f"TEST_DEADLINE_{kind.upper()}_S", 60))
    monkeypatch.setenv("PARSER_ENABLED_KINDS", enabled.format(kind=kind))
    return calls


def _work_keys(root: Path) -> list[str]:
    work = root / "work"
    return (
        sorted(str(p.relative_to(root)) for p in work.rglob("*") if p.is_file())
        if work.exists()
        else []
    )


def test_config_hash_changes_with_handler_version(monkeypatch: pytest.MonkeyPatch) -> None:
    from parser_service.formats import handlers as unit_handlers
    from parser_service.formats.sniff import KINDS

    configs = {kind: config_module.pipeline_config(kind) for kind in KINDS}
    hashes = {kind: config_module.config_hash(config) for kind, config in configs.items()}
    assert len(set(hashes.values())) == len(KINDS)  # every kind has its own hash
    assert config_module.config_hash() == hashes["pdf"]  # the default is the PDF's
    escalation = {"escalation_engine", "vlm_model", "scan_fastpath", "arbitration"}
    assert escalation <= set(configs["pdf"])
    # An image can escalate: it keeps every escalation setting but the PDF scan fast-path.
    image_escalation = escalation - {"scan_fastpath"}
    for kind, config in configs.items():
        assert (config["kind"], config["handler_version"]) == (kind, "1")
        if kind in ("png", "jpeg"):
            assert escalation & set(config) == image_escalation
        elif kind != "pdf":
            assert not escalation & set(config)
    assert "docling_version" not in configs["txt"] and "docling_version" in configs["docx"]

    # A VLM model change re-parses PDFs and images only.
    monkeypatch.setenv("BEDROCK_VLM_MODEL", "another-model")
    changed = {
        kind
        for kind in KINDS
        if config_module.config_hash(config_module.pipeline_config(kind)) != hashes[kind]
    }
    assert changed == {"pdf", "png", "jpeg"}
    monkeypatch.delenv("BEDROCK_VLM_MODEL")

    # A handler bump changes its own kinds' hashes and no other (email: eml and msg;
    # image: png and jpeg).
    for handler, bumped in (
        ("docx", {"docx"}),
        ("email", {"eml", "msg"}),
        ("image", {"png", "jpeg"}),
        ("pdf", {"pdf"}),
    ):
        with monkeypatch.context() as patch:
            patch.setitem(unit_handlers.HANDLER_VERSIONS, handler, "2")
            changed = {
                kind
                for kind in KINDS
                if config_module.config_hash(config_module.pipeline_config(kind)) != hashes[kind]
            }
        assert changed == bumped, handler


def test_disabled_kind_fails_with_unsupported_type_and_writes_no_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from parser_service import settings

    monkeypatch.setenv("PARSER_ENABLED_KINDS", "pdf")  # docx switched off for this test
    calls = _install_fake(monkeypatch, lambda n: _result(f"p{n}"))
    big_txt = tmp_path / "in" / "notes.txt"
    big_txt.parent.mkdir()
    big_txt.write_bytes(b"plain text line\n" * 70_000)  # 1.12 MB
    cases = [
        (DOCX, "unsupported_type", "docx", "docx"),  # a kind, but switched off
        (XLSX, "unsupported_type", None, "xlsx"),  # no kind at all
    ]
    for source, code, kind, detected in cases:
        result = parse_pdf_pages(source, tmp_path / "store")
        assert (result["state"], result["reason_code"]) == ("failed", code)
        assert (result["kind"], result["detected_type"], result["work_id"]) == (
            kind,
            detected,
            None,
        )
        status = json.loads(Path(result["status_path"]).read_text())
        assert (status["state"], status["work_id"], status["page_count"]) == ("failed", None, 1)
        [unit] = status["pages"]
        assert (unit["reason_code"], unit["attempts"], unit["retryable"]) == (code, 0, False)

    # An enabled single-unit kind over its raw cap (MB) fails with too_large.
    _enable_test_kind(monkeypatch, "txt")
    monkeypatch.setitem(settings.RAW_CAP_ENV, "txt", ("TEST_MAX_TXT_MB", 1))
    result = parse_pdf_pages(big_txt, tmp_path / "store")
    assert (result["state"], result["reason_code"], result["kind"]) == (
        "failed",
        "too_large",
        "txt",
    )

    assert calls == [] and _work_keys(tmp_path / "store") == []


def test_reenabled_kind_parses_without_stale_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    monkeypatch.setenv("PARSER_ENABLED_KINDS", "pdf")  # docx switched off first
    first = parse_pdf_pages(DOCX, store_root)
    assert (first["state"], first["reason_code"]) == ("failed", "unsupported_type")

    calls = _enable_test_kind(monkeypatch, "docx")
    second = parse_pdf_pages(DOCX, store_root, workers=0)

    assert second["state"] == "complete" and second["file_id"] == first["file_id"]
    assert calls == [("source.docx", 1)]
    [unit] = second["pages"]
    assert (unit["state"], unit["attempts"]) == ("ok", 1)
    status = json.loads(Path(second["status_path"]).read_text())
    assert status["state"] == "complete" and status["work_id"] == second["work_id"]


def test_routing_uses_manifest_kind_not_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DOCX named .pdf is stored as source.docx with manifest kind docx, and both
    executors are asked for docx (spec D1, D2)."""
    from parser_service.storage.store import LocalStore

    calls = _enable_test_kind(monkeypatch, "docx")
    renamed = tmp_path / "in" / "report.pdf"
    renamed.parent.mkdir()
    renamed.write_bytes(DOCX.read_bytes())

    result = parse_pdf_pages(renamed, tmp_path / "store", workers=0)

    assert (result["state"], result["kind"], result["unit"]) == ("complete", "docx", "document")
    work_id = result["work_id"]
    assert work_id == intake.work_id_for(renamed)
    assert work_id.endswith(config_module.config_hash(config_module.pipeline_config("docx")))
    store = LocalStore(tmp_path / "store")
    manifest = layout._read_json(store, layout._manifest_key(work_id))
    assert (manifest["kind"], manifest["unit"], manifest["page_count"]) == ("docx", "document", 1)
    assert manifest["config"] == config_module.pipeline_config("docx")
    assert _work_keys(tmp_path / "store") == sorted(
        f"work/{work_id}/{name}"
        for name in (
            "source.docx",  # no source.pdf, no pages/
            "manifest.json",
            "attempts/0001",
            "results/0001.json",
            "results/0001.md",
        )
    )
    assert calls == [("source.docx", 1)]  # the local executor parsed source.docx as docx

    # The page service routes on the manifest's kind too.
    requests: list[Request] = []

    class Recorder:
        pid = 4242

        def run(self, request: Request, deadline_s: float) -> Reply:
            requests.append(request)
            return "ok", None, None, "worker text", {}

    other = LocalStore(tmp_path / "service")
    intake._intake(other, work_id, renamed, "docx")
    summary = remote_runner.process_page_request(other, work_id, 1, False, 3, runner=Recorder())
    assert (summary["state"], summary["kind"]) == ("ok", "docx")
    [(kind, unit_path, page, _escalate, _cap)] = requests
    assert (kind, Path(unit_path).name, page) == ("docx", "source.docx", 1)


def test_binary_kind_mismatch_is_counted_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A PDF named .docx still routes to pdf on its content; the mismatch is logged."""
    calls = _install_fake(monkeypatch, lambda n: _result(f"p{n}"))
    renamed = tmp_path / "in" / "report.docx"
    renamed.parent.mkdir()
    renamed.write_bytes(THREE_PAGE.read_bytes())

    with caplog.at_level("WARNING", logger="parser_service.page_pipeline"):
        result = parse_pdf_pages(renamed, tmp_path / "store", workers=0)

    assert (result["state"], result["kind"], result["detected_type"]) == ("complete", "pdf", "pdf")
    assert result["work_id"] == intake.work_id_for(THREE_PAGE)  # same bytes, same work
    assert sorted(calls) == [1, 2, 3]
    assert any("disagrees with the content" in r.getMessage() for r in caplog.records)


def test_manifest_without_kind_resumes_as_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest written before kinds existed reads as pdf/page and resumes."""
    from parser_service.storage.store import LocalStore

    work_dir = work_dir_for(THREE_PAGE, tmp_path)
    manifest = split_pdf(THREE_PAGE, work_dir)
    assert (manifest["kind"], manifest["unit"]) == ("pdf", "page")
    old = {k: v for k, v in manifest.items() if k not in ("kind", "unit")}
    (work_dir / "manifest.json").write_text(json.dumps(old))
    assert config_module.manifest_kind(old) == "pdf"
    work_id = intake.work_id_for(THREE_PAGE)
    assert layout._unit_key(work_id, old, 2) == f"work/{work_id}/pages/0002.pdf"

    calls = _install_fake(monkeypatch, lambda n: _result(f"p{n}"))
    store = LocalStore(tmp_path)
    summary = remote_runner.process_page_request(store, work_id, 1, False, 3)
    assert (summary["state"], summary["kind"]) == ("ok", "pdf")
    result = parse_pdf_pages(THREE_PAGE, tmp_path, workers=0)

    assert result["state"] == "complete" and sorted(calls) == [1, 2, 3]
    assert json.loads((work_dir / "manifest.json").read_text()) == old  # not rewritten


# ---------------------------------------------------------------------------
# Status and the publish contract (Group 3b)
# ---------------------------------------------------------------------------


def test_intake_rejection_writes_status_and_one_failed_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """status.json only (work_id null, one unit, 0 attempts, not retryable); one
    ``failed`` event, not duplicated by a re-run; the driver exits 2."""
    from test_scripts_rewiring import _load_script

    from parser_service.delivery.publish import PublishRequest, version_for

    monkeypatch.setenv("PARSER_ENABLED_KINDS", "pdf")  # the other kinds switched off
    root = tmp_path / "store"

    def events() -> list[Path]:
        return sorted((root / "published/v1/events").rglob("*.json"))

    result = api.parse_file_in_store(
        XLSX, str(root), workers=0, publish=PublishRequest("sheet", "job-1")
    )

    assert (result["state"], result["reason_code"], result["work_id"]) == (
        "failed",
        "unsupported_type",
        None,
    )
    status = json.loads(Path(result["status_path"]).read_text())
    assert (status["state"], status["work_id"], status["page_count"], status["unit"]) == (
        "failed",
        None,
        1,
        "document",
    )
    assert (status["kind"], status["detected_type"], status["flags"]) == (None, "xlsx", [])
    [unit] = status["pages"]
    assert (unit["attempts"], unit["retryable"], unit["reason_code"]) == (
        0,
        False,
        "unsupported_type",
    )
    assert _work_keys(root) == [] and not (root / "published/v1/docs").exists()

    [event_path] = events()
    event = json.loads(event_path.read_text())
    sha256 = status["source_sha256"]
    intake_hash = config_module.config_hash({"intake": "unsupported_type", "kind": None})
    assert event["version"] == version_for(sha256, intake_hash, "failed", 0)
    assert result["published"]["version"] == event["version"]
    assert (event["type"], event["doc_id"], event["job_id"]) == ("failed", "sheet", "job-1")
    assert (event["kind"], event["unit"], event["flags"], event["handler_version"]) == (
        None,
        "document",
        [],
        None,
    )
    assert (event["page_count"], event["coverage"], event["missing_pages"]) == (1, 0.0, [1])
    [not_ok] = event["not_ok_pages"]
    assert (not_ok["page"], not_ok["reason_code"]) == (1, "unsupported_type")

    # A re-run (here through the driver, which exits 2: final) adds no event.
    before = event_path.read_bytes()
    script = _load_script("parse_pages")
    monkeypatch.setattr(
        "sys.argv",
        [
            "parse_pages.py",
            "--input",
            str(XLSX),
            "--work-dir",
            str(root),
            "--doc-id",
            "sheet",
            "--workers",
            "0",
            "--no-escalate",
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        script.main()
    assert exit_info.value.code == 2
    assert events() == [event_path] and event_path.read_bytes() == before
    again = api.parse_file_in_store(
        XLSX, str(root), workers=0, publish=PublishRequest("sheet", "job-1")
    )
    assert again["published"] == result["published"]
    assert event_path.read_bytes() == before
    assert _work_keys(root) == []
