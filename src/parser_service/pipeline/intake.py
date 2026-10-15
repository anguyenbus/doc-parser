"""
intake.py — Intake: sniff a file's kind, refuse what is disabled or too large, and write its
units (one-page PDFs, or the source itself) and the manifest.
"""

from __future__ import annotations

import io
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from parser_service.delivery.publish import PublishRequest, publish_document
from parser_service.formats.result import (
    STATE_DEGRADED,
    STATE_FAILED,
    STATE_OK,
    STATE_PENDING,
)
from parser_service.formats.sniff import SNIFF_BYTES, Sniff, extension_of, sniff
from parser_service.observability import (
    extension_label,
    kind_label,
    log_event,
)
from parser_service.parsing.elements import sha256_file
from parser_service.pipeline.config import (
    FILE_FAILED,
    UNIT_DOCUMENT,
    UNIT_PAGE,
    PipelineConfigMismatch,
    config_hash,
    enabled_kinds,
    logger,
    manifest_kind,
    pipeline_config,
    raw_cap_bytes,
)
from parser_service.pipeline.layout import (
    _file_id,
    _local_store_for,
    _manifest_key,
    _out_prefix,
    _page_pdf_key,
    _pages_prefix,
    _put_json,
    _read_json,
    _source_key,
    _status_key,
    _work_id,
    _work_prefix,
)
from parser_service.settings import (
    ENABLED_KINDS_ENV,
)
from parser_service.storage.store import Store


def work_id_for(source: Path, kind: str | None = None) -> str:
    """The id of a source's work in the store: ``<sha256>/<config hash>``.

    Keyed by content and config, never by name: the same bytes under any name are
    split and parsed once per config; each name still gets its own
    ``out/<file id>/``. The config is ``kind``'s (default: the kind the file
    sniffs as, pdf when it has none).
    """
    if kind is None:
        kind = _sniff_source(Path(source)).kind or "pdf"
    return _work_id(sha256_file(source), config_hash(pipeline_config(kind)))


def work_dir_for(source: Path, work_root: Path) -> Path:
    """The work directory for a source file: ``work/<sha256>/<config hash>`` under the root."""
    return Path(work_root) / _work_prefix(work_id_for(source))


def _read_source(source: Path) -> bytes:
    with source.open("rb") as handle:
        return handle.read()


def _page_bytes(reader: Any, page_number: int) -> bytes:
    """Page ``page_number`` (1-based) of an open ``PdfReader`` as a one-page PDF."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_page(reader.pages[page_number - 1])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _restore_expired(store: Store, work_id: str, source: Path, manifest: Mapping[str, Any]) -> None:
    """Re-create ``source.<ext>`` (and a PDF's page files) gone though the manifest is not.

    An S3 lifecycle rule expires objects one by one, by age, and the page files are
    older than the manifest. The run always has the original in hand, so whatever
    is missing is written again from it. The manifest is not rewritten.
    """
    from pypdf import PdfReader

    kind = manifest_kind(manifest)
    if not store.exists(_source_key(work_id, kind)):
        store.write(_source_key(work_id, kind), _read_source(source))
    if kind != "pdf":
        return  # a single-unit kind's only unit is the source itself
    page_count = int(manifest["page_count"])
    present = set(store.list_keys(_pages_prefix(work_id)))
    missing = [n for n in range(1, page_count + 1) if _page_pdf_key(work_id, n) not in present]
    if not missing:
        return
    logger.info("%s: re-creating %d missing page file(s): %s", source.name, len(missing), missing)
    reader = PdfReader(str(source))
    for page_number in missing:
        store.write(_page_pdf_key(work_id, page_number), _page_bytes(reader, page_number))


def _intake(store: Store, work_id: str, source: Path, kind: str) -> dict[str, Any]:
    """Store ``source`` as ``kind`` units and return the manifest.

    A PDF is split into one-page PDFs. Order of writes: ``source.pdf`` (a copy of
    the original), the page files, then the manifest LAST, so a crash mid-split
    leaves no manifest and the split is simply redone. A single-unit kind writes
    ``source.<ext>``, then the manifest LAST (``unit`` ``document``,
    ``page_count`` 1), with the same rules; it has no ``pages/``.

    Idempotent: when a manifest already exists it is returned as is, after
    checking the pinned config and re-creating any stored file that has expired.
    Raises on an unreadable or encrypted PDF, and ``PipelineConfigMismatch`` when
    the file was started under a different config.
    """
    config = pipeline_config(kind)
    existing = _read_json(store, _manifest_key(work_id))
    if isinstance(existing, dict):
        if existing.get("config") != config:
            raise PipelineConfigMismatch(
                f"{store.location(_work_prefix(work_id))} was started with config "
                f"{existing.get('config')} but the current config is {config}; finish it "
                "under the original settings or restart it"
            )
        _restore_expired(store, work_id, source, existing)
        return existing

    if kind != "pdf":
        store.write(_source_key(work_id, kind), _read_source(source))
        manifest = {
            "source_name": source.name,
            "source_sha256": sha256_file(source),
            "kind": kind,
            "unit": UNIT_DOCUMENT,
            "page_count": 1,
            "config": config,
            "created_at": datetime.now(tz=UTC).isoformat(),
        }
        _put_json(store, _manifest_key(work_id), manifest)
        return manifest

    from pypdf import PdfReader  # local import; only needed for PDFs

    reader = PdfReader(str(source))
    if reader.is_encrypted:
        raise ValueError(f"{source.name} is encrypted and cannot be split")
    page_count = len(reader.pages)
    if page_count == 0:
        raise ValueError(f"{source.name} has no pages")

    store.write(_source_key(work_id), _read_source(source))
    for page_number in range(1, page_count + 1):
        store.write(_page_pdf_key(work_id, page_number), _page_bytes(reader, page_number))

    manifest = {
        "source_name": source.name,
        "source_sha256": sha256_file(source),
        "kind": "pdf",
        "unit": UNIT_PAGE,
        "page_count": page_count,
        "config": config,
        "created_at": datetime.now(tz=UTC).isoformat(),
    }
    _put_json(store, _manifest_key(work_id), manifest)
    return manifest


def _split(store: Store, work_id: str, source: Path) -> dict[str, Any]:
    """``_intake`` of a PDF: split ``source`` into one-page PDFs, return the manifest."""
    return _intake(store, work_id, source, "pdf")


def split_pdf(source: Path, work_dir: Path) -> dict[str, Any]:
    """Split ``source`` into one-page PDFs under ``work_dir`` and return the manifest.

    A thin wrapper over a local store, for callers that hold a directory:
    ``work_dir`` must be the directory ``work_dir_for`` returns. See ``_split``.
    """
    store, work_id = _local_store_for(Path(work_dir))
    manifest = _split(store, work_id, Path(source))
    # The one direct directory write: callers may put files here by hand afterwards.
    for sub in ("pages", "attempts", "results"):
        (Path(work_dir) / sub).mkdir(parents=True, exist_ok=True)
    return manifest


# An OLE2 file's directory can start past the first 64 KiB; one that sniffs as a
# plain ``ole2`` (no kind) is sniffed again with up to this much of the file.
_OLE2_SNIFF_BYTES = 8 * 1024 * 1024

# Intake reason codes: decided by the driver for the whole file, with no record.
INTAKE_UNSUPPORTED = "unsupported_type"
INTAKE_TOO_LARGE = "too_large"


def _sniff_source(source: Path, declared_type: str | None = None) -> Sniff:
    """Sniff a local file from its head, its name and its declared content type."""
    with source.open("rb") as handle:
        head = handle.read(SNIFF_BYTES)
        found = sniff(head, source.name, declared_type)
        if found.detected_type == "ole2" and len(head) == SNIFF_BYTES:
            head += handle.read(_OLE2_SNIFF_BYTES - SNIFF_BYTES)
            found = sniff(head, source.name, declared_type)
    return found


def _intake_rejection(found: Sniff, size: int) -> tuple[str, str] | None:
    """``(reason_code, reason)`` when intake refuses the file, else None (spec D1).

    No kind, or a kind missing from ``PARSER_ENABLED_KINDS``: ``unsupported_type``.
    A file over its kind's raw cap: ``too_large``. Both are decided again on every
    run, so a kind enabled later, or a raised cap, takes effect at once.
    """
    if found.kind is None:
        return INTAKE_UNSUPPORTED, (
            f"{INTAKE_UNSUPPORTED}: detected {found.detected_type}, which no kind handles"
        )
    if found.kind not in enabled_kinds():
        return INTAKE_UNSUPPORTED, (
            f"{INTAKE_UNSUPPORTED}: kind {found.kind} (detected {found.detected_type}) is not "
            f"in {ENABLED_KINDS_ENV}"
        )
    cap = raw_cap_bytes(found.kind)
    if cap is not None and size > cap:
        return INTAKE_TOO_LARGE, (
            f"{INTAKE_TOO_LARGE}: {size} bytes, over the {found.kind} cap of {cap} bytes"
        )
    return None


def _reject_at_intake(
    store: Store,
    source: Path,
    sha256: str,
    found: Sniff,
    reason_code: str,
    reason: str,
    started: float,
    publish: PublishRequest | None = None,
) -> dict[str, Any]:
    """Fail a file refused at intake: write ``out/<file id>/status.json`` only.

    Nothing is written under ``work/`` (there is no work id): the refusal is
    decided again on every run, so no stale record can outlive it. With
    ``publish``, a ``failed`` event is written too. Its version hashes the source
    with the config ``{"intake": reason_code, "kind": kind}``, so a re-run of the
    same refusal writes no second event.
    """
    file_id = _file_id(source.stem, sha256)
    unit = {
        "page": 1,
        "state": STATE_FAILED,
        "reason": reason,
        "reason_code": reason_code,
        "route": None,
        "attempts": 0,
        "retryable": False,
    }
    counts = {STATE_OK: 0, STATE_DEGRADED: 0, STATE_FAILED: 1, STATE_PENDING: 0}
    status = {
        "state": FILE_FAILED,
        "source_name": source.name,
        "source_sha256": sha256,
        "work_id": None,
        "kind": found.kind,
        "unit": UNIT_DOCUMENT,
        "detected_type": found.detected_type,
        "flags": [],
        "page_count": 1,
        "counts": counts,
        "not_ok_pages": [unit],
        "pages": [unit],
        "updated_at": datetime.now(tz=UTC).isoformat(),
    }
    published = None
    if publish is not None:
        intake_config = {"intake": reason_code, "kind": found.kind}
        published = publish_document(
            store,
            publish,
            state=FILE_FAILED,
            source_name=source.name,
            source_sha256=sha256,
            work_id=None,
            config=intake_config,
            config_hash=config_hash(intake_config),
            page_count=1,
            markdown="",
            pages_meta=[unit],
            page_texts={},
            kind=found.kind,
            unit=UNIT_DOCUMENT,
            flags=[],
            handler_version=None,
        )
    _put_json(store, _status_key(file_id), status)
    logger.error("%s: refused at intake: %s", source.name, reason_code)
    return {
        "state": FILE_FAILED,
        "file_id": file_id,
        "work_id": None,
        "kind": found.kind,
        "unit": UNIT_DOCUMENT,
        "detected_type": found.detected_type,
        "flags": [],
        "reason_code": reason_code,
        "work_dir": None,
        "out_dir": store.location(_out_prefix(file_id)),
        "page_count": 1,
        "counts": counts,
        "not_ok_pages": [unit],
        "pages": [unit],
        "status_path": store.location(_status_key(file_id)),
        "output_path": None,
        "partial_path": None,
        "output_key": None,
        "partial_key": None,
        "published": published,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def _file_event_fields(
    source: Path, found: Sniff, file_id: str, size: int, publish: PublishRequest | None
) -> dict[str, Any]:
    """The fields ``file_intake`` and ``file_done`` share (spec D9). Never the name."""
    return {
        "job_id": publish.job_id if publish is not None else None,
        "doc_id": publish.doc_id if publish is not None else None,
        "file_id": file_id,
        "kind": kind_label(found.kind),
        "detected_type": found.detected_type,
        "extension": extension_label(extension_of(source.name)),
        "mismatch": found.mismatch,
        "input_bytes": size,
    }


def _log_file_done(fields: Mapping[str, Any], result: Mapping[str, Any], output_chars: int) -> None:
    """The driver's ``file_done`` event: the file's outcome, intake rejections included.

    ``reason_code`` is the intake refusal, else the first unit that is not ok's
    code, else null.
    """
    reason_code = result.get("reason_code") or next(
        (p.get("reason_code") for p in result["not_ok_pages"]), None
    )
    log_event(
        "file_done",
        **fields,
        work_id=result["work_id"],
        unit=result["unit"],
        state=result["state"],
        reason_code=reason_code,
        flags=result["flags"],
        output_chars=output_chars,
        elapsed_seconds=result["elapsed_seconds"],
    )
