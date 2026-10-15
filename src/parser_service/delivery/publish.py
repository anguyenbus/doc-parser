"""
publish.py — the versioned hand-off of a parsed document to its consumers.

``work/`` and ``out/`` are the pipeline's own; ``published/`` is the contract a
consumer (a RAG ingestion job) builds against:

    published/v1/docs/<doc_id>/<version>/
        pages/0001.md …      one file per ok page: that page's markdown, nothing else
        document.md          the whole document, a ``==== page N ===`` line before each page
        document.json        ids, coverage, config, and a per-page map with char spans
                             (plus ``email``, the headers and attachment list, for eml/msg)
        _manifest.json       written LAST: every file above with its sha256 and size
    published/v1/events/dt=<YYYY-MM-DD>/<doc_id>--<version>--<type>.json

The rules that make it safe to consume:

- ``_manifest.json`` is the commit marker. It is written after every file it
  lists, so a version without one is incomplete and must be ignored.
- A version never changes. Every file is written with write-if-absent; a re-run
  writes nothing new, and anything already there wins (the manifest lists the
  stored bytes, so it always matches what a reader gets).
- ``doc_id`` is identity (it comes from the producer and stays the same across
  corrections); ``version`` is content: a hash of the source bytes and the
  pipeline config. Same input and config, same version. New bytes or a new model,
  new version, and the consumer replaces every chunk of the ``doc_id``.
- An incomplete document is not published unless asked: with ``partial=True`` it
  becomes a ``partial`` version carrying its ``coverage`` and ``missing_pages``,
  and the consumer decides whether to index it. Otherwise a ``failed`` event is
  written so a consumer is not left waiting.
- Every ``document.json`` and every event names the file's ``kind`` (``pdf``,
  ``docx``, ``html``, ``txt``, ``eml``, ``msg``), its ``unit`` (``page`` for a PDF,
  else ``document``: the whole file is one unit, published as page 1), its
  ``flags`` (for example ``images_not_ocrd``; a flag never changes the state) and
  its ``handler_version``. Email headers and attachment names go in
  ``document.json`` only, never in an event: events are fanned out widely.
- The event log is append-only and its keys are deterministic, so a re-run never
  duplicates an event. It is what a consumer replays to backfill or reconcile;
  delivery (S3 -> EventBridge -> SQS on ``_manifest.json``) is infrastructure.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from parser_service.storage.store import Store, StoreNotFoundError

# Stays 1 while the new fields (kind, unit, flags, handler_version, email) are
# additive; becomes 2 if consumers turn out to reject unknown fields (an open
# question of roadmap item 15, a release gate of its sub-step 3).
SCHEMA_VERSION = 1
ROOT = "published/v1"

# The kinds whose document.json carries ``email``.
EMAIL_KINDS = ("eml", "msg")

EVENT_PUBLISHED = "published"
EVENT_PARTIAL = "partial"
EVENT_FAILED = "failed"
EVENT_DELETED = "deleted"

_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")


def slugify(text: str) -> str:
    """A valid id from free text: lowercase, runs of other characters become ``-``."""
    slug = re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-._")[:128].rstrip("-._")
    if not slug:
        raise ValueError(f"cannot make an id from {text!r}")
    return slug


def check_id(value: Any, what: str) -> str:
    """``value`` if it is a valid id (``[a-z0-9][a-z0-9._-]*``, at most 128), else ValueError.

    Ids are S3 key segments: no ``/``, no spaces (S3 events URL-encode them), no
    upper case (two spellings of one document), no ``..``.
    """
    if not isinstance(value, str) or not _ID_RE.fullmatch(value) or ".." in value:
        raise ValueError(
            f"{what} must be lowercase letters, digits, '.', '_' or '-' (at most 128, "
            f"starting with a letter or digit), got {value!r}"
        )
    return value


@dataclass(frozen=True)
class PublishRequest:
    """What to publish a parsed file as."""

    doc_id: str
    job_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    partial: bool = False  # publish an incomplete document as a ``partial`` version

    def __post_init__(self) -> None:
        check_id(self.doc_id, "doc_id")
        if self.job_id is not None:
            check_id(self.job_id, "job_id")


def version_for(source_sha256: str, config_hash: str, kind: str, ok_pages: int) -> str:
    """The version name: a hash of the content and config, plus the coverage when not complete.

    ``<hash>`` for a complete document; ``<hash>.partial-NNNN`` or
    ``<hash>.failed-NNNN`` (NNNN = ok pages) otherwise. Ok pages are never
    replaced, so a repair that adds pages gives a new name, and the last one is
    the complete version.
    """
    base = hashlib.sha256(f"{source_sha256}/{config_hash}".encode()).hexdigest()[:16]
    if kind == EVENT_PUBLISHED:
        return base
    return f"{base}.{kind}-{ok_pages:04d}"


def _doc_prefix(doc_id: str, version: str) -> str:
    return f"{ROOT}/docs/{doc_id}/{version}"


def manifest_key(doc_id: str, version: str) -> str:
    return f"{_doc_prefix(doc_id, version)}/_manifest.json"


def event_key(day: str, doc_id: str, version: str, kind: str) -> str:
    return f"{ROOT}/events/dt={day}/{doc_id}--{version}--{kind}.json"


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _write_once(store: Store, key: str, data: bytes) -> bytes:
    """Write ``data`` unless ``key`` exists; return the bytes stored there either way."""
    if store.write_if_absent(key, data):
        return data
    return store.read(key)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def publish_document(
    store: Store,
    request: PublishRequest,
    *,
    state: str,
    source_name: str,
    source_sha256: str,
    work_id: str | None,
    config: dict[str, Any],
    config_hash: str,
    page_count: int,
    markdown: str,
    pages_meta: list[dict[str, Any]],
    page_texts: dict[int, str],
    kind: str | None = "pdf",
    unit: str = "page",
    flags: list[str] | tuple[str, ...] = (),
    handler_version: str | None = None,
    email: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish one parsed file under ``request.doc_id``; return what was published.

    ``state`` is the file's state (``complete``, ``failed`` or ``in_progress``);
    ``markdown`` and ``pages_meta`` are the stitched document and its page map
    (with gap markers when not complete; a not-ok entry may carry its
    ``reason_code``); ``page_texts`` holds the markdown of every ok page. An
    ``in_progress`` file publishes nothing. ``work_id`` is None for a file refused
    at intake (only a ``failed`` event is written for it).

    ``kind``, ``unit``, ``flags`` and ``handler_version`` describe the file (None
    kind: intake found none) and go into ``document.json`` and every event.
    ``email`` (``{"headers", "attachments"}``) is for ``eml`` and ``msg`` only and
    goes into ``document.json`` only: no event ever carries it. Flags never change
    the outcome: a flagged complete file is ``published``.

    Returns ``{"type", "version", "manifest_key", "event_key"}``; ``type`` is
    ``published``, ``partial``, ``failed`` or None.
    """
    if email is not None and kind not in EMAIL_KINDS:
        raise ValueError(f"email is for {' and '.join(EMAIL_KINDS)} only, not kind {kind!r}")
    ok_pages = sorted(n for n, _ in page_texts.items())
    missing = [p["page"] for p in pages_meta if p["state"] != "ok"]
    complete = state == "complete"
    if complete:
        event_type = EVENT_PUBLISHED
    elif state == "failed":
        event_type = EVENT_PARTIAL if request.partial and ok_pages else EVENT_FAILED
    else:
        return {"type": None, "version": None, "manifest_key": None, "event_key": None}

    version = version_for(source_sha256, config_hash, event_type, len(ok_pages))
    coverage = round(len(ok_pages) / page_count, 4) if page_count else 0.0
    # Shared by document.json and the events. Never add ``email`` here.
    common = {
        "schema_version": SCHEMA_VERSION,
        "doc_id": request.doc_id,
        "version": version,
        "job_id": request.job_id,
        "source_name": source_name,
        "source_sha256": source_sha256,
        "page_count": page_count,
        "coverage": coverage,
        "missing_pages": missing,
        "kind": kind,
        "unit": unit,
        "flags": sorted({str(flag) for flag in flags}),
        "handler_version": handler_version,
    }

    if event_type == EVENT_FAILED:
        key = event_key(_now()[:10], request.doc_id, version, event_type)
        reasons = [
            {
                "page": p["page"],
                "state": p["state"],
                "reason": p["reason"],
                "reason_code": p.get("reason_code"),
            }
            for p in pages_meta
            if p["state"] != "ok"
        ]
        event = {"type": event_type, **common, "not_ok_pages": reasons, "at": _now()}
        _write_once(store, key, _json_bytes(event))
        return {"type": event_type, "version": version, "manifest_key": None, "event_key": key}

    prefix = _doc_prefix(request.doc_id, version)
    m_key = manifest_key(request.doc_id, version)
    stored_manifest = _read_json(store, m_key)
    if stored_manifest is None:
        files: list[tuple[str, bytes]] = []
        page_files: dict[int, str] = {}
        for n in ok_pages:
            path = f"pages/{n:04d}.md"
            page_files[n] = path
            files.append((path, (page_texts[n].strip() + "\n").encode("utf-8")))
        files.append(("document.md", markdown.encode("utf-8")))
        document = {
            **common,
            "state": "complete" if complete else "partial",
            "work_id": work_id,
            "config": config,
            "config_hash": config_hash,
            "metadata": request.metadata,
            "published_at": _now(),
            "page_marker": "==== page {number} ===",
            "pages": [
                {
                    "page": p["page"],
                    "state": p["state"],
                    "route": p.get("route"),
                    "confidence": p.get("confidence"),
                    "char_span": p["char_span"],
                    "file": page_files.get(p["page"]),
                }
                for p in pages_meta
            ],
        }
        if kind in EMAIL_KINDS:
            document["email"] = email
        files.append(("document.json", _json_bytes(document)))
        entries = []
        for path, data in files:
            stored = _write_once(store, f"{prefix}/{path}", data)
            entries.append({"path": path, "sha256": _sha256(stored), "bytes": len(stored)})
        stored_document = json.loads(store.read(f"{prefix}/document.json"))
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "doc_id": request.doc_id,
            "version": version,
            "state": stored_document["state"],
            "published_at": stored_document["published_at"],
            "files": entries,
        }
        _write_once(store, m_key, _json_bytes(manifest))
        stored_manifest = _read_json(store, m_key)
        assert stored_manifest is not None  # just written (or already there)

    published_at = stored_manifest["published_at"]
    key = event_key(published_at[:10], request.doc_id, version, event_type)
    event = {
        "type": event_type,
        **common,
        "manifest": store.location(m_key),
        "document": store.location(f"{prefix}/document.json"),
        "metadata": request.metadata,
        "at": published_at,
    }
    _write_once(store, key, _json_bytes(event))
    return {"type": event_type, "version": version, "manifest_key": m_key, "event_key": key}


def publish_deletion(store: Store, doc_id: str, job_id: str | None) -> str:
    """Write a ``deleted`` event for ``doc_id``; return its key.

    The consumer purges every chunk of the document. Versions already published
    stay (they are immutable); a retention rule removes them.
    """
    check_id(doc_id, "doc_id")
    now = _now()
    key = event_key(now[:10], doc_id, job_id or "no-job", EVENT_DELETED)
    body = {"schema_version": SCHEMA_VERSION, "type": EVENT_DELETED, "doc_id": doc_id, "job_id": job_id, "at": now}
    _write_once(store, key, _json_bytes(body))
    return key


def _read_json(store: Store, key: str) -> dict[str, Any] | None:
    try:
        value = json.loads(store.read(key).decode("utf-8"))
    except (StoreNotFoundError, ValueError):
        return None
    return value if isinstance(value, dict) else None
