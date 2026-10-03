"""
page_pipeline.py — split-first, per-page PDF parsing with resume and a fail-closed stitch.

``parse_pdf_pages_in_store(source, location)`` is the page-level counterpart of
``markdown_pipeline.parse_to_markdown``; ``parse_pdf_pages(source, work_root)`` is
the same on a local directory:

  1. SPLIT   store ``source.pdf``, then the PDF's one-page files (pypdf), then
             ``manifest.json`` LAST (source hash, true page count, pinned pipeline
             config).
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
reached.

Keys under the store location (file id = ``<stem>-<sha256[:16]>``). ``work/`` is
the part a lifecycle rule may expire; ``out/`` is self-contained:

    work/<file id>/source.pdf                    copy of the original; written first
    work/<file id>/pages/0001.pdf …              the one-page files (1-based)
    work/<file id>/manifest.json                 written last by the split
    work/<file id>/attempts/0001 …               attempt counter, written before each try
    work/<file id>/results/0001.json             the page's record; exists = page finished
    work/<file id>/results/0001.md               readable copy of the page's markdown
    work/<file id>/results/0001.attempt-1.json   an archived non-ok record
    out/<file id>/status.json                    summary rebuilt from the records
    out/<file id>/output.md                      only when every page is ok
    out/<file id>/output.json                    metadata for output.md
    out/<file id>/output.partial.md, .json       only on request, when not complete

PDF only: office, HTML and image inputs have no page split and stay on
``parse_to_markdown``.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import multiprocessing
import os
import re
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from parser_service.markdown_pipeline import enable_converter_reuse, parse_to_markdown
from parser_service.parser_service import PARSER_VERSION, _sha256_file
from parser_service.render import render_page, text_layer_tokens
from parser_service.route_stats import (
    ROUTE_DOCLING_KEPT,
    ROUTE_TEXTRACT_FALLBACK,
    ROUTE_VLM_FALLBACK,
)
from parser_service.store import LocalStore, Store, StoreNotFoundError, open_store

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


def pipeline_config() -> dict[str, Any]:
    """The settings that change page output. Pinned in the manifest at intake."""
    return {
        "parser_version": PARSER_VERSION,
        "escalation_engine": os.environ.get("PARSER_ESCALATION_ENGINE", "vlm"),
        "vlm_model": os.environ.get("BEDROCK_VLM_MODEL", ""),
        "scan_fastpath": os.environ.get("PARSER_SCAN_FASTPATH", "").lower() in ("1", "true"),
        "arbitration": os.environ.get("PARSER_ESCALATION_ARBITRATION", "").lower() in ("1", "true"),
    }


def strip_page_markers(markdown: str) -> str:
    """Remove the ``==== page N ===`` lines (evaluation gold has no markers)."""
    return _PAGE_MARKER_RE.sub("", markdown)


# ---------------------------------------------------------------------------
# Store keys and small read/write helpers. Only the key helpers know the layout.
# ---------------------------------------------------------------------------


def _name(page_number: int) -> str:
    return f"{page_number:04d}"


def _work_prefix(file_id: str) -> str:
    """Intermediates: the part of a file an S3 lifecycle rule may expire."""
    return f"work/{file_id}"


def _out_prefix(file_id: str) -> str:
    """The final document and its status; self-contained once ``work/`` is gone."""
    return f"out/{file_id}"


def _manifest_key(file_id: str) -> str:
    return f"{_work_prefix(file_id)}/manifest.json"


def _source_key(file_id: str) -> str:
    return f"{_work_prefix(file_id)}/source.pdf"


def _pages_prefix(file_id: str) -> str:
    return f"{_work_prefix(file_id)}/pages/"


def _page_pdf_key(file_id: str, page_number: int) -> str:
    return f"{_work_prefix(file_id)}/pages/{_name(page_number)}.pdf"


def _record_key(file_id: str, page_number: int) -> str:
    return f"{_work_prefix(file_id)}/results/{_name(page_number)}.json"


def _markdown_key(file_id: str, page_number: int) -> str:
    return f"{_work_prefix(file_id)}/results/{_name(page_number)}.md"


def _archive_prefix(file_id: str, page_number: int) -> str:
    return f"{_work_prefix(file_id)}/results/{_name(page_number)}.attempt-"


def _archive_key(file_id: str, page_number: int, k: int) -> str:
    return f"{_archive_prefix(file_id, page_number)}{k}.json"


def _archive_number(key: str, page_number: int) -> int | None:
    """K when ``key`` is this page's ``NNNN.attempt-K.json``, else None."""
    match = re.fullmatch(rf"{_name(page_number)}\.attempt-(\d+)\.json", key.rpartition("/")[2])
    return int(match.group(1)) if match else None


def _attempts_key(file_id: str, page_number: int) -> str:
    return f"{_work_prefix(file_id)}/attempts/{_name(page_number)}"


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


def _read_attempts(store: Store, file_id: str, page_number: int) -> int:
    try:
        return int(store.read(_attempts_key(file_id, page_number)).decode("utf-8").strip())
    except (StoreNotFoundError, ValueError):
        return 0


def _load_record(store: Store, file_id: str, page_number: int) -> dict[str, Any] | None:
    record = _read_json(store, _record_key(file_id, page_number))
    return record if isinstance(record, dict) else None


def _read_record(
    store: Store, file_id: str, page_number: int
) -> tuple[bytes, dict[str, Any]] | None:
    """The page's record as stored bytes and parsed dict, or None when it has none.

    Unlike ``_load_record`` this does not read an unparseable record as "no record":
    it raises ``PageRecordError``, before any work is spent on a page whose result
    could never be written over it.
    """
    key = _record_key(file_id, page_number)
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


def _load_record_checked(store: Store, file_id: str, page_number: int) -> dict[str, Any] | None:
    """``_read_record`` without the bytes: the record, None, or ``PageRecordError``."""
    found = _read_record(store, file_id, page_number)
    return found[1] if found is not None else None


def file_id_for(source: Path) -> str:
    """The id of a source file in the store: ``<stem>-<sha256[:16]>``."""
    return f"{source.stem}-{_sha256_file(source)[:16]}"


def work_dir_for(source: Path, work_root: Path) -> Path:
    """The work directory for a source file: ``work/<stem>-<sha256[:16]>`` under the root."""
    return Path(work_root) / _work_prefix(file_id_for(source))


def _local_store_for(work_dir: Path) -> tuple[LocalStore, str]:
    """The local store and file id that ``work_dir_for`` built ``work_dir`` from."""
    file_id = work_dir.name
    root = work_dir.parent.parent
    if work_dir != root / _work_prefix(file_id):
        raise ValueError(
            f"{work_dir} is not a work directory: expected <work_root>/{_work_prefix(file_id)} "
            "(see work_dir_for)"
        )
    return LocalStore(root), file_id


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


def _restore_expired(store: Store, file_id: str, source: Path, page_count: int) -> None:
    """Re-create page files (and ``source.pdf``) that are gone though the manifest is not.

    An S3 lifecycle rule expires objects one by one, by age, and the page files are
    older than the manifest. The run always has the original in hand, so whatever
    is missing is written again from it. The manifest is not rewritten.
    """
    from pypdf import PdfReader

    if not store.exists(_source_key(file_id)):
        store.write(_source_key(file_id), _read_source(source))
    present = set(store.list_keys(_pages_prefix(file_id)))
    missing = [n for n in range(1, page_count + 1) if _page_pdf_key(file_id, n) not in present]
    if not missing:
        return
    logger.info("%s: re-creating %d missing page file(s): %s", source.name, len(missing), missing)
    reader = PdfReader(str(source))
    for page_number in missing:
        store.write(_page_pdf_key(file_id, page_number), _page_bytes(reader, page_number))


def _split(store: Store, file_id: str, source: Path) -> dict[str, Any]:
    """Split ``source`` into one-page PDFs in the store and return the manifest.

    Order of writes: ``source.pdf`` (a copy of the original), the page files, then
    the manifest LAST, so a crash mid-split leaves no manifest and the split is
    simply redone. Idempotent: when a manifest already exists it is returned as is,
    after checking the pinned config and re-creating any page file that has
    expired. Raises on an unreadable or encrypted PDF, and
    ``PipelineConfigMismatch`` when the file was started under a different config.
    """
    from pypdf import PdfReader  # local import; only needed for PDFs

    config = pipeline_config()
    existing = _read_json(store, _manifest_key(file_id))
    if isinstance(existing, dict):
        if existing.get("config") != config:
            raise PipelineConfigMismatch(
                f"{store.location(_work_prefix(file_id))} was started with config "
                f"{existing.get('config')} but the current config is {config}; finish it "
                "under the original settings or restart it"
            )
        _restore_expired(store, file_id, source, int(existing["page_count"]))
        return existing

    reader = PdfReader(str(source))
    if reader.is_encrypted:
        raise ValueError(f"{source.name} is encrypted and cannot be split")
    page_count = len(reader.pages)
    if page_count == 0:
        raise ValueError(f"{source.name} has no pages")

    store.write(_source_key(file_id), _read_source(source))
    for page_number in range(1, page_count + 1):
        store.write(_page_pdf_key(file_id, page_number), _page_bytes(reader, page_number))

    manifest = {
        "source_name": source.name,
        "source_sha256": _sha256_file(source),
        "page_count": page_count,
        "config": config,
        "created_at": datetime.now(tz=UTC).isoformat(),
    }
    _put_json(store, _manifest_key(file_id), manifest)
    return manifest


def split_pdf(source: Path, work_dir: Path) -> dict[str, Any]:
    """Split ``source`` into one-page PDFs under ``work_dir`` and return the manifest.

    A thin wrapper over a local store, for callers that hold a directory:
    ``work_dir`` must be the directory ``work_dir_for`` returns. See ``_split``.
    """
    store, file_id = _local_store_for(Path(work_dir))
    manifest = _split(store, file_id, Path(source))
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
        logger.warning("blank-page probe failed for %s: %s", page_pdf.name, exc)
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
    file_id: str,
    page_number: int,
    state: str,
    reason: str | None,
    markdown: str,
    attempt: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the page's record (the commit marker), then its readable ``.md`` copy.

    First write wins: the record is written only if the page has none. The writer
    that created it then writes the ``.md`` copy; any other writer gets the stored
    record back and its own result is discarded, so one run's markdown is never
    left beside another run's record.
    """
    markdown_key = _markdown_key(file_id, page_number)
    record: dict[str, Any] = {
        "page": page_number,
        "state": state,
        "reason": reason,
        "markdown": markdown,
        "parser_version": PARSER_VERSION,
        "n_chars": len(markdown),
        "markdown_file": markdown_key.rpartition("/")[2],
        "markdown_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        "attempt": attempt,
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "worker_pid": os.getpid(),
    }
    if extra:
        record.update(extra)
    if store.write_if_absent(_record_key(file_id, page_number), _json_bytes(record)):
        _put_text(store, markdown_key, markdown)
        return record
    stored = _load_record(store, file_id, page_number)
    if stored is None:
        # The one place an unparseable record is not read as "no record": the
        # write was refused because of it, so it cannot be treated as absent.
        raise PageRecordError(
            f"page {page_number}: a result already exists at "
            f"{store.location(_record_key(file_id, page_number))} but cannot be read as a record"
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


def _process_page(
    store: Store, file_id: str, page_number: int, escalate: bool, max_attempts: int
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
    """
    existing = _load_record_checked(store, file_id, page_number)
    if existing is not None:
        return existing
    attempt = _read_attempts(store, file_id, page_number) + 1
    if attempt > max_attempts:
        return _finalize_exhausted(store, file_id, page_number)
    _put_text(store, _attempts_key(file_id, page_number), str(attempt))

    with store.local_file(_page_pdf_key(file_id, page_number)) as page_pdf:
        started = time.perf_counter()
        try:
            state, reason, markdown, extra = _parse_page(page_pdf, page_number, escalate)
        except Exception as exc:  # noqa: BLE001 — a page error is a record, not a crash
            logger.exception("page %d failed", page_number)
            state, reason = STATE_FAILED, f"exception: {type(exc).__name__}: {exc}"
            markdown, extra = "", {}
        extra["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        return _write_record(store, file_id, page_number, state, reason, markdown, attempt, extra)


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


def page_archives(store: Store, file_id: str, page_number: int) -> PageArchives:
    """The archived results of one page: ``results/NNNN.attempt-K.json``, by K.

    Each archive is one earlier non-ok result that a re-run replaced, so the count
    is the number of times the page has been re-driven (fewer, if any expired).
    """
    numbered = sorted(
        (k, key)
        for key in store.list_keys(_archive_prefix(file_id, page_number))
        if (k := _archive_number(key, page_number)) is not None
    )
    keys = [key for _k, key in numbered]
    return PageArchives(keys, len(keys), numbered[-1][0] if numbered else 0)


def _archive_for_retry(store: Store, file_id: str, page_number: int) -> bool:
    """Move a non-ok record aside so the page can run again; return whether it should.

    Copy to ``results/NNNN.attempt-K.json`` (write-if-absent), delete the ``.md``
    copy, delete the record — in that order, so running it again after a crash at
    any point completes it: when the newest archive is byte-identical to the
    record the copy is already done and no second archive is made.

    The record is re-read here and archived only while it is still not ok: the
    caller decided from an earlier read, and another run of the same file may have
    written an ok result since. Returns False, archiving nothing, when the record
    is now ok or was replaced while being archived; True when the page needs a run
    (including when the record is already gone). The store has no conditional
    delete, so a replacement landing between the last check and the delete is not
    caught: one writer per file is still assumed.
    """
    record_key = _record_key(file_id, page_number)
    found = _read_record(store, file_id, page_number)
    if found is None:
        return True
    data, record = found
    if record.get("state") == STATE_OK:
        return False
    archives = page_archives(store, file_id, page_number)
    if not archives.count or store.read(archives.keys[-1]) != data:
        archive_key = _archive_key(file_id, page_number, archives.last_number + 1)
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
    store.delete(_markdown_key(file_id, page_number))
    store.delete(record_key)
    return True


def _finalize_exhausted(store: Store, file_id: str, page_number: int) -> dict[str, Any]:
    """Record a page that used every attempt without producing a result."""
    existing = _load_record_checked(store, file_id, page_number)
    if existing is not None:
        return existing
    attempts = _read_attempts(store, file_id, page_number)
    return _write_record(
        store,
        file_id,
        page_number,
        STATE_FAILED,
        f"no_result_after_{attempts}_attempts: the worker died or was killed on this page",
        "",
        attempts,
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
    file_id: str,
    page_number: int,
    escalate: bool,
    max_attempts: int,
) -> dict[str, Any]:
    """``_process_page`` for a worker process, which opens its own store."""
    store = _WORKER_STORES.get((location, region))
    if store is None:
        store = _WORKER_STORES[(location, region)] = open_store(location, region=region)
    return _process_page(store, file_id, page_number, escalate, max_attempts)


def _run_pool(
    location: str,
    region: str | None,
    file_id: str,
    pages: list[int],
    workers: int,
    escalate: bool,
    max_attempts: int,
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
            pool.submit(_process_page_task, location, region, file_id, n, escalate, max_attempts): n
            for n in pages
        }
        for future in as_completed(futures):
            page_number = futures[future]
            try:
                record = future.result()
            except BrokenProcessPool:
                continue
            except Exception as exc:  # noqa: BLE001
                logger.error("page %d: task raised %s: %s", page_number, type(exc).__name__, exc)
                finished.add(page_number)
                continue
            finished.add(page_number)
            logger.info(
                "page %d: %s%s",
                page_number,
                record.get("state"),
                f" ({record.get('reason')})" if record.get("reason") else "",
            )
    return finished


def _run_pages(
    store: Store,
    location: str,
    region: str | None,
    file_id: str,
    pages: list[int],
    workers: int,
    escalate: bool,
    max_attempts: int,
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
            _process_page(store, file_id, page_number, escalate, max_attempts)
        return

    remaining = list(pages)
    while remaining:
        before = {n: _read_attempts(store, file_id, n) for n in remaining}
        finished = _run_pool(location, region, file_id, remaining, workers, escalate, max_attempts)
        remaining = [n for n in remaining if n not in finished]
        if not remaining:
            return

        in_doubt = [n for n in remaining if _read_attempts(store, file_id, n) > before[n]]
        logger.error(
            "a worker died; re-running %d in-flight page(s) in isolation: %s",
            len(in_doubt),
            in_doubt,
        )
        if not in_doubt:
            raise RuntimeError(
                f"worker pool broke before any page started in "
                f"{store.location(_work_prefix(file_id))}"
            )
        for page_number in in_doubt:
            while True:
                if _read_attempts(store, file_id, page_number) >= max_attempts:
                    _finalize_exhausted(store, file_id, page_number)
                    break
                if _run_pool(location, region, file_id, [page_number], 1, escalate, max_attempts):
                    break
        remaining = [n for n in remaining if n not in in_doubt]


# ---------------------------------------------------------------------------
# Status and stitch
# ---------------------------------------------------------------------------


def build_status(
    store: Store, file_id: str, manifest: dict[str, Any], max_attempts: int
) -> dict[str, Any]:
    """Rebuild ``status.json`` from the page records and return it."""
    pages: list[dict[str, Any]] = []
    for page_number in range(1, int(manifest["page_count"]) + 1):
        record = _load_record(store, file_id, page_number)
        attempts = _read_attempts(store, file_id, page_number)
        state = record["state"] if record is not None else STATE_PENDING
        pages.append(
            {
                "page": page_number,
                "state": state,
                "reason": record.get("reason") if record is not None else None,
                "route": record.get("route") if record is not None else None,
                "attempts": attempts,
                "retryable": state != STATE_OK and attempts < max_attempts,
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
        "source_name": manifest["source_name"],
        "source_sha256": manifest["source_sha256"],
        "page_count": manifest["page_count"],
        "counts": counts,
        "not_ok_pages": [p for p in pages if p["state"] != STATE_OK],
        "pages": pages,
        "updated_at": datetime.now(tz=UTC).isoformat(),
    }
    _put_json(store, _status_key(file_id), status)
    return status


def _stitch_markdown(
    store: Store, file_id: str, status: dict[str, Any]
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
        record = _load_record(store, file_id, page_number) or {}
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
    file_id: str,
    manifest: dict[str, Any],
    status: dict[str, Any],
    partial: bool = False,
) -> dict[str, str | None]:
    """Write ``output.md`` iff every page is ok; optionally a partial document.

    Returns the store keys written: ``{"output": key or None, "partial": key or None}``.

    Fail closed: a file that is not complete gets NO ``output.md``. With
    ``partial=True`` it gets ``output.partial.md`` instead, with a gap marker where
    each missing page belongs. Re-runnable: a pure function of the page records.
    """
    keys: dict[str, str | None] = {"output": None, "partial": None}
    complete = status["state"] == FILE_COMPLETE
    if not complete and not partial:
        return keys

    markdown, pages_meta = _stitch_markdown(store, file_id, status)
    call_counts: dict[str, int] = {}
    for page in status["pages"]:
        record = _load_record(store, file_id, page["page"]) or {}
        for engine, count in (record.get("call_counts") or {}).items():
            call_counts[engine] = call_counts.get(engine, 0) + int(count)
    metadata = {
        "state": status["state"],
        "source_name": manifest["source_name"],
        "source_sha256": manifest["source_sha256"],
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
# Public entry point
# ---------------------------------------------------------------------------


def parse_pdf_pages_in_store(
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
) -> dict[str, Any]:
    """Split a PDF, parse its pages in parallel, and stitch when every page is ok.

    Args:
        source: The PDF to parse (a local file).
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

    Returns:
        ``{"state", "file_id", "work_dir", "out_dir", "page_count", "counts",
        "not_ok_pages", "pages", "status_path", "output_path", "partial_path",
        "output_key", "partial_key", "elapsed_seconds"}``. ``state`` is ``complete``
        only when every page is ok; ``output_path`` is None otherwise. ``work_dir``
        (the ``work/<file id>`` prefix), ``out_dir`` and the ``*_path`` values come
        from the store's ``location`` (a local path or an ``s3://`` URI); the
        ``*_key`` values are store keys.

    Raises on an input that cannot be split at all (missing, not a PDF, encrypted)
    and on ``PipelineConfigMismatch``; page-level failures never raise. A store
    error propagates.
    """
    source = Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() != ".pdf":
        raise ValueError(f"the page pipeline handles PDFs only, got {source.name}")
    workers = default_workers() if workers is None else workers
    max_attempts = default_max_attempts() if max_attempts is None else max_attempts
    if store is None:
        store = open_store(location, region=region)

    started = time.perf_counter()
    file_id = file_id_for(source)
    if restart:
        store.delete_prefix(f"{_work_prefix(file_id)}/")
        store.delete_prefix(f"{_out_prefix(file_id)}/")
    manifest = _split(store, file_id, source)

    todo: list[int] = []
    for page_number in range(1, int(manifest["page_count"]) + 1):
        # An unparseable record raises here, before any page is parsed.
        record = _load_record_checked(store, file_id, page_number)
        if record is not None and record.get("state") == STATE_OK:
            continue
        if _read_attempts(store, file_id, page_number) >= max_attempts:
            _finalize_exhausted(store, file_id, page_number)
            continue
        # failed or degraded, under the limit: archive it so the page's next result
        # is again the first write. Done here, never in a page task. The step says
        # no when the page turned ok in the meantime.
        if record is not None and not _archive_for_retry(store, file_id, page_number):
            continue
        todo.append(page_number)

    logger.info(
        "%s: %d pages, %d to process, %d already done, workers=%d",
        source.name,
        manifest["page_count"],
        len(todo),
        int(manifest["page_count"]) - len(todo),
        workers,
    )
    if todo:
        _run_pages(store, location, region, file_id, todo, workers, escalate, max_attempts)

    status = build_status(store, file_id, manifest, max_attempts)
    keys = stitch(store, file_id, manifest, status, partial=partial)
    if status["state"] != FILE_COMPLETE:
        logger.error(
            "%s: %s — pages not ok: %s",
            source.name,
            status["state"],
            [(p["page"], p["state"], p["reason"]) for p in status["not_ok_pages"]],
        )
    return {
        "state": status["state"],
        "file_id": file_id,
        "work_dir": store.location(_work_prefix(file_id)),
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
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def parse_pdf_pages(
    source: Path,
    work_root: Path,
    workers: int | None = None,
    escalate: bool = True,
    max_attempts: int | None = None,
    partial: bool = False,
    restart: bool = False,
) -> dict[str, Any]:
    """``parse_pdf_pages_in_store`` on a local directory.

    A thin wrapper for callers that hold a ``Path``: ``work_root`` is the store
    location, holding ``work/<file id>/`` and ``out/<file id>/`` per source file.
    """
    return parse_pdf_pages_in_store(
        source,
        str(Path(work_root).resolve()),
        workers=workers,
        escalate=escalate,
        max_attempts=max_attempts,
        partial=partial,
        restart=restart,
    )
