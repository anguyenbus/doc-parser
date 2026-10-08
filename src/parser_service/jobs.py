"""
jobs.py — a bundle of documents dropped in the inbox, and its status for people.

A producer uploads a bundle and writes ``job.json`` LAST, as the trigger:

    inbox/<source_system>/<job_id>/
        <any>.pdf …
        job.json

    {
      "job_id": "audit-2026-10-08",
      "docs": [
        {"doc_id": "annual-report-2025", "file": "annual-report.pdf",
         "metadata": {"title": "Annual report 2025"}},
        {"doc_id": "old-policy", "action": "delete"}
      ]
    }

``file`` is relative to the directory of ``job.json`` (or a full path or s3://
URI). ``action`` is ``upsert`` (the default: parse and publish) or ``delete``
(publish a ``deleted`` event; no file). ``metadata`` is passed through to the
published ``document.json`` untouched.

One driver runs one document (a Kubernetes Indexed Job: index = position in
``docs``) and records its outcome; the job summary is rebuilt from those:

    ops/jobs/<job_id>/docs/<doc_id>.json    one per document, written by its driver
    ops/jobs/<job_id>/status.json           the job summary, rebuilt after each document

Each document's file has a single writer, so drivers never overwrite each other;
the summary is derived and any driver (or ``rebuild_job_status``) can rebuild it.
The job's coverage is counted against the true page total: it stays None until
every document has been split and its page count is known.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from parser_service.publish import check_id
from parser_service.store import Store, StoreNotFoundError

ACTION_UPSERT = "upsert"
ACTION_DELETE = "delete"
DOC_DELETED = "deleted"
DOC_PENDING = "pending"


@dataclass(frozen=True)
class JobDoc:
    doc_id: str
    action: str = ACTION_UPSERT
    file: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Job:
    job_id: str
    docs: tuple[JobDoc, ...]
    location: str  # where job.json was read from: a path or s3:// URI


def parse_job(text: str, location: str) -> Job:
    """Validate a ``job.json`` body; ``ValueError`` names the first problem."""
    try:
        body = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{location} is not valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError(f"{location} must hold a JSON object")
    job_id = check_id(body.get("job_id"), "job_id")
    raw_docs = body.get("docs")
    if not isinstance(raw_docs, list) or not raw_docs:
        raise ValueError(f"{location}: docs must be a non-empty list")
    docs: list[JobDoc] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_docs):
        where = f"{location}: docs[{i}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{where} must be an object")
        doc_id = check_id(raw.get("doc_id"), f"{where}.doc_id")
        if doc_id in seen:
            raise ValueError(f"{where}: doc_id {doc_id!r} appears twice")
        seen.add(doc_id)
        action = raw.get("action", ACTION_UPSERT)
        if action not in (ACTION_UPSERT, ACTION_DELETE):
            raise ValueError(f"{where}.action must be {ACTION_UPSERT} or {ACTION_DELETE}, got {action!r}")
        file = raw.get("file")
        if action == ACTION_UPSERT:
            if not isinstance(file, str) or not file.lower().endswith(".pdf"):
                raise ValueError(f"{where}.file must name a PDF, got {file!r}")
            if ".." in file.split("/"):
                raise ValueError(f"{where}.file must not contain '..', got {file!r}")
        metadata = raw.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError(f"{where}.metadata must be an object")
        docs.append(JobDoc(doc_id, action, file if action == ACTION_UPSERT else None, metadata))
    return Job(job_id, tuple(docs), location)


def resolve_file(job: Job, doc: JobDoc) -> str:
    """The document's file as a path or s3:// URI; a relative name is next to job.json."""
    assert doc.file is not None
    if "://" in doc.file or doc.file.startswith("/"):
        return doc.file
    if job.location.startswith("s3://"):
        return f"{job.location.rpartition('/')[0]}/{doc.file}"
    return str(Path(job.location).parent / doc.file)


def _doc_key(job_id: str, doc_id: str) -> str:
    return f"ops/jobs/{job_id}/docs/{doc_id}.json"


def job_status_key(job_id: str) -> str:
    return f"ops/jobs/{job_id}/status.json"


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def record_doc(store: Store, job: Job, doc: JobDoc, outcome: dict[str, Any]) -> None:
    """Store one document's latest outcome (``state`` and anything else worth showing)."""
    body = {"doc_id": doc.doc_id, "action": doc.action, **outcome}
    body["updated_at"] = datetime.now(tz=UTC).isoformat()
    store.write(_doc_key(job.job_id, doc.doc_id), _json_bytes(body))


def rebuild_job_status(store: Store, job: Job) -> dict[str, Any]:
    """Rebuild and store ``ops/jobs/<job_id>/status.json`` from the documents' outcomes.

    ``state``: ``complete`` when every document is complete (or deleted);
    ``in_progress`` while any has no outcome yet or is in progress; else
    ``failed`` (finished, but not every document complete; see ``docs``).
    """
    docs: list[dict[str, Any]] = []
    for doc in job.docs:
        try:
            outcome = json.loads(store.read(_doc_key(job.job_id, doc.doc_id)))
        except (StoreNotFoundError, ValueError):
            outcome = {"doc_id": doc.doc_id, "action": doc.action, "state": DOC_PENDING}
        docs.append(outcome)

    states = [d.get("state") for d in docs]
    by_state: dict[str, int] = {}
    for state in states:
        by_state[str(state)] = by_state.get(str(state), 0) + 1
    if all(s in ("complete", DOC_DELETED) for s in states):
        job_state = "complete"
    elif any(s in (DOC_PENDING, "in_progress") for s in states):
        job_state = "in_progress"
    else:
        job_state = "failed"

    parsed = [d for d in docs if d.get("action") == ACTION_UPSERT]
    known = [d for d in parsed if isinstance(d.get("page_count"), int)]
    pages_total = sum(d["page_count"] for d in known)
    pages_ok = sum(int(d.get("ok_pages", 0)) for d in known)
    all_known = len(known) == len(parsed)
    status = {
        "job_id": job.job_id,
        "job": job.location,
        "state": job_state,
        "docs_total": len(docs),
        "docs_by_state": by_state,
        "pages_total": pages_total if all_known else None,
        "pages_ok": pages_ok,
        "coverage": round(pages_ok / pages_total, 4) if all_known and pages_total else None,
        "docs": docs,
        "updated_at": datetime.now(tz=UTC).isoformat(),
    }
    store.write(job_status_key(job.job_id), _json_bytes(status))
    return status
