"""
test_jobs.py — inbox jobs (``parser_service.jobs``) and ``parse_pages.py --job``.

Run ONLY these tests:
    uv run pytest tests/test_jobs.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from test_page_pipeline import THREE_PAGE, _install_fake, _result
from test_page_pipeline_store import MemoryStore
from test_parse_pages_script import _main, script  # noqa: F401  (fixture)

from parser_service.jobs import (
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
        _job(docs=[{"doc_id": "a", "file": "a.docx"}]),
        _job(docs=[{"doc_id": "a", "file": "../a.pdf"}]),
        _job(docs=[{"doc_id": "a", "action": "archive"}]),
        _job(docs=[{"doc_id": "a", "file": "a.pdf", "metadata": []}]),
    ],
)
def test_parse_job_refuses_bad_jobs(body: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_job(json.dumps(body), "job.json")


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
