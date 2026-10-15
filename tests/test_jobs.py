"""
test_jobs.py — inbox jobs (``parser_service.delivery.jobs``) and ``parse_pages.py --job``.

Run ONLY these tests:
    uv run pytest tests/test_jobs.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from test_page_pipeline import THREE_PAGE, XLSX, _install_fake, _result
from test_page_pipeline_store import MemoryStore
from test_parse_pages_script import _main, script  # noqa: F401  (fixture)

from parser_service.delivery.jobs import (
    JobDoc,
    job_status_key,
    parse_job,
    rebuild_job_status,
    record_doc,
    resolve_file,
)


def _job(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "job_id": "audit-1",
        "docs": [
            {"doc_id": "report", "file": "report.pdf", "metadata": {"title": "Report"}},
            {"doc_id": "old-policy", "action": "delete"},
        ],
    }
    body.update(overrides)
    return body


def test_parse_job_reads_docs_and_actions() -> None:
    job = parse_job(json.dumps(_job()), "s3://b/inbox/sys/audit-1/job.json")
    assert job.job_id == "audit-1"
    assert job.docs[0] == JobDoc("report", "upsert", "report.pdf", {"title": "Report"})
    assert job.docs[1] == JobDoc("old-policy", "delete", None, {})
    assert resolve_file(job, job.docs[0]) == "s3://b/inbox/sys/audit-1/report.pdf"
    local = parse_job(json.dumps(_job()), "/data/inbox/job.json")
    assert resolve_file(local, local.docs[0]) == "/data/inbox/report.pdf"


@pytest.mark.parametrize(
    "body",
    [
        _job(job_id="Bad Id"),
        _job(docs=[]),
        _job(docs=[{"doc_id": "a", "file": "a.pdf"}, {"doc_id": "a", "file": "b.pdf"}]),
        _job(docs=[{"doc_id": "a", "file": ""}]),
        _job(docs=[{"doc_id": "a"}]),
        _job(docs=[{"doc_id": "a", "file": "../a.pdf"}]),
        _job(docs=[{"doc_id": "a", "action": "archive"}]),
        _job(docs=[{"doc_id": "a", "file": "a.pdf", "metadata": []}]),
    ],
)
def test_parse_job_refuses_bad_jobs(body: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_job(json.dumps(body), "job.json")


@pytest.mark.parametrize(
    "file", ["a.docx", "a.xlsx", "notes.TXT", "mail.eml", "no-extension", "sub/dir/a.html"]
)
def test_parse_job_accepts_any_extension(file: str) -> None:
    """The content decides the kind at intake; a file nothing handles fails alone."""
    job = parse_job(json.dumps(_job(docs=[{"doc_id": "a", "file": file}])), "job.json")
    assert job.docs[0].file == file
    for bad in ("", "   ", None, 7, f"../{file}", f"x/../{file}"):
        with pytest.raises(ValueError):
            parse_job(json.dumps(_job(docs=[{"doc_id": "a", "file": bad}])), "job.json")


def test_job_coverage_waits_for_every_page_count() -> None:
    store = MemoryStore()
    job = parse_job(
        json.dumps(_job(docs=[{"doc_id": "a", "file": "a.pdf"}, {"doc_id": "b", "file": "b.pdf"}])),
        "job.json",
    )
    record_doc(store, job, job.docs[0], {"state": "complete", "page_count": 10, "ok_pages": 10})

    status = rebuild_job_status(store, job)
    assert status["state"] == "in_progress"
    assert status["pages_total"] is None and status["coverage"] is None  # b not split yet

    record_doc(store, job, job.docs[1], {"state": "failed", "page_count": 30, "ok_pages": 27})
    status = rebuild_job_status(store, job)
    assert status["state"] == "failed"
    assert (status["pages_total"], status["pages_ok"], status["coverage"]) == (40, 37, 0.925)
    assert json.loads(store.read(job_status_key("audit-1"))) == status


def test_cli_runs_a_job_document_by_document(
    script: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path  # noqa: F811
) -> None:
    _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    inbox = tmp_path / "inbox" / "sys" / "audit-1"
    inbox.mkdir(parents=True)
    shutil.copy(THREE_PAGE, inbox / "report.pdf")
    (inbox / "job.json").write_text(json.dumps(_job()))
    location = tmp_path / "store"
    args = ("--job", str(inbox / "job.json"), "--work-dir", str(location), "--workers", "0", "--no-escalate")

    assert _main(script, monkeypatch, *args, "--index", "0") == 0
    status = json.loads((location / job_status_key("audit-1")).read_text())
    assert status["state"] == "in_progress"  # old-policy has not run yet

    assert _main(script, monkeypatch, *args, "--index", "1") == 0
    status = json.loads((location / job_status_key("audit-1")).read_text())
    assert status["state"] == "complete" and status["coverage"] == 1.0
    report, deleted = status["docs"]
    assert report["state"] == "complete" and report["published"] == "published"
    assert deleted["state"] == "deleted"

    version = report["version"]
    document = json.loads(
        (location / f"published/v1/docs/report/{version}/document.json").read_text()
    )
    assert document["job_id"] == "audit-1" and document["metadata"] == {"title": "Report"}
    events = sorted(p.name for p in (location / "published/v1/events").rglob("*.json"))
    assert events == ["old-policy--audit-1--deleted.json", f"report--{version}--published.json"]

    assert _main(script, monkeypatch, *args, "--index", "2") == 1  # outside the job


def test_cli_publishes_a_single_file_under_its_slugified_name(
    script: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path  # noqa: F811
) -> None:
    _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    source = tmp_path / "Annual Report 2025.pdf"
    shutil.copy(THREE_PAGE, source)
    location = tmp_path / "store"
    args = ("--input", str(source), "--work-dir", str(location), "--workers", "0", "--no-escalate")

    assert _main(script, monkeypatch, *args, "--publish") == 0
    manifests = list((location / "published/v1/docs").rglob("_manifest.json"))
    assert [m.parents[1].name for m in manifests] == ["annual-report-2025"]
    assert not (location / "ops").exists()  # no job, no job status

    assert _main(script, monkeypatch, *args, "--doc-id", "Not Valid") == 1


# ---------------------------------------------------------------------------
# Pages and documents (Group 3b)
# ---------------------------------------------------------------------------


def test_job_status_counts_pages_and_documents() -> None:
    """Coverage is (pages_ok + documents_ok) / (pages_total + documents_total); a doc
    with no unit is a page doc; coverage waits until every doc has a unit and count."""
    store = MemoryStore()
    ids = ("old-pdf", "pdf", "memo", "notes", "sheet")
    job = parse_job(
        json.dumps(_job(docs=[{"doc_id": i, "file": f"{i}.bin"} for i in ids])), "job.json"
    )
    outcomes = {
        # written before units existed: a page doc
        "old-pdf": {"state": "complete", "page_count": 4, "ok_pages": 4},
        "pdf": {"state": "failed", "unit": "page", "kind": "pdf", "page_count": 6, "ok_pages": 5},
        "memo": {
            "state": "complete",
            "unit": "document",
            "kind": "docx",
            "page_count": 1,
            "ok_pages": 1,
        },
        "notes": {
            "state": "failed",
            "unit": "document",
            "kind": "txt",
            "page_count": 1,
            "ok_pages": 0,
        },
    }
    for doc in job.docs[:4]:
        record_doc(store, job, doc, outcomes[doc.doc_id])

    status = rebuild_job_status(store, job)  # sheet has no outcome yet
    assert (status["pages_total"], status["documents_total"], status["coverage"]) == (
        None,
        None,
        None,
    )
    assert (status["pages_ok"], status["documents_ok"]) == (9, 1)

    record_doc(
        store,
        job,
        job.docs[4],
        {
            "state": "failed",
            "unit": "document",
            "kind": None,
            "detected_type": "xlsx",
            "flags": [],
            "page_count": 1,
            "ok_pages": 0,
        },
    )
    status = rebuild_job_status(store, job)
    assert (status["pages_total"], status["pages_ok"]) == (10, 9)
    assert (status["documents_total"], status["documents_ok"]) == (3, 1)
    assert status["coverage"] == round((9 + 1) / (10 + 3), 4)
    assert status["docs"][4]["detected_type"] == "xlsx"

    # An outcome with no count (a driver error) keeps the totals unknown, as before.
    record_doc(store, job, job.docs[3], {"state": "error", "error": "boom"})
    status = rebuild_job_status(store, job)
    assert (status["pages_total"], status["documents_total"], status["coverage"]) == (
        None,
        None,
        None,
    )


def test_job_with_one_unsupported_file_parses_the_rest(
    script: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A PDF and an XLSX: the PDF completes, the XLSX fails alone with unsupported_type
    (exit 2), and the job reports coverage 1/2 units."""
    from pypdf import PdfReader, PdfWriter

    monkeypatch.delenv("PARSER_ENABLED_KINDS", raising=False)  # the default: every handled kind
    calls = _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    inbox = tmp_path / "inbox" / "sys" / "audit-1"
    inbox.mkdir(parents=True)
    writer = PdfWriter()
    writer.add_page(PdfReader(THREE_PAGE).pages[0])
    with (inbox / "one-page.pdf").open("wb") as handle:
        writer.write(handle)
    shutil.copy(XLSX, inbox / "sheet.xlsx")
    docs = [{"doc_id": "report", "file": "one-page.pdf"}, {"doc_id": "sheet", "file": "sheet.xlsx"}]
    (inbox / "job.json").write_text(json.dumps(_job(docs=docs)))
    location = tmp_path / "store"
    args = (
        "--job",
        str(inbox / "job.json"),
        "--work-dir",
        str(location),
        "--workers",
        "0",
        "--no-escalate",
    )

    assert _main(script, monkeypatch, *args, "--index", "1") == 2  # the XLSX fails, alone
    assert _main(script, monkeypatch, *args, "--index", "0") == 0  # the PDF still runs
    assert calls == [1]

    status = json.loads((location / job_status_key("audit-1")).read_text())
    assert status["state"] == "failed"  # finished, not every document complete
    assert status["docs_by_state"] == {"complete": 1, "failed": 1}
    assert (status["pages_total"], status["pages_ok"]) == (1, 1)
    assert (status["documents_total"], status["documents_ok"]) == (1, 0)
    assert status["coverage"] == 0.5
    report, sheet = status["docs"]
    assert (report["kind"], report["unit"], report["detected_type"], report["flags"]) == (
        "pdf",
        "page",
        "pdf",
        [],
    )
    assert (sheet["kind"], sheet["unit"], sheet["detected_type"], sheet["flags"]) == (
        None,
        "document",
        "xlsx",
        [],
    )
    assert sheet["work_id"] is None and sheet["published"] == "failed"
    [unit] = sheet["not_ok_pages"]
    assert unit["reason_code"] == "unsupported_type"
    assert [p.name for p in (location / "work").rglob("source.*")] == ["source.pdf"]  # no XLSX work
