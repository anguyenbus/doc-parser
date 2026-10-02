"""
page_pipeline.py — split-first, per-page PDF parsing with resume and a fail-closed stitch.

``parse_pdf_pages(source, work_root)`` is the page-level counterpart of
``markdown_pipeline.parse_to_markdown``:

  1. SPLIT   the PDF into one-page PDF files (pypdf), then write ``manifest.json``
             LAST (source hash, true page count, pinned pipeline config).
  2. PARSE   each one-page file with the unchanged ``parse_to_markdown`` — Docling
             first, gate, escalation to VLM/Textract only when promoted — in a pool
             of worker processes. Each page writes ``results/NNNN.md`` and then
             ``results/NNNN.json`` (the JSON is the commit marker).
  3. STATUS  ``status.json`` is rebuilt from the page records (never the source of
             truth). Every page is ``ok``, ``degraded``, ``failed`` or ``pending``.
  4. STITCH  only when EVERY page is ``ok``: ``output.md`` with a
             ``==== page {number} ===`` line before each page, plus ``output.json``.
             Anything else leaves the file ``failed`` with the page numbers and
             reasons in ``status.json`` and no ``output.md``. A partial document
             (``output.partial.md``, gaps marked) is written only on request.

Restart is the normal path — there is no recovery mode. A page is finished iff its
result JSON exists; every write is temp-file + rename; a re-run skips ``ok`` pages
and retries the rest until ``max_attempts``. The attempt counter is written BEFORE
each try, so a page that kills its worker (no exception, no record) still becomes
``failed`` once the limit is reached.

Work directory layout (``<work_root>/<stem>-<sha256[:16]>/``):

    manifest.json        written last by the split
    pages/0001.pdf …     the one-page files (1-based page numbers)
    attempts/0001 …      attempt counter, written before each try
    results/0001.md      the page's markdown
    results/0001.json    the page's record; exists = page finished
    status.json          summary rebuilt from results/
    output.md            only when every page is ok
    output.json          metadata for output.md
    output.partial.md    only on request, when the file is not complete

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
import shutil
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
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
# Files: atomic writes and paths
# ---------------------------------------------------------------------------


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_write_json(path: Path, obj: Any) -> None:
    _atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _name(page_number: int) -> str:
    return f"{page_number:04d}"


def _page_pdf(work_dir: Path, page_number: int) -> Path:
    return work_dir / "pages" / f"{_name(page_number)}.pdf"


def _record_path(work_dir: Path, page_number: int) -> Path:
    return work_dir / "results" / f"{_name(page_number)}.json"


def _markdown_path(work_dir: Path, page_number: int) -> Path:
    return work_dir / "results" / f"{_name(page_number)}.md"


def _attempts_path(work_dir: Path, page_number: int) -> Path:
    return work_dir / "attempts" / _name(page_number)


def _read_attempts(work_dir: Path, page_number: int) -> int:
    try:
        return int(_attempts_path(work_dir, page_number).read_text().strip())
    except (OSError, ValueError):
        return 0


def _load_record(work_dir: Path, page_number: int) -> dict[str, Any] | None:
    record = _read_json(_record_path(work_dir, page_number))
    return record if isinstance(record, dict) else None


def work_dir_for(source: Path, work_root: Path) -> Path:
    """The work directory for a source file: ``<stem>-<sha256[:16]>`` under the root."""
    return Path(work_root) / f"{source.stem}-{_sha256_file(source)[:16]}"


# ---------------------------------------------------------------------------
# Split
# ---------------------------------------------------------------------------


def split_pdf(source: Path, work_dir: Path) -> dict[str, Any]:
    """Split ``source`` into one-page PDFs under ``work_dir`` and return the manifest.

    Idempotent: when a manifest already exists it is returned as is (after checking
    the pinned config). The manifest is written LAST, so a crash mid-split leaves no
    manifest and the split is simply redone. Raises on an unreadable or encrypted
    PDF, and ``PipelineConfigMismatch`` when the directory was started under a
    different config.
    """
    from pypdf import PdfReader, PdfWriter  # local import; only needed for PDFs

    manifest_path = work_dir / "manifest.json"
    config = pipeline_config()
    existing = _read_json(manifest_path)
    if isinstance(existing, dict):
        if existing.get("config") != config:
            raise PipelineConfigMismatch(
                f"{work_dir} was started with config {existing.get('config')} but the "
                f"current config is {config}; finish it under the original settings or "
                "restart it"
            )
        return existing

    reader = PdfReader(str(source))
    if reader.is_encrypted:
        raise ValueError(f"{source.name} is encrypted and cannot be split")
    page_count = len(reader.pages)
    if page_count == 0:
        raise ValueError(f"{source.name} has no pages")

    for sub in ("pages", "attempts", "results"):
        (work_dir / sub).mkdir(parents=True, exist_ok=True)
    for index in range(page_count):
        writer = PdfWriter()
        writer.add_page(reader.pages[index])
        buf = io.BytesIO()
        writer.write(buf)
        _atomic_write_bytes(_page_pdf(work_dir, index + 1), buf.getvalue())

    manifest = {
        "source_name": source.name,
        "source_sha256": _sha256_file(source),
        "page_count": page_count,
        "config": config,
        "created_at": datetime.now(tz=UTC).isoformat(),
    }
    _atomic_write_json(manifest_path, manifest)
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
    work_dir: Path,
    page_number: int,
    state: str,
    reason: str | None,
    markdown: str,
    attempt: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the page's markdown, then its record JSON (the commit marker)."""
    _atomic_write_text(_markdown_path(work_dir, page_number), markdown)
    record: dict[str, Any] = {
        "page": page_number,
        "state": state,
        "reason": reason,
        "n_chars": len(markdown),
        "markdown_file": _markdown_path(work_dir, page_number).name,
        "markdown_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        "attempt": attempt,
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "worker_pid": os.getpid(),
    }
    if extra:
        record.update(extra)
    _atomic_write_json(_record_path(work_dir, page_number), record)
    return record


def _process_page(
    work_dir_str: str, page_number: int, escalate: bool, max_attempts: int
) -> dict[str, Any]:
    """Parse one page and write its record. Runs in a worker process.

    The attempt counter is written BEFORE the work, so a page that kills the
    process is still counted. Never raises for a page-level failure: an exception
    becomes a ``failed`` record with its reason.
    """
    work_dir = Path(work_dir_str)
    attempt = _read_attempts(work_dir, page_number) + 1
    if attempt > max_attempts:
        return _finalize_exhausted(work_dir, page_number)
    _atomic_write_text(_attempts_path(work_dir, page_number), str(attempt))

    page_pdf = _page_pdf(work_dir, page_number)
    started = time.perf_counter()
    try:
        if _is_blank_page(page_pdf):
            return _write_record(
                work_dir,
                page_number,
                STATE_OK,
                None,
                "",
                attempt,
                {
                    "route": _ROUTE_BLANK,
                    "route_reason": "blank_page",
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                },
            )
        result = parse_to_markdown(page_pdf, escalate=escalate)
    except Exception as exc:  # noqa: BLE001 — a page error is a record, not a crash
        logger.exception("page %d failed", page_number)
        return _write_record(
            work_dir,
            page_number,
            STATE_FAILED,
            f"exception: {type(exc).__name__}: {exc}",
            "",
            attempt,
            {"elapsed_seconds": round(time.perf_counter() - started, 3)},
        )

    state, reason = classify_page_result(result)
    # The one-page file numbers its only page 0; store the real 0-based index.
    routes = [{**r, "page_index": page_number - 1} for r in result.get("page_routes", [])]
    warnings = [
        {**w, "page_index": page_number - 1} if "page_index" in w else dict(w)
        for w in result.get("warnings", [])
    ]
    return _write_record(
        work_dir,
        page_number,
        state,
        reason,
        str(result.get("markdown", "")),
        attempt,
        {
            "route": routes[0].get("route") if routes else None,
            "route_reason": routes[0].get("reason") if routes else None,
            "page_routes": routes,
            "warnings": warnings,
            "confidence": result.get("confidence", {}).get("document"),
            "call_counts": result.get("call_counts", {}),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        },
    )


def _finalize_exhausted(work_dir: Path, page_number: int) -> dict[str, Any]:
    """Record a page that used every attempt without producing a result."""
    existing = _load_record(work_dir, page_number)
    if existing is not None:
        return existing
    attempts = _read_attempts(work_dir, page_number)
    return _write_record(
        work_dir,
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


def _run_pool(
    work_dir: Path, pages: list[int], workers: int, escalate: bool, max_attempts: int
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
            pool.submit(_process_page, str(work_dir), n, escalate, max_attempts): n for n in pages
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
    work_dir: Path, pages: list[int], workers: int, escalate: bool, max_attempts: int
) -> None:
    """Process ``pages`` once each, surviving worker deaths.

    ``workers <= 0`` runs in this process, one page at a time (debugging). Otherwise
    pages run in a pool. When a worker dies the pool breaks; the pages that had
    started are then re-run ONE PER POOL so a page that keeps killing its worker
    only uses up its own attempts, and the rest go back to a normal pool.
    """
    if workers <= 0:
        enable_converter_reuse()
        for page_number in pages:
            _process_page(str(work_dir), page_number, escalate, max_attempts)
        return

    remaining = list(pages)
    while remaining:
        before = {n: _read_attempts(work_dir, n) for n in remaining}
        finished = _run_pool(work_dir, remaining, workers, escalate, max_attempts)
        remaining = [n for n in remaining if n not in finished]
        if not remaining:
            return

        in_doubt = [n for n in remaining if _read_attempts(work_dir, n) > before[n]]
        logger.error(
            "a worker died; re-running %d in-flight page(s) in isolation: %s",
            len(in_doubt),
            in_doubt,
        )
        if not in_doubt:
            raise RuntimeError(f"worker pool broke before any page started in {work_dir}")
        for page_number in in_doubt:
            while True:
                if _read_attempts(work_dir, page_number) >= max_attempts:
                    _finalize_exhausted(work_dir, page_number)
                    break
                if _run_pool(work_dir, [page_number], 1, escalate, max_attempts):
                    break
        remaining = [n for n in remaining if n not in in_doubt]


# ---------------------------------------------------------------------------
# Status and stitch
# ---------------------------------------------------------------------------


def build_status(work_dir: Path, manifest: dict[str, Any], max_attempts: int) -> dict[str, Any]:
    """Rebuild ``status.json`` from the page records and return it."""
    pages: list[dict[str, Any]] = []
    for page_number in range(1, int(manifest["page_count"]) + 1):
        record = _load_record(work_dir, page_number)
        attempts = _read_attempts(work_dir, page_number)
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
    _atomic_write_json(work_dir / "status.json", status)
    return status


def _stitch_markdown(work_dir: Path, status: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
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
        record = _load_record(work_dir, page_number) or {}
        if page["state"] == STATE_OK:
            marker = PAGE_MARKER.format(number=page_number)
            body = _markdown_path(work_dir, page_number).read_text(encoding="utf-8").strip()
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
    work_dir: Path, manifest: dict[str, Any], status: dict[str, Any], partial: bool = False
) -> dict[str, str | None]:
    """Write ``output.md`` iff every page is ok; optionally a partial document.

    Fail closed: a file that is not complete gets NO ``output.md``. With
    ``partial=True`` it gets ``output.partial.md`` instead, with a gap marker where
    each missing page belongs. Re-runnable: a pure function of the page records.
    """
    paths: dict[str, str | None] = {"output": None, "partial": None}
    complete = status["state"] == FILE_COMPLETE
    if not complete and not partial:
        return paths

    markdown, pages_meta = _stitch_markdown(work_dir, status)
    call_counts: dict[str, int] = {}
    for page in status["pages"]:
        record = _load_record(work_dir, page["page"]) or {}
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
    _atomic_write_text(work_dir / f"{stem}.md", markdown)
    _atomic_write_json(work_dir / f"{stem}.json", metadata)
    paths["output" if complete else "partial"] = str(work_dir / f"{stem}.md")
    return paths


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_pdf_pages(
    source: Path,
    work_root: Path,
    workers: int | None = None,
    escalate: bool = True,
    max_attempts: int | None = None,
    partial: bool = False,
    restart: bool = False,
) -> dict[str, Any]:
    """Split a PDF, parse its pages in parallel, and stitch when every page is ok.

    Args:
        source: The PDF to parse.
        work_root: Directory holding one work directory per source file.
        workers: Worker processes (default ``default_workers()``); ``0`` runs the
            pages in this process, one at a time.
        escalate: Passed to ``parse_to_markdown``. With False, a page the gate
            promotes keeps its Docling text and is recorded as ``degraded``.
        max_attempts: Attempt limit per page (default ``default_max_attempts()``).
        partial: Also write ``output.partial.md`` when the file is not complete.
        restart: Delete the work directory first and start again.

    Returns:
        ``{"state", "work_dir", "page_count", "counts", "not_ok_pages", "pages",
        "output_path", "partial_path", "elapsed_seconds"}``. ``state`` is
        ``complete`` only when every page is ok; ``output_path`` is None otherwise.

    Raises on an input that cannot be split at all (missing, not a PDF, encrypted)
    and on ``PipelineConfigMismatch``; page-level failures never raise.
    """
    source = Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() != ".pdf":
        raise ValueError(f"the page pipeline handles PDFs only, got {source.name}")
    workers = default_workers() if workers is None else workers
    max_attempts = default_max_attempts() if max_attempts is None else max_attempts

    started = time.perf_counter()
    work_dir = work_dir_for(source, Path(work_root))
    if restart and work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    manifest = split_pdf(source, work_dir)

    todo: list[int] = []
    for page_number in range(1, int(manifest["page_count"]) + 1):
        record = _load_record(work_dir, page_number)
        if record is not None and record.get("state") == STATE_OK:
            continue
        if _read_attempts(work_dir, page_number) >= max_attempts:
            _finalize_exhausted(work_dir, page_number)
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
        _run_pages(work_dir, todo, workers, escalate, max_attempts)

    status = build_status(work_dir, manifest, max_attempts)
    paths = stitch(work_dir, manifest, status, partial=partial)
    if status["state"] != FILE_COMPLETE:
        logger.error(
            "%s: %s — pages not ok: %s",
            source.name,
            status["state"],
            [(p["page"], p["state"], p["reason"]) for p in status["not_ok_pages"]],
        )
    return {
        "state": status["state"],
        "work_dir": str(work_dir),
        "page_count": manifest["page_count"],
        "counts": status["counts"],
        "not_ok_pages": status["not_ok_pages"],
        "pages": status["pages"],
        "output_path": paths["output"],
        "partial_path": paths["partial"],
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
