"""
page_pipeline.py — split-first, per-unit parsing with resume and a fail-closed stitch.

``parse_file_in_store(source, location)`` (old name ``parse_pdf_pages_in_store``) is
the unit-level counterpart of ``markdown_pipeline.parse_to_markdown``;
``parse_pdf_pages(source, work_root)`` is the same on a local directory:

  0. INTAKE  sniff the file's first bytes (``sniff.py``) to route it to a kind.
             No kind, or a kind missing from ``PARSER_ENABLED_KINDS``, fails the
             file with ``unsupported_type``; a single-unit file over its raw cap
             fails with ``too_large``. Neither writes anything under ``work/``.
  1. SPLIT   a PDF: store ``source.pdf``, then its one-page files (pypdf), then
             ``manifest.json`` LAST (kind, unit, source hash, true page count,
             pinned pipeline config). A single-unit kind (txt, docx, html, eml,
             msg) is one unit, the file itself: store ``source.<ext>``, then the
             manifest (``unit`` ``document``, ``page_count`` 1). Routing after
             intake reads the manifest's ``kind``, never the file name.
  2. PARSE   each one-page file with the unchanged ``parse_to_markdown`` — Docling
             first, gate, escalation to VLM/Textract only when promoted — in a pool
             of worker processes. Each page writes ``results/NNNN.json`` (the page
             result and the commit marker; it holds the markdown) and then a
             readable copy, ``results/NNNN.md``, that the pipeline never reads.
  3. STATUS  ``status.json`` is rebuilt from the page records (never the source of
             truth). Every page is ``ok``, ``degraded``, ``failed`` or ``pending``.
  4. STITCH  only when EVERY page is ``ok``: ``output.md`` with a
             ``==== page {number} ===`` line before each page, plus ``output.json``.
             Anything else leaves the file ``failed`` with the page numbers and
             reasons in ``status.json`` and no ``output.md``. A partial document
             (``output.partial.md``, gaps marked) is written only on request.

Every read and write goes through a store (``store.py``): a local directory or an
S3 prefix. Restart is the normal path — there is no recovery mode. A page is
finished iff its result JSON exists, and a stored result is never overwritten
(first write wins). A re-run skips ``ok`` pages; a ``failed`` or ``degraded`` page
under ``max_attempts`` has its record moved to ``results/NNNN.attempt-K.json`` and
runs again. The attempt counter is written BEFORE each try, so a page that kills
its worker (no exception, no record) still becomes ``failed`` once the limit is
reached. A record also carries ``reason_code`` (the text of ``reason`` before the
first ``:``). A deterministic failure (``timeout`` and the other codes in
``FINAL_REASON_CODES``) stores the ``limits`` behind it and is final: it is never
re-run while those limits keep their values (``_is_final``).

Keys under the store location. ``work/`` is keyed by content and config (work id
= ``<source sha256>/<config hash>``), so the same bytes under any name are split
and parsed once per config, and a new config never mixes with an old one;
``out/`` is keyed by the named file (file id = ``<stem>-<sha256[:16]>``), so each
name gets its own status and output. ``work/`` is the part a lifecycle rule may
expire; ``out/`` is self-contained. ``published/`` (``publish.py``) is the
versioned hand-off to consumers:

    work/<work id>/source.<ext>                  copy of the original; written first. The
                                                 extension is fixed by the kind; a single-unit
                                                 kind's only unit is this file
    work/<work id>/pages/0001.pdf …              a PDF's one-page files (1-based)
    work/<work id>/manifest.json                 written last by the split
    work/<work id>/attempts/0001 …               attempt counter, written before each try
    work/<work id>/results/0001.json             the page's record; exists = page finished
    work/<work id>/results/0001.md               readable copy of the page's markdown
    work/<work id>/results/0001.attempt-1.json   an archived non-ok record
    out/<file id>/output.md                      only when every page is ok
    out/<file id>/output.json                    metadata for output.md
    out/<file id>/output.partial.md, .json       only on request, when not complete
    out/<file id>/status.json                    summary rebuilt from the records; written
                                                 LAST, after the outputs it reports
    published/v1/...                             on request: see ``publish.py``

A manifest with no ``kind`` (written before kinds existed) is a PDF of pages. Each
kind has its own pipeline config (``pipeline_config(kind)``, with the kind's
handler version), so each kind's work has its own config hash.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.metadata
import io
import json
import logging
import multiprocessing
import os
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from parser_service.guards import DocxLimits, GuardRejection, check_docx
from parser_service.markdown_pipeline import enable_converter_reuse, parse_to_markdown
from parser_service.observability import (
    extension_label,
    kind_label,
    log_event,
    observe_unit,
    truncate,
)
from parser_service.page_worker import Reply, Request, WorkerTimeout, _exception_reply
from parser_service.parser_service import PARSER_VERSION, _sha256_file
from parser_service.publish import EMAIL_KINDS, PublishRequest, publish_document
from parser_service.render import render_page, text_layer_tokens
from parser_service.route_stats import (
    ROUTE_DOCLING_KEPT,
    ROUTE_TEXTRACT_FALLBACK,
    ROUTE_VLM_FALLBACK,
)
from parser_service.settings import (
    DEADLINE_ENV,
    DEFAULT_MAX_OUTPUT_MB,
    DEFAULT_REMOTE_REQUEST_TIMEOUT_S,
    DOCX_GUARD_ENV,
    ENABLED_KINDS_ENV,
    MAX_OUTPUT_ENV,
    RAW_CAP_ENV,
    REMOTE_REQUEST_TIMEOUT_ENV,
    split_kinds,
)
from parser_service.sniff import KIND_EXTENSIONS, KINDS, SNIFF_BYTES, Sniff, sniff
from parser_service.store import LocalStore, Store, StoreNotFoundError, open_store
from parser_service.unit_handlers import handler_version, parse_unit

logger = logging.getLogger(__name__)

# Page states. ``pending`` appears only in status.json (a page with no record yet).
STATE_OK = "ok"
STATE_DEGRADED = "degraded"
STATE_FAILED = "failed"
STATE_PENDING = "pending"

# File states in status.json.
FILE_COMPLETE = "complete"
FILE_FAILED = "failed"
FILE_IN_PROGRESS = "in_progress"

PAGE_MARKER = "==== page {number} ==="
_PAGE_MARKER_RE = re.compile(r"^==== page \d+(?: \([^)\n]*\))? ===\n\n?", re.MULTILINE)

_DEFAULT_MAX_ATTEMPTS = 3
_ROUTE_BLANK = "blank"

# parse_to_markdown warning codes that mean the page produced nothing usable.
_FATAL_WARNING_CODES = (
    "docling_failed",
    "unhandled_exception",
    "input_too_large",
    "unsupported_type",
    "page_unparseable",
)

# Blank-page probe: a page with no text layer is rendered small and counted as
# blank when almost no pixel is darker than near-white.
_BLANK_PROBE_DPI = 36
_BLANK_DARK_LEVEL = 235
_BLANK_MAX_DARK_FRACTION = 0.0005


class PipelineConfigMismatch(RuntimeError):
    """The work directory was started under a different pipeline config."""


class PageRecordError(RuntimeError):
    """A page's stored result exists but cannot be used (unreadable, or lost in an archive)."""


class KindNotEnabled(RuntimeError):
    """The unit's kind is not in this page service's ``PARSER_ENABLED_KINDS`` (HTTP 409)."""


def default_max_attempts() -> int:
    """Attempt limit per page (``PARSER_PAGE_MAX_ATTEMPTS``, default 3)."""
    return int(os.environ.get("PARSER_PAGE_MAX_ATTEMPTS", str(_DEFAULT_MAX_ATTEMPTS)))


def default_workers() -> int:
    """Worker processes (``PARSER_PAGE_WORKERS``, default min(4, cpus // 2)).

    Each worker holds its own Docling models (about 2-3 GB peak on OCR pages).
    """
    env = os.environ.get("PARSER_PAGE_WORKERS")
    if env is not None:
        return int(env)
    return max(1, min(4, (os.cpu_count() or 2) // 2))


# Docling decides the text of every docling-kept page, so its version is part of the
# config: an upgrade must not resume onto pages parsed by the old version.
DOCLING_VERSION: str = importlib.metadata.version("docling")


UNIT_PAGE = "page"
UNIT_DOCUMENT = "document"


def pipeline_config(kind: str = "pdf") -> dict[str, Any]:
    """The settings that change a ``kind`` unit's output. Pinned in the manifest at intake.

    A PDF page: the parser and Docling versions and the escalation settings. Every
    other kind leaves the escalation settings out (a VLM model change does not
    re-parse a DOCX), and txt, which never runs Docling, leaves Docling out too.
    Each config names its kind and the kind's handler version
    (``unit_handlers.HANDLER_VERSIONS``), so every kind gets its own hash and a
    handler bump changes only its own kind's.
    """
    if kind not in KIND_EXTENSIONS:
        raise ValueError(f"unknown kind {kind!r}")
    if kind == "pdf":
        config: dict[str, Any] = {
            "parser_version": PARSER_VERSION,
            "docling_version": DOCLING_VERSION,
            "escalation_engine": os.environ.get("PARSER_ESCALATION_ENGINE", "vlm"),
            "vlm_model": os.environ.get("BEDROCK_VLM_MODEL", ""),
            "scan_fastpath": os.environ.get("PARSER_SCAN_FASTPATH", "").lower() in ("1", "true"),
            "arbitration": os.environ.get("PARSER_ESCALATION_ARBITRATION", "").lower()
            in ("1", "true"),
        }
    else:
        config = {"parser_version": PARSER_VERSION}
        if kind != "txt":
            config["docling_version"] = DOCLING_VERSION
    config["kind"] = kind
    config["handler_version"] = handler_version(kind)
    return config


def manifest_kind(manifest: Mapping[str, Any]) -> str:
    """The kind of a manifest's file. No ``kind`` (written before kinds existed) is pdf."""
    return str(manifest.get("kind") or "pdf")


def unit_of(kind: str) -> str:
    """What one unit of ``kind`` is: a ``page`` of a PDF, else the whole ``document``."""
    return UNIT_PAGE if kind == "pdf" else UNIT_DOCUMENT


def enabled_kinds() -> tuple[str, ...]:
    """The kinds intake routes (``PARSER_ENABLED_KINDS``, default every handled kind).

    ``settings.load_settings`` validates the value at start-up; here it is only
    split, like the other call-time reads.
    """
    return split_kinds(os.environ.get(ENABLED_KINDS_ENV))


def raw_cap_bytes(kind: str) -> int | None:
    """The raw size cap of a ``kind`` file in bytes (None: no cap). Read at call time.

    The caps are in MB (1,000,000 bytes) in ``RAW_CAP_ENV``; PDFs have none here.
    """
    if kind not in RAW_CAP_ENV:
        return None
    name, default = RAW_CAP_ENV[kind]
    return int(os.environ.get(name, str(default))) * 1_000_000


def max_output_mb() -> int:
    """The output cap of a single-unit document, in MB (``PARSER_MAX_OUTPUT_MB``)."""
    return int(os.environ.get(MAX_OUTPUT_ENV, str(DEFAULT_MAX_OUTPUT_MB)))


def output_cap_bytes(kind: str) -> int | None:
    """The cap on one unit's markdown in bytes, sent to the parser (None: no cap).

    Single-unit kinds only: a PDF page's output is bounded by the page, so PDF
    requests keep sending None. Read at call time.
    """
    if kind == "pdf":
        return None
    return max_output_mb() * 1_000_000


# Reason codes of deterministic unit failures (D5). A record with one of them, and
# with the ``limits`` behind it still equal to the current values, is final: it is
# never archived or re-run. Every other failure (an exception, ``docling_failed``,
# a worker or pod death) is retried under the attempt limit.
FINAL_REASON_CODES = frozenset(
    {"timeout", "zip_bomb", "too_complex", "output_too_large", "unsupported_type"}
)


def deadline_for(kind: str = "pdf") -> int:
    """The parse deadline of one unit of ``kind``, in seconds. Read at call time.

    Not part of ``pipeline_config``: a changed deadline never re-parses finished
    work; it only releases ``timeout`` records stored under another value.
    """
    if kind not in DEADLINE_ENV:
        raise ValueError(f"no parse deadline is defined for kind {kind!r}")
    name, default = DEADLINE_ENV[kind]
    return int(os.environ.get(name, str(default)))


def remote_request_timeout() -> int:
    """The driver's timeout for one page request (``PARSER_REMOTE_REQUEST_TIMEOUT_S``)."""
    return int(os.environ.get(REMOTE_REQUEST_TIMEOUT_ENV, str(DEFAULT_REMOTE_REQUEST_TIMEOUT_S)))


def docx_guard_limits() -> DocxLimits:
    """The DOCX cost guard's thresholds (``DOCX_GUARD_ENV``). Read at call time.

    ``settings.load_settings`` validates them at start-up. Not part of
    ``pipeline_config``: a changed threshold never re-parses finished work; it
    only releases ``zip_bomb`` and ``too_complex`` records stored under another
    value (spec D5).
    """
    values = {
        key: int(os.environ.get(name, str(default)))
        for key, (name, default) in DOCX_GUARD_ENV.items()
    }
    return DocxLimits(**values)


def current_limits(kind: str = "pdf") -> dict[str, Any]:
    """The current value of every limit a final record of ``kind`` can store.

    Read from the environment at call time, like ``pipeline_config``. A stored
    record's ``limits`` are compared with these values by ``_is_final``. A
    single-unit kind also has the output cap (``output_too_large``), and a DOCX
    the six guard thresholds (``zip_bomb``, ``too_complex``). A kind whose
    handler has not shipped yet may have no deadline: it then has no
    ``deadline_s`` (no ``timeout`` record of it can exist).
    """
    limits: dict[str, Any] = {}
    if kind in DEADLINE_ENV:
        limits["deadline_s"] = deadline_for(kind)
    if output_cap_bytes(kind) is not None:
        limits["max_output_mb"] = max_output_mb()
    if kind == "docx":
        limits.update(asdict(docx_guard_limits()))
    return limits


def _guard_rejection(kind: str, unit_path: Path) -> GuardRejection | None:
    """The service-side cost guard of one unit (spec D7): a rejection, or None to parse.

    DOCX only; every other kind is bounded by its raw cap and deadline.
    """
    if kind != "docx":
        return None
    return check_docx(unit_path, docx_guard_limits())


def _reason_code(reason: str | None) -> str | None:
    """The machine-readable code of a ``"<code>: <detail>"`` reason (None for no reason)."""
    if not reason:
        return None
    return reason.partition(":")[0].strip() or None


def _flag_list(flags: Any) -> list[str]:
    """``flags`` as a sorted list of distinct strings (none: an empty list)."""
    return sorted({str(flag) for flag in flags or ()})


def _is_final(record: Mapping[str, Any], limits_now: Mapping[str, Any]) -> bool:
    """Whether a stored record is a deterministic failure that must not run again.

    Final when its ``reason_code`` is in ``FINAL_REASON_CODES``, it has a ``limits``
    mapping, and every stored limit equals its current value. A record written
    before reason codes existed (no ``reason_code``) is never final. A raised (or
    otherwise changed) limit releases the record: it is then retried like any
    other non-ok record, under the same attempt counter.
    """
    if record.get("state") == STATE_OK or record.get("reason_code") not in FINAL_REASON_CODES:
        return False
    limits = record.get("limits")
    if not isinstance(limits, dict):
        return False
    return all(key in limits_now and limits_now[key] == value for key, value in limits.items())


def config_hash(config: dict[str, Any] | None = None) -> str:
    """12 hex digits naming a pipeline config: the second level of ``work/``.

    A change of model, engine or parser version gets a new hash, so its pages are
    parsed into their own directory instead of mixing with (or being refused by)
    the old ones.
    """
    canonical = json.dumps(pipeline_config() if config is None else config, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def strip_page_markers(markdown: str) -> str:
    """Remove the ``==== page N ===`` lines (evaluation gold has no markers)."""
    return _PAGE_MARKER_RE.sub("", markdown)


# ---------------------------------------------------------------------------
# Store keys and small read/write helpers. Only the key helpers know the layout.
# ---------------------------------------------------------------------------


def _name(page_number: int) -> str:
    return f"{page_number:04d}"


def _work_prefix(work_id: str) -> str:
    """Intermediates: the part of a file an S3 lifecycle rule may expire."""
    return f"work/{work_id}"


def _out_prefix(file_id: str) -> str:
    """The final document and its status; self-contained once ``work/`` is gone."""
    return f"out/{file_id}"


def _manifest_key(work_id: str) -> str:
    return f"{_work_prefix(work_id)}/manifest.json"


def _source_key(work_id: str, kind: str = "pdf") -> str:
    """The stored original: ``source.<ext>``, the extension fixed by the kind."""
    return f"{_work_prefix(work_id)}/source.{KIND_EXTENSIONS[kind]}"


def _pages_prefix(work_id: str) -> str:
    return f"{_work_prefix(work_id)}/pages/"


def _page_pdf_key(work_id: str, page_number: int) -> str:
    return f"{_work_prefix(work_id)}/pages/{_name(page_number)}.pdf"


def _unit_key(work_id: str, manifest: Mapping[str, Any], page_number: int) -> str:
    """The file of one unit: a PDF's page file, or a single-unit kind's stored source.

    The file ``store.local_file`` hands out keeps the key's base name, so its
    extension always agrees with the manifest's kind.
    """
    kind = manifest_kind(manifest)
    if kind == "pdf":
        return _page_pdf_key(work_id, page_number)
    return _source_key(work_id, kind)


def _record_key(work_id: str, page_number: int) -> str:
    return f"{_work_prefix(work_id)}/results/{_name(page_number)}.json"


def _markdown_key(work_id: str, page_number: int) -> str:
    return f"{_work_prefix(work_id)}/results/{_name(page_number)}.md"


def _archive_prefix(work_id: str, page_number: int) -> str:
    return f"{_work_prefix(work_id)}/results/{_name(page_number)}.attempt-"


def _archive_key(work_id: str, page_number: int, k: int) -> str:
    return f"{_archive_prefix(work_id, page_number)}{k}.json"


def _archive_number(key: str, page_number: int) -> int | None:
    """K when ``key`` is this page's ``NNNN.attempt-K.json``, else None."""
    match = re.fullmatch(rf"{_name(page_number)}\.attempt-(\d+)\.json", key.rpartition("/")[2])
    return int(match.group(1)) if match else None


def _attempts_key(work_id: str, page_number: int) -> str:
    return f"{_work_prefix(work_id)}/attempts/{_name(page_number)}"


def _status_key(file_id: str) -> str:
    return f"{_out_prefix(file_id)}/status.json"


def _output_key(file_id: str, name: str) -> str:
    return f"{_out_prefix(file_id)}/{name}"


def _put_text(store: Store, key: str, text: str) -> None:
    store.write(key, text.encode("utf-8"))


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _put_json(store: Store, key: str, obj: Any) -> None:
    store.write(key, _json_bytes(obj))


def _read_json(store: Store, key: str) -> Any:
    """The parsed object, or None when the key is missing or is not valid JSON."""
    try:
        return json.loads(store.read(key).decode("utf-8"))
    except (StoreNotFoundError, ValueError):
        return None


def _read_attempts(store: Store, work_id: str, page_number: int) -> int:
    try:
        return int(store.read(_attempts_key(work_id, page_number)).decode("utf-8").strip())
    except (StoreNotFoundError, ValueError):
        return 0


def _load_record(store: Store, work_id: str, page_number: int) -> dict[str, Any] | None:
    record = _read_json(store, _record_key(work_id, page_number))
    return record if isinstance(record, dict) else None


def _read_record(
    store: Store, work_id: str, page_number: int
) -> tuple[bytes, dict[str, Any]] | None:
    """The page's record as stored bytes and parsed dict, or None when it has none.

    Unlike ``_load_record`` this does not read an unparseable record as "no record":
    it raises ``PageRecordError``, before any work is spent on a page whose result
    could never be written over it.
    """
    key = _record_key(work_id, page_number)
    try:
        data = store.read(key)
    except StoreNotFoundError:
        return None
    try:
        record = json.loads(data.decode("utf-8"))
    except ValueError:
        record = None
    if not isinstance(record, dict):
        raise PageRecordError(
            f"page {page_number}: {store.location(key)} exists but cannot be read as a record; "
            "remove it or restart the file"
        )
    return data, record


def _load_record_checked(store: Store, work_id: str, page_number: int) -> dict[str, Any] | None:
    """``_read_record`` without the bytes: the record, None, or ``PageRecordError``."""
    found = _read_record(store, work_id, page_number)
    return found[1] if found is not None else None


def _file_id(stem: str, sha256: str) -> str:
    return f"{stem}-{sha256[:16]}"


def _work_id(sha256: str, config: str) -> str:
    return f"{sha256}/{config}"


def work_id_for(source: Path, kind: str | None = None) -> str:
    """The id of a source's work in the store: ``<sha256>/<config hash>``.

    Keyed by content and config, never by name: the same bytes under any name are
    split and parsed once per config; each name still gets its own
    ``out/<file id>/``. The config is ``kind``'s (default: the kind the file
    sniffs as, pdf when it has none).
    """
    if kind is None:
        kind = _sniff_source(Path(source)).kind or "pdf"
    return _work_id(_sha256_file(source), config_hash(pipeline_config(kind)))


def file_id_for(source: Path) -> str:
    """The id of a named source file's output in the store: ``<stem>-<sha256[:16]>``."""
    return _file_id(source.stem, _sha256_file(source))


def work_dir_for(source: Path, work_root: Path) -> Path:
    """The work directory for a source file: ``work/<sha256>/<config hash>`` under the root."""
    return Path(work_root) / _work_prefix(work_id_for(source))


def _local_store_for(work_dir: Path) -> tuple[LocalStore, str]:
    """The local store and work id that ``work_dir_for`` built ``work_dir`` from."""
    work_id = f"{work_dir.parent.name}/{work_dir.name}"
    root = work_dir.parents[2] if len(work_dir.parents) > 2 else work_dir.parent
    if work_dir != root / _work_prefix(work_id):
        raise ValueError(
            f"{work_dir} is not a work directory: expected <work_root>/{_work_prefix(work_id)} "
            "(see work_dir_for)"
        )
    return LocalStore(root), work_id


# ---------------------------------------------------------------------------
# Split
# ---------------------------------------------------------------------------


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
            "source_sha256": _sha256_file(source),
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
        "source_sha256": _sha256_file(source),
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


# ---------------------------------------------------------------------------
# One page
# ---------------------------------------------------------------------------


def _is_blank_page(page_pdf: Path) -> bool:
    """True iff the page has no text layer AND renders (almost) entirely white.

    A blank page has nothing for Docling or an engine to recover; without this
    probe it would come back empty and fail the whole file. Never raises — any
    probe failure means "not blank" and the page takes the normal path.
    """
    try:
        if text_layer_tokens(page_pdf).get(0, 0) > 0:
            return False
        from PIL import Image

        image = Image.open(io.BytesIO(render_page(page_pdf, 0, dpi=_BLANK_PROBE_DPI)))
        histogram = image.convert("L").histogram()
        total = sum(histogram)
        dark = sum(histogram[:_BLANK_DARK_LEVEL])
        return total > 0 and dark / total <= _BLANK_MAX_DARK_FRACTION
    except Exception as exc:  # noqa: BLE001
        logger.warning("blank-page probe failed for %s: %s", page_pdf.name, truncate(str(exc)))
        return False


def classify_page_result(result: dict[str, Any]) -> tuple[str, str | None]:
    """Map one page's ``parse_to_markdown`` result to ``(state, reason)``.

    ok        Docling kept by the gate, the engine's output shipped, or arbitration
              kept Docling over a successful engine call.
    degraded  the gate rejected Docling but its text was kept anyway: the engine
              failed (``*-fallback-docling``) or the budget cap suppressed the call.
    failed    nothing usable: Docling failed, the engine failed with no Docling
              text to fall back to, or the page came back empty.
    """
    for warning in result.get("warnings", []):
        code = warning.get("code")
        if code in _FATAL_WARNING_CODES:
            return STATE_FAILED, f"{code}: {warning.get('message', '')}"

    routes = result.get("page_routes", [])
    if not routes:
        return STATE_FAILED, "no_route_recorded"
    route = routes[0].get("route")
    route_reason = routes[0].get("reason")

    if route in (ROUTE_VLM_FALLBACK, ROUTE_TEXTRACT_FALLBACK):
        return STATE_DEGRADED, f"{route}: {route_reason}"
    if route == ROUTE_DOCLING_KEPT and route_reason == "budget_exhausted_no_escalation":
        return STATE_DEGRADED, route_reason
    if not str(result.get("markdown", "")).strip():
        return STATE_FAILED, f"empty_output: route={route}"
    return STATE_OK, None


def _write_record(
    store: Store,
    work_id: str,
    page_number: int,
    state: str,
    reason: str | None,
    markdown: str,
    attempt: int,
    extra: dict[str, Any] | None = None,
    *,
    reason_code: str | None = None,
    limits: dict[str, Any] | None = None,
    kind: str = "pdf",
) -> dict[str, Any]:
    """Write the page's record (the commit marker), then its readable ``.md`` copy.

    ``reason_code`` defaults to the text of ``reason`` before the first ``:``.
    ``limits`` (the limit values behind a deterministic failure, see ``_is_final``)
    is stored only when given. Every record names its ``kind`` (from the
    manifest), ``unit``, ``handler_version`` and ``flags`` (``extra["flags"]`` from
    the handler, else an empty list; a flag never changes the state).

    First write wins: the record is written only if the page has none. The writer
    that created it then writes the ``.md`` copy; any other writer gets the stored
    record back and its own result is discarded, so one run's markdown is never
    left beside another run's record.
    """
    markdown_key = _markdown_key(work_id, page_number)
    record: dict[str, Any] = {
        "page": page_number,
        "state": state,
        "reason": reason,
        "reason_code": reason_code if reason_code is not None else _reason_code(reason),
        "markdown": markdown,
        "parser_version": PARSER_VERSION,
        "n_chars": len(markdown),
        "markdown_file": markdown_key.rpartition("/")[2],
        "markdown_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        "attempt": attempt,
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "worker_pid": os.getpid(),
        "worker_host": socket.gethostname(),
        "kind": kind,
        "unit": unit_of(kind),
        "handler_version": handler_version(kind),
    }
    if limits is not None:
        record["limits"] = dict(limits)
    if extra:
        record.update(extra)
    record["flags"] = _flag_list(record.get("flags"))
    if store.write_if_absent(_record_key(work_id, page_number), _json_bytes(record)):
        _put_text(store, markdown_key, markdown)
        return record
    stored = _load_record(store, work_id, page_number)
    if stored is None:
        # The one place an unparseable record is not read as "no record": the
        # write was refused because of it, so it cannot be treated as absent.
        raise PageRecordError(
            f"page {page_number}: a result already exists at "
            f"{store.location(_record_key(work_id, page_number))} but cannot be read as a record"
        )
    return stored


def _parse_page(
    page_pdf: Path, page_number: int, escalate: bool
) -> tuple[str, str | None, str, dict[str, Any]]:
    """Probe and parse one page file: ``(state, reason, markdown, record fields)``."""
    if _is_blank_page(page_pdf):
        return STATE_OK, None, "", {"route": _ROUTE_BLANK, "route_reason": "blank_page"}
    result = parse_to_markdown(page_pdf, escalate=escalate)

    state, reason = classify_page_result(result)
    # The one-page file numbers its only page 0; store the real 0-based index.
    routes = [{**r, "page_index": page_number - 1} for r in result.get("page_routes", [])]
    warnings = [
        {**w, "page_index": page_number - 1} if "page_index" in w else dict(w)
        for w in result.get("warnings", [])
    ]
    return (
        state,
        reason,
        str(result.get("markdown", "")),
        {
            "route": routes[0].get("route") if routes else None,
            "route_reason": routes[0].get("reason") if routes else None,
            "page_routes": routes,
            "warnings": warnings,
            "confidence": result.get("confidence", {}).get("document"),
            "call_counts": result.get("call_counts", {}),
        },
    )


class ParseRunner(Protocol):
    """Runs one parse out of process under a deadline (``page_worker.ParseWorker``).

    ``run`` returns the worker's reply, or raises ``WorkerTimeout`` (the worker
    was killed and reaped) or ``WorkerDied`` (it died on its own).
    """

    @property
    def pid(self) -> int | None: ...

    def run(self, request: Request, deadline_s: float) -> Reply: ...


def _process_page(
    store: Store,
    work_id: str,
    page_number: int,
    escalate: bool,
    max_attempts: int,
    *,
    runner: ParseRunner | None = None,
    kind: str = "pdf",
) -> dict[str, Any]:
    """Parse one page and write its record.

    The attempt counter is written BEFORE the work, so a page that kills the
    process is still counted. Never raises for a page-level failure: an exception
    from the probe or the parse becomes a ``failed`` record with its reason. A
    store error (fetching the page, writing the result) is not a page result: it
    propagates, and the page stays ``pending`` with its attempt counted.

    A page that already has a record is returned as stored, without touching the
    attempt counter or parsing: a stored result is never replaced by a page task. A
    stored record that cannot be read raises ``PageRecordError`` before any parse.

    ``runner`` None parses in this process (the local executor). With a runner
    (the page service's parse worker) the parse runs there under
    ``deadline_for(kind)``: past the deadline the worker is killed and ONE
    ``failed`` record with ``reason_code`` ``timeout`` is written (first write
    wins: a record another pod wrote meanwhile is returned instead). A worker that
    dies on its own writes no record and raises ``WorkerDied``; the attempt stays
    counted, and the caller's retry path decides.

    Before either, the cost guard (spec D7, DOCX only) runs here, in the calling
    process, after the attempt is counted: a rejection is ONE ``failed`` record
    (``zip_bomb`` or ``too_complex``, with its ``limits``) and the unit never
    reaches the parser.
    """
    existing = _load_record_checked(store, work_id, page_number)
    if existing is not None:
        return existing
    attempt = _read_attempts(store, work_id, page_number) + 1
    if attempt > max_attempts:
        return _finalize_exhausted(store, work_id, page_number, kind)
    _put_text(store, _attempts_key(work_id, page_number), str(attempt))

    with store.local_file(_unit_key(work_id, {"kind": kind}, page_number)) as unit_path:
        guard_started = time.perf_counter()
        rejection = _guard_rejection(kind, unit_path)
        if rejection is not None:
            guard_code, detail, guard_limits = rejection
            logger.error("page %d: refused by the %s guard: %s", page_number, kind, guard_code)
            record = _write_record(
                store,
                work_id,
                page_number,
                STATE_FAILED,
                f"{guard_code}: {detail}",
                "",
                attempt,
                {
                    "elapsed_seconds": round(time.perf_counter() - guard_started, 3),
                    "parse_worker_pid": None,
                },
                reason_code=guard_code,
                limits=guard_limits,
                kind=kind,
            )
            observe_unit(record, work_id=work_id, input_bytes=unit_path.stat().st_size)
            return record
        if runner is not None:
            return _run_in_worker(
                store, work_id, page_number, escalate, attempt, runner, kind, unit_path
            )
        started = time.perf_counter()
        input_bytes = unit_path.stat().st_size
        try:
            state, reason, reason_code, markdown, reply_extra = parse_unit(
                kind, unit_path, page_number, escalate, output_cap_bytes(kind)
            )
        except Exception as exc:  # noqa: BLE001 — a page error is a record, not a crash
            # Exception text may quote the document: the type in the log, and at
            # most 500 characters in the record, as the parse worker does (D9).
            logger.error("page %d: parse raised %s", page_number, type(exc).__name__)
            state, reason, reason_code, markdown, reply_extra = _exception_reply(exc)
        extra = dict(reply_extra)
        limits = extra.pop("limits", None)
        extra["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        extra["parse_worker_pid"] = os.getpid()
        record = _write_record(
            store,
            work_id,
            page_number,
            state,
            reason,
            markdown,
            attempt,
            extra,
            reason_code=reason_code,
            limits=limits,
            kind=kind,
        )
        observe_unit(record, work_id=work_id, input_bytes=input_bytes)
        return record


def _run_in_worker(
    store: Store,
    work_id: str,
    page_number: int,
    escalate: bool,
    attempt: int,
    runner: ParseRunner,
    kind: str,
    unit_path: Path,
) -> dict[str, Any]:
    """Step 5 of spec D4: parse in the worker under the deadline, then write the record.

    ``WorkerDied`` propagates with no record written.
    """
    deadline = deadline_for(kind)
    parse_worker_pid = runner.pid
    # The worker applies the output cap before it replies (none for a PDF page).
    request: Request = (kind, str(unit_path), page_number, escalate, output_cap_bytes(kind))
    input_bytes = unit_path.stat().st_size
    started = time.perf_counter()
    try:
        state, reason, reason_code, markdown, extra = runner.run(request, deadline)
    except WorkerTimeout:
        logger.error(
            "page %d: no result after %d s (%s); worker killed", page_number, deadline, kind
        )
        record = _write_record(
            store,
            work_id,
            page_number,
            STATE_FAILED,
            f"timeout: no result after {deadline} s ({kind})",
            "",
            attempt,
            {
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "parse_worker_pid": None,
            },
            reason_code="timeout",
            limits={"deadline_s": deadline},
            kind=kind,
        )
        observe_unit(record, work_id=work_id, input_bytes=input_bytes)
        return record
    fields = dict(extra)
    limits = fields.pop("limits", None)
    fields["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    fields["parse_worker_pid"] = parse_worker_pid
    record = _write_record(
        store,
        work_id,
        page_number,
        state,
        reason,
        markdown,
        attempt,
        fields,
        reason_code=reason_code,
        limits=limits,
        kind=kind,
    )
    observe_unit(record, work_id=work_id, input_bytes=input_bytes)
    return record


@dataclass(frozen=True)
class PageArchives:
    """A page's archived (replaced) results, oldest first.

    ``count`` is how many archives exist now; ``last_number`` is the highest K among
    them. They differ once an older archive has expired, and a new archive must be
    numbered from ``last_number``, never from ``count``.
    """

    keys: list[str]
    count: int
    last_number: int = 0


def page_archives(store: Store, work_id: str, page_number: int) -> PageArchives:
    """The archived results of one page: ``results/NNNN.attempt-K.json``, by K.

    Each archive is one earlier non-ok result that a re-run replaced, so the count
    is the number of times the page has been re-driven (fewer, if any expired).
    """
    numbered = sorted(
        (k, key)
        for key in store.list_keys(_archive_prefix(work_id, page_number))
        if (k := _archive_number(key, page_number)) is not None
    )
    keys = [key for _k, key in numbered]
    return PageArchives(keys, len(keys), numbered[-1][0] if numbered else 0)


def _archive_for_retry(store: Store, work_id: str, page_number: int) -> bool:
    """Move a non-ok record aside so the page can run again; return whether it should.

    Copy to ``results/NNNN.attempt-K.json`` (write-if-absent), delete the ``.md``
    copy, delete the record — in that order, so running it again after a crash at
    any point completes it: when the newest archive is byte-identical to the
    record the copy is already done and no second archive is made.

    The record is re-read here and archived only while it is still not ok: the
    caller decided from an earlier read, and another run of the same content (under any name) may have
    written an ok result since. Returns False, archiving nothing, when the record
    is now ok or was replaced while being archived; True when the page needs a run
    (including when the record is already gone). The store has no conditional
    delete, so a replacement landing between the last check and the delete is not
    caught: one writer per work id (per content) is still assumed.
    """
    record_key = _record_key(work_id, page_number)
    found = _read_record(store, work_id, page_number)
    if found is None:
        return True
    data, record = found
    if record.get("state") == STATE_OK:
        return False
    archives = page_archives(store, work_id, page_number)
    if not archives.count or store.read(archives.keys[-1]) != data:
        archive_key = _archive_key(work_id, page_number, archives.last_number + 1)
        if not store.write_if_absent(archive_key, data) and store.read(archive_key) != data:
            raise PageRecordError(
                f"page {page_number}: {store.location(archive_key)} already holds a different "
                "result; the record was not removed"
            )
    try:
        if store.read(record_key) != data:
            return False
    except StoreNotFoundError:
        return True
    store.delete(_markdown_key(work_id, page_number))
    store.delete(record_key)
    return True


def _finalize_exhausted(
    store: Store, work_id: str, page_number: int, kind: str | None = None
) -> dict[str, Any]:
    """Record a page that used every attempt without producing a result.

    ``kind`` None reads it from the manifest (the remote executor's caller has none).
    """
    existing = _load_record_checked(store, work_id, page_number)
    if existing is not None:
        return existing
    attempts = _read_attempts(store, work_id, page_number)
    if kind is None:
        manifest = _read_json(store, _manifest_key(work_id))
        kind = manifest_kind(manifest if isinstance(manifest, dict) else {})
    return _write_record(
        store,
        work_id,
        page_number,
        STATE_FAILED,
        f"no_result_after_{attempts}_attempts: the worker died or was killed on this page",
        "",
        attempts,
        kind=kind,
    )


# ---------------------------------------------------------------------------
# Running pages
# ---------------------------------------------------------------------------


def _init_worker(threads: int) -> None:
    """Worker start-up: bound the thread count and reuse one Docling converter."""
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))
    enable_converter_reuse()


# One store per worker process, opened on the first page it is given.
_WORKER_STORES: dict[tuple[str, str | None], Store] = {}


def _process_page_task(
    location: str,
    region: str | None,
    work_id: str,
    page_number: int,
    escalate: bool,
    max_attempts: int,
    kind: str = "pdf",
) -> dict[str, Any]:
    """``_process_page`` for a worker process, which opens its own store."""
    store = _WORKER_STORES.get((location, region))
    if store is None:
        store = _WORKER_STORES[(location, region)] = open_store(location, region=region)
    return _process_page(store, work_id, page_number, escalate, max_attempts, kind=kind)


def _run_pool(
    location: str,
    region: str | None,
    work_id: str,
    pages: list[int],
    workers: int,
    escalate: bool,
    max_attempts: int,
    kind: str = "pdf",
) -> set[int]:
    """Run ``pages`` in a process pool; return the pages whose task returned.

    A page missing from the returned set means the pool broke (a worker died)
    before that page's task came back.
    """
    workers = max(1, min(workers, len(pages)))
    threads = max(1, (os.cpu_count() or 1) // workers)
    finished: set[int] = set()
    with ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_init_worker,
        initargs=(threads,),
    ) as pool:
        futures = {
            pool.submit(
                _process_page_task, location, region, work_id, n, escalate, max_attempts, kind
            ): n
            for n in pages
        }
        for future in as_completed(futures):
            page_number = futures[future]
            try:
                record = future.result()
            except BrokenProcessPool:
                continue
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "page %d: task raised %s: %s",
                    page_number,
                    type(exc).__name__,
                    truncate(str(exc)),
                )
                finished.add(page_number)
                continue
            finished.add(page_number)
            logger.info(
                "page %d: %s%s",
                page_number,
                record.get("state"),
                f" ({truncate(record.get('reason'))})" if record.get("reason") else "",
            )
    return finished


def _run_pages(
    store: Store,
    location: str,
    region: str | None,
    work_id: str,
    pages: list[int],
    workers: int,
    escalate: bool,
    max_attempts: int,
    kind: str = "pdf",
) -> None:
    """Process ``pages`` once each, surviving worker deaths.

    ``workers <= 0`` runs in this process on ``store``, one page at a time
    (debugging). Otherwise pages run in a pool whose workers each open the store at
    ``location``. When a worker dies the pool breaks; the pages that had started
    are then re-run ONE PER POOL so a page that keeps killing its worker only uses
    up its own attempts, and the rest go back to a normal pool.
    """
    if workers <= 0:
        enable_converter_reuse()
        for page_number in pages:
            _process_page(store, work_id, page_number, escalate, max_attempts, kind=kind)
        return

    remaining = list(pages)
    while remaining:
        before = {n: _read_attempts(store, work_id, n) for n in remaining}
        finished = _run_pool(
            location, region, work_id, remaining, workers, escalate, max_attempts, kind
        )
        remaining = [n for n in remaining if n not in finished]
        if not remaining:
            return

        in_doubt = [n for n in remaining if _read_attempts(store, work_id, n) > before[n]]
        logger.error(
            "a worker died; re-running %d in-flight page(s) in isolation: %s",
            len(in_doubt),
            in_doubt,
        )
        if not in_doubt:
            raise RuntimeError(
                f"worker pool broke before any page started in "
                f"{store.location(_work_prefix(work_id))}"
            )
        for page_number in in_doubt:
            while True:
                if _read_attempts(store, work_id, page_number) >= max_attempts:
                    _finalize_exhausted(store, work_id, page_number, kind)
                    break
                if _run_pool(
                    location, region, work_id, [page_number], 1, escalate, max_attempts, kind
                ):
                    break
        remaining = [n for n in remaining if n not in in_doubt]


# ---------------------------------------------------------------------------
# Running pages on a page service (KServe InferenceService)
# ---------------------------------------------------------------------------

EXECUTOR_LOCAL = "local"
EXECUTOR_KSERVE = "kserve"
KSERVE_MODEL_NAME = "page-parser"

# 429: the pod is parsing another page; 503: the model is not ready yet. Neither
# reached _process_page, so neither costs an attempt.
_REMOTE_BUSY_CODES = (429, 503)
_REMOTE_BUSY_DEADLINE = 3600.0
# Failed requests allowed beyond max_attempts before a page that never starts
# (no attempt counted) is reported instead of retried forever.
_REMOTE_EXTRA_ERRORS = 5
_sleep = time.sleep  # replaced in tests


class RemotePageError(RuntimeError):
    """The page service refused a page for a reason a retry cannot fix, or never took it."""


def kserve_predict_url(endpoint: str) -> str:
    """The V1 predict URL of the page model at ``endpoint`` (``http://host[:port]``)."""
    return f"{endpoint.rstrip('/')}/v1/models/{KSERVE_MODEL_NAME}:predict"


def process_page_request(
    store: Store,
    work_id: str,
    page_number: int,
    escalate: bool,
    max_attempts: int,
    *,
    runner: ParseRunner | None = None,
) -> dict[str, Any]:
    """Serve one unit request: the page service's side of the remote executor.

    The file must already be taken in under ``store``, under this process's
    pipeline config for its kind: the work id's config hash must be the hash of
    some kind's ``pipeline_config``, and the manifest's config must equal
    ``pipeline_config(manifest kind)``. A unit parsed with other settings than
    the ones pinned at intake would silently mix outputs, so a mismatch raises
    ``PipelineConfigMismatch``. A kind missing from this pod's
    ``PARSER_ENABLED_KINDS`` raises ``KindNotEnabled``. A page number outside the
    file raises ``ValueError``. The unit is routed on the manifest's ``kind``.
    Otherwise this is
    ``_process_page``: a page already recorded is returned as stored, and a
    page-level failure is a ``failed`` record, never an exception. With ``runner``
    (the page service's parse worker) the parse runs there under the deadline, and
    a worker death raises ``WorkerDied`` with no record written.

    Returns a summary of the record; the record itself is in the store.
    """
    requested_config = work_id.rpartition("/")[2]
    known = {config_hash(pipeline_config(kind)): kind for kind in KINDS}
    if requested_config not in known:
        raise PipelineConfigMismatch(
            f"{work_id} is work for config {requested_config} but this page service runs "
            f"none of that hash (pdf: {config_hash()} {pipeline_config()})"
        )
    manifest = _read_json(store, _manifest_key(work_id))
    if not isinstance(manifest, dict):
        raise ValueError(f"{store.location(_work_prefix(work_id))} has no manifest; split it first")
    kind = manifest_kind(manifest)
    if kind not in KIND_EXTENSIONS:
        raise ValueError(f"{work_id} has a manifest of unknown kind {kind!r}")
    if known[requested_config] != kind or manifest.get("config") != pipeline_config(kind):
        raise PipelineConfigMismatch(
            f"{work_id} was taken in with config {manifest.get('config')} but this page "
            f"service runs {pipeline_config(kind)} for {kind}"
        )
    if kind not in enabled_kinds():
        raise KindNotEnabled(
            f"{work_id} is a {kind} file, and {kind} is not in this page service's "
            f"{ENABLED_KINDS_ENV} ({','.join(enabled_kinds())})"
        )
    if not 1 <= page_number <= int(manifest["page_count"]):
        raise ValueError(f"{work_id} has {manifest['page_count']} pages, not page {page_number}")
    record = _process_page(
        store, work_id, page_number, escalate, max_attempts, runner=runner, kind=kind
    )
    return {
        "work_id": work_id,
        "kind": kind,
        "page": page_number,
        "state": record.get("state"),
        "reason": record.get("reason"),
        "reason_code": record.get("reason_code"),
        "attempt": record.get("attempt"),
        "route": record.get("route"),
        "elapsed_seconds": record.get("elapsed_seconds"),
        "worker_host": record.get("worker_host"),
    }


def _post_page(
    url: str, work_id: str, page_number: int, escalate: bool, max_attempts: int, timeout: float
) -> tuple[int, dict[str, Any] | None, str]:
    """POST one page; return ``(status, body, error detail)``.

    One connection per request (``Connection: close``): a kept-alive connection
    stays on the pod it first reached, so pods added by a scale-out would get no
    pages. A transport error (refused, reset, timeout) raises.
    """
    instance = {
        "work_id": work_id,
        "page": page_number,
        "escalate": escalate,
        "max_attempts": max_attempts,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps({"instances": [instance]}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Connection": "close"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read()), ""
    except urllib.error.HTTPError as exc:
        return exc.code, None, exc.read().decode("utf-8", "replace")[:500]


def _remote_page(
    store: Store,
    url: str,
    work_id: str,
    page_number: int,
    escalate: bool,
    max_attempts: int,
    stop: threading.Event,
    request_timeout: float,
) -> dict[str, Any]:
    """Send one page until it has a result; return the summary or the record.

    Each request waits at most ``request_timeout`` seconds
    (``PARSER_REMOTE_REQUEST_TIMEOUT_S``), which settings keep above every parse
    deadline, so a page that times out on the service is answered, not resent.

    - 200: the service parsed the page (or returned its stored record).
    - 429/503: the service did not start it. Retried with jittered backoff at no
      attempt cost, for at most ``busy_deadline`` seconds of continuous refusals.
    - Any other 4xx: a request the service will never accept (config mismatch, a
      page or file it does not know). Raises ``RemotePageError``.
    - A transport error or other 5xx: the page may have started on a pod that died.
      The store decides: a record means the page is done; an attempt counter at
      the limit means the page is finalized ``failed``; otherwise it is sent again.
      A page whose requests keep failing without it ever starting raises.
    """
    timeout, busy_deadline = request_timeout, _REMOTE_BUSY_DEADLINE
    busy_since: float | None = None
    backoff = 0.5
    errors = 0
    while True:
        if stop.is_set():
            raise RemotePageError(f"page {page_number}: stopped after another page failed")
        try:
            status, body, detail = _post_page(
                url, work_id, page_number, escalate, max_attempts, timeout
            )
        except (OSError, http.client.HTTPException) as exc:  # URLError is an OSError
            status, body, detail = 0, None, f"{type(exc).__name__}: {exc}"

        if status == 200 and body and body.get("predictions"):
            return dict(body["predictions"][0])
        if status in _REMOTE_BUSY_CODES:
            now = time.monotonic()
            busy_since = now if busy_since is None else busy_since
            if now - busy_since > busy_deadline:
                raise RemotePageError(
                    f"page {page_number}: the page service refused it for {busy_deadline:.0f}s "
                    f"({status}: {detail})"
                )
            _sleep(random.uniform(0.5, 1.0) * backoff)
            backoff = min(backoff * 2, 8.0)
            continue
        if 400 <= status < 500:
            raise RemotePageError(f"page {page_number}: the page service refused it ({status}: {detail})")

        busy_since, backoff = None, 0.5
        errors += 1
        logger.warning("page %d: request failed (%s); checking the store", page_number, detail or status)
        record = _load_record_checked(store, work_id, page_number)
        if record is not None:
            return record
        if _read_attempts(store, work_id, page_number) >= max_attempts:
            return _finalize_exhausted(store, work_id, page_number)
        if errors > max_attempts + _REMOTE_EXTRA_ERRORS:
            raise RemotePageError(
                f"page {page_number}: {errors} requests failed without the page starting; "
                f"last: {detail or status}"
            )
        _sleep(random.uniform(0.5, 2.0))


def _run_pages_remote(
    store: Store,
    endpoint: str,
    work_id: str,
    pages: list[int],
    concurrency: int,
    escalate: bool,
    max_attempts: int,
    request_timeout: float,
) -> None:
    """Process ``pages`` once each on the page service at ``endpoint``.

    Up to ``concurrency`` requests are in flight; the service admits one page per
    pod and refuses the rest with 429, so the queue lives here and the service
    scales on its own load. The first ``RemotePageError`` stops the other pages
    and propagates; pages without a record stay ``pending``.
    """
    url = kserve_predict_url(endpoint)
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(pages)))) as pool:
        futures = {
            pool.submit(
                _remote_page,
                store,
                url,
                work_id,
                n,
                escalate,
                max_attempts,
                stop,
                request_timeout,
            ): n
            for n in pages
        }
        try:
            for future in as_completed(futures):
                page_number = futures[future]
                summary = future.result()
                logger.info(
                    "page %d: %s%s%s",
                    page_number,
                    summary.get("state"),
                    f" ({summary.get('reason')})" if summary.get("reason") else "",
                    f" on {summary['worker_host']}" if summary.get("worker_host") else "",
                )
        except BaseException:
            stop.set()
            for future in futures:
                future.cancel()
            raise


# ---------------------------------------------------------------------------
# Status and stitch
# ---------------------------------------------------------------------------


def build_status(
    store: Store,
    work_id: str,
    file_id: str,
    manifest: dict[str, Any],
    max_attempts: int,
    source_name: str | None = None,
    *,
    records: dict[int, dict[str, Any] | None] | None = None,
    write: bool = True,
    limits_now: Mapping[str, Any] | None = None,
    detected_type: str | None = None,
) -> dict[str, Any]:
    """Rebuild ``out/<file_id>/status.json`` from the page records under ``work/<work_id>``.

    ``records`` are the page records already read (default: read them here);
    ``write=False`` returns the status without storing it, for a caller that
    writes it last.

    ``source_name`` is the name of the file being run (default: the name the
    content was first split under, from the manifest). ``detected_type`` is the
    intake sniff's result for it (None: not sniffed by this caller).

    A page is ``retryable`` when it is not ok, is under the attempt limit, and is
    not final (``_is_final`` against ``limits_now``, default ``current_limits``).
    The file's ``kind`` and ``unit`` come from the manifest; its ``flags`` are the
    union of the units' flags.
    """
    kind = manifest_kind(manifest)
    if limits_now is None:
        limits_now = current_limits(kind)
    flags: set[str] = set()
    pages: list[dict[str, Any]] = []
    for page_number in range(1, int(manifest["page_count"]) + 1):
        record = (records or {}).get(page_number) if records is not None else _load_record(
            store, work_id, page_number
        )
        attempts = _read_attempts(store, work_id, page_number)
        state = record["state"] if record is not None else STATE_PENDING
        if record is not None:
            flags.update(_flag_list(record.get("flags")))
        pages.append(
            {
                "page": page_number,
                "state": state,
                "reason": record.get("reason") if record is not None else None,
                "reason_code": record.get("reason_code") if record is not None else None,
                "route": record.get("route") if record is not None else None,
                "attempts": attempts,
                "retryable": state != STATE_OK
                and attempts < max_attempts
                and not (record is not None and _is_final(record, limits_now)),
            }
        )

    counts = {
        state: sum(1 for p in pages if p["state"] == state)
        for state in (STATE_OK, STATE_DEGRADED, STATE_FAILED, STATE_PENDING)
    }
    if counts[STATE_OK] == len(pages):
        file_state = FILE_COMPLETE
    elif counts[STATE_PENDING]:
        file_state = FILE_IN_PROGRESS
    else:
        file_state = FILE_FAILED

    status = {
        "state": file_state,
        "source_name": source_name or manifest["source_name"],
        "source_sha256": manifest["source_sha256"],
        "work_id": work_id,
        "kind": kind,
        "unit": unit_of(kind),
        "detected_type": detected_type,
        "flags": sorted(flags),
        "page_count": manifest["page_count"],
        "counts": counts,
        "not_ok_pages": [p for p in pages if p["state"] != STATE_OK],
        "pages": pages,
        "updated_at": datetime.now(tz=UTC).isoformat(),
    }
    if write:
        _put_json(store, _status_key(file_id), status)
    return status


def _stitch_markdown(
    store: Store,
    work_id: str,
    status: dict[str, Any],
    records: dict[int, dict[str, Any] | None] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Join the page markdown in page order, a marker line before each page.

    Returns the markdown and one metadata entry per page with its character span.
    Pages that are not ``ok`` (partial stitch only) get a marker naming the gap and
    no content.
    """
    parts: list[str] = []
    meta: list[dict[str, Any]] = []
    offset = 0
    for page in status["pages"]:
        page_number = page["page"]
        record = (
            records.get(page_number) if records is not None else _load_record(store, work_id, page_number)
        ) or {}
        if page["state"] == STATE_OK:
            marker = PAGE_MARKER.format(number=page_number)
            body = str(record.get("markdown", "")).strip()
        else:
            marker = PAGE_MARKER.format(number=f"{page_number} (missing: {page['state']})")
            body = ""
        block = f"{marker}\n\n{body}" if body else marker
        if parts:
            offset += 2  # the "\n\n" joiner
        meta.append(
            {
                "page": page_number,
                "state": page["state"],
                "reason": page["reason"],
                "route": record.get("route"),
                "route_reason": record.get("route_reason"),
                "confidence": record.get("confidence"),
                "n_chars": len(body),
                "char_span": [offset, offset + len(block)],
            }
        )
        parts.append(block)
        offset += len(block)
    return "\n\n".join(parts) + "\n", meta


def stitch(
    store: Store,
    work_id: str,
    file_id: str,
    manifest: dict[str, Any],
    status: dict[str, Any],
    partial: bool = False,
    records: dict[int, dict[str, Any] | None] | None = None,
) -> dict[str, str | None]:
    """Write ``out/<file_id>/output.md`` iff every page is ok; optionally a partial document.

    Returns the store keys written: ``{"output": key or None, "partial": key or None}``.

    Fail closed: a file that is not complete gets NO ``output.md``. With
    ``partial=True`` it gets ``output.partial.md`` instead, with a gap marker where
    each missing page belongs. Re-runnable: a pure function of the page records.
    """
    keys: dict[str, str | None] = {"output": None, "partial": None}
    complete = status["state"] == FILE_COMPLETE
    if not complete and not partial:
        return keys

    markdown, pages_meta = _stitch_markdown(store, work_id, status, records)
    call_counts: dict[str, int] = {}
    for page in status["pages"]:
        record = (
            records.get(page["page"]) if records is not None else _load_record(store, work_id, page["page"])
        ) or {}
        for engine, count in (record.get("call_counts") or {}).items():
            call_counts[engine] = call_counts.get(engine, 0) + int(count)
    metadata = {
        "state": status["state"],
        "source_name": status["source_name"],
        "source_sha256": manifest["source_sha256"],
        "work_id": work_id,
        "kind": manifest_kind(manifest),
        "unit": unit_of(manifest_kind(manifest)),
        "flags": list(status.get("flags", [])),
        "page_count": manifest["page_count"],
        "config": manifest["config"],
        "counts": status["counts"],
        "call_counts": call_counts,
        "pages": pages_meta,
        "stitched_at": datetime.now(tz=UTC).isoformat(),
    }
    stem = "output" if complete else "output.partial"
    _put_text(store, _output_key(file_id, f"{stem}.md"), markdown)
    _put_json(store, _output_key(file_id, f"{stem}.json"), metadata)
    keys["output" if complete else "partial"] = _output_key(file_id, f"{stem}.md")
    return keys


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------

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
        "extension": extension_label(source.name),
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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_file_in_store(
    source: Path,
    location: str,
    region: str | None = None,
    *,
    store: Store | None = None,
    workers: int | None = None,
    escalate: bool = True,
    max_attempts: int | None = None,
    partial: bool = False,
    restart: bool = False,
    executor: str = EXECUTOR_LOCAL,
    endpoint: str | None = None,
    concurrency: int | None = None,
    publish: PublishRequest | None = None,
    request_timeout: float | None = None,
    declared_type: str | None = None,
) -> dict[str, Any]:
    """Take in a file, parse its units in parallel, and stitch when every unit is ok.

    Intake sniffs the file (``sniff.py``) to route it to a kind. A file with no
    kind, or a kind missing from ``PARSER_ENABLED_KINDS``, fails alone with
    ``unsupported_type``; a single-unit file over its kind's raw cap fails with
    ``too_large``. Neither writes under ``work/``: only ``out/<file id>/status.json``.
    A PDF is split into pages; every other kind is one unit, the file itself.

    Args:
        source: The file to parse (a local file, any name: the content decides).
        location: The store location: a local directory or ``s3://bucket[/prefix]``.
            Worker processes each open their own store from it.
        region: AWS region for an S3 location (never read from the environment here).
        store: An already opened store for ``location`` (default: open it). It does
            the parent's reads and writes, and every page's with ``workers=0``.
        workers: Worker processes (default ``default_workers()``); ``0`` runs the
            pages in this process, one at a time.
        escalate: Passed to ``parse_to_markdown``. With False, a page the gate
            promotes keeps its Docling text and is recorded as ``degraded``.
        max_attempts: Attempt limit per page (default ``default_max_attempts()``).
        partial: Also write ``output.partial.md`` when the file is not complete.
        restart: Delete everything stored for this file first and start again.
        executor: ``"local"`` (worker processes here) or ``"kserve"``: each page is
            sent to the page service at ``endpoint``, which writes its record to
            the same store. Split, archive, status and stitch stay here either way.
        endpoint: The page service base URL (``http://host[:port]``), for kserve.
        concurrency: Page requests in flight, for kserve (default ``workers``).
        request_timeout: Seconds one page request may take, for kserve (default
            ``remote_request_timeout()``, from ``PARSER_REMOTE_REQUEST_TIMEOUT_S``).
        publish: Also publish the result to ``published/`` under this doc id
            (``publish.py``): a complete file as a version, an incomplete one as a
            ``partial`` version when the request allows it, else a ``failed`` event.
        declared_type: The input's declared content type (an S3 object's
            ``Content-Type``), or None; used by the sniff's text rule.

    Returns:
        ``{"state", "file_id", "work_id", "kind", "unit", "detected_type", "flags", "work_dir",
        "out_dir", "page_count", "counts",
        "not_ok_pages", "pages", "status_path", "output_path", "partial_path",
        "output_key", "partial_key", "published", "elapsed_seconds"}``. ``state`` is ``complete``
        only when every page is ok; ``output_path`` is None otherwise. ``work_dir``
        (the ``work/<work id>`` prefix, shared by every name with the same bytes), ``out_dir`` and the ``*_path`` values come
        from the store's ``location`` (a local path or an ``s3://`` URI); the
        ``*_key`` values are store keys.

        An intake rejection returns ``state`` ``failed`` with ``work_id`` and
        ``work_dir`` None and the ``reason_code``; with ``publish`` it writes a
        ``failed`` event (once per refusal, however often the file is re-run).

    Raises on an input that cannot be split at all (missing, a PDF that pypdf
    cannot read, encrypted) and on ``PipelineConfigMismatch``; unit-level failures
    never raise. A store error propagates.
    """
    source = Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if executor not in (EXECUTOR_LOCAL, EXECUTOR_KSERVE):
        raise ValueError(f"executor must be {EXECUTOR_LOCAL} or {EXECUTOR_KSERVE}, got {executor!r}")
    if executor == EXECUTOR_KSERVE and not endpoint:
        raise ValueError("the kserve executor needs the page service endpoint")
    workers = default_workers() if workers is None else workers
    max_attempts = default_max_attempts() if max_attempts is None else max_attempts
    if store is None:
        store = open_store(location, region=region)

    started = time.perf_counter()
    sha256 = _sha256_file(source)
    found = _sniff_source(source, declared_type)
    if found.mismatch:
        logger.warning(
            "%s: the name or declared type disagrees with the content (detected %s, kind %s)",
            source.name,
            found.detected_type,
            found.kind,
        )
    size = source.stat().st_size
    rejection = _intake_rejection(found, size)
    file_id = _file_id(source.stem, sha256)
    event_fields = _file_event_fields(source, found, file_id, size, publish)
    log_event("file_intake", **event_fields, reason_code=rejection[0] if rejection else None)
    if rejection is not None:
        if rejection[0] == INTAKE_TOO_LARGE:
            log_event(
                "guard_reject",
                **event_fields,
                reason_code=INTAKE_TOO_LARGE,
                limits={"raw_cap_bytes": raw_cap_bytes(str(found.kind))},
            )
        refused = _reject_at_intake(store, source, sha256, found, *rejection, started, publish)
        _log_file_done(event_fields, refused, 0)
        return refused
    kind = found.kind
    assert kind is not None
    config = pipeline_config(kind)
    work_id = _work_id(sha256, config_hash(config))
    if restart:
        # The work is shared by every name with these bytes: they all start again.
        store.delete_prefix(f"{_work_prefix(work_id)}/")
        store.delete_prefix(f"{_out_prefix(file_id)}/")
    manifest = _intake(store, work_id, source, kind)
    limits_now = current_limits(kind)

    todo: list[int] = []
    for page_number in range(1, int(manifest["page_count"]) + 1):
        # An unparseable record raises here, before any page is parsed.
        record = _load_record_checked(store, work_id, page_number)
        if record is not None and record.get("state") == STATE_OK:
            continue
        # A deterministic failure under unchanged limits is final: never re-run.
        if record is not None and _is_final(record, limits_now):
            continue
        if _read_attempts(store, work_id, page_number) >= max_attempts:
            _finalize_exhausted(store, work_id, page_number, kind)
            continue
        # failed or degraded, under the limit: archive it so the page's next result
        # is again the first write. Done here, never in a page task. The step says
        # no when the page turned ok in the meantime.
        if record is not None and not _archive_for_retry(store, work_id, page_number):
            continue
        todo.append(page_number)

    remote = executor == EXECUTOR_KSERVE
    in_flight = (concurrency or max(1, workers)) if remote else workers
    logger.info(
        "%s: %s, %d %s(s), %d to process, %d already done, %s=%d",
        source.name,
        kind,
        manifest["page_count"],
        unit_of(kind),
        len(todo),
        int(manifest["page_count"]) - len(todo),
        f"kserve {endpoint} concurrency" if remote else "workers",
        in_flight,
    )
    if todo and remote:
        assert endpoint is not None
        timeout = remote_request_timeout() if request_timeout is None else request_timeout
        _run_pages_remote(
            store, endpoint, work_id, todo, in_flight, escalate, max_attempts, timeout
        )
    elif todo:
        _run_pages(store, location, region, work_id, todo, workers, escalate, max_attempts, kind)

    # Records are read once and shared by the status, the stitch and the publish.
    # status.json is written LAST, so it never says complete before the outputs exist.
    records = {
        n: _load_record(store, work_id, n) for n in range(1, int(manifest["page_count"]) + 1)
    }
    status = build_status(
        store,
        work_id,
        file_id,
        manifest,
        max_attempts,
        source.name,
        records=records,
        write=False,
        limits_now=limits_now,
        detected_type=found.detected_type,
    )
    keys = stitch(store, work_id, file_id, manifest, status, partial=partial, records=records)
    published = None
    if publish is not None:
        markdown, pages_meta = _stitch_markdown(store, work_id, status, records)
        reason_codes = {p["page"]: p["reason_code"] for p in status["pages"]}
        first = records.get(1) or {}
        published = publish_document(
            store,
            publish,
            state=status["state"],
            source_name=source.name,
            source_sha256=manifest["source_sha256"],
            work_id=work_id,
            config=manifest["config"],
            config_hash=config_hash(manifest["config"]),
            page_count=int(manifest["page_count"]),
            markdown=markdown,
            pages_meta=[{**m, "reason_code": reason_codes[m["page"]]} for m in pages_meta],
            page_texts={
                n: str(r.get("markdown", ""))
                for n, r in records.items()
                if r is not None and r.get("state") == STATE_OK
            },
            kind=kind,
            unit=unit_of(kind),
            flags=status["flags"],
            handler_version=manifest["config"].get("handler_version"),
            # An email's headers and attachment list, from its one unit's record.
            email=first.get("email") if kind in EMAIL_KINDS else None,
        )
    _put_json(store, _status_key(file_id), status)
    if status["state"] != FILE_COMPLETE:
        logger.error(
            "%s: %s — pages not ok: %s",
            source.name,
            status["state"],
            [(p["page"], p["state"], truncate(p["reason"])) for p in status["not_ok_pages"]],
        )
    result: dict[str, Any] = {
        "state": status["state"],
        "file_id": file_id,
        "work_id": work_id,
        "kind": kind,
        "unit": unit_of(kind),
        "detected_type": found.detected_type,
        "flags": status["flags"],
        "work_dir": store.location(_work_prefix(work_id)),
        "out_dir": store.location(_out_prefix(file_id)),
        "page_count": manifest["page_count"],
        "counts": status["counts"],
        "not_ok_pages": status["not_ok_pages"],
        "pages": status["pages"],
        "status_path": store.location(_status_key(file_id)),
        "output_path": store.location(keys["output"]) if keys["output"] else None,
        "partial_path": store.location(keys["partial"]) if keys["partial"] else None,
        "output_key": keys["output"],
        "partial_key": keys["partial"],
        "published": published,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    output_chars = sum(int(r.get("n_chars") or 0) for r in records.values() if r is not None)
    _log_file_done(event_fields, result, output_chars)
    return result


# The old name, kept for the scripts and tests that use it.
parse_pdf_pages_in_store = parse_file_in_store


def parse_pdf_pages(
    source: Path,
    work_root: Path,
    workers: int | None = None,
    escalate: bool = True,
    max_attempts: int | None = None,
    partial: bool = False,
    restart: bool = False,
) -> dict[str, Any]:
    """``parse_file_in_store`` on a local directory (any kind, despite the name).

    A thin wrapper for callers that hold a ``Path``: ``work_root`` is the store
    location, holding ``work/<work id>/`` per content and ``out/<file id>/`` per source file.
    """
    return parse_file_in_store(
        source,
        str(Path(work_root).resolve()),
        workers=workers,
        escalate=escalate,
        max_attempts=max_attempts,
        partial=partial,
        restart=restart,
    )
