"""
units.py — One unit: process it (in process or in the parse worker), write its record
(first write wins), and archive failed attempts for a retry.
"""

from __future__ import annotations

import hashlib
import os
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from parser_service.formats.handlers import handler_version, parse_unit
from parser_service.formats.result import (
    STATE_FAILED,
    STATE_OK,
    Reply,
    exception_reply,
)
from parser_service.observability import (
    observe_unit,
)
from parser_service.pipeline import layout
from parser_service.pipeline.config import (
    PageRecordError,
    _flag_list,
    _guard_rejection,
    _reason_code,
    deadline_for,
    logger,
    manifest_kind,
    output_cap_bytes,
    unit_of,
)
from parser_service.pipeline.layout import (
    _archive_key,
    _archive_number,
    _archive_prefix,
    _attempts_key,
    _json_bytes,
    _load_record,
    _manifest_key,
    _markdown_key,
    _put_text,
    _read_attempts,
    _read_json,
    _read_record,
    _record_key,
    _unit_key,
)
from parser_service.service.worker import Request, WorkerTimeout
from parser_service.storage.store import Store, StoreNotFoundError
from parser_service.version import PARSER_VERSION


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
    existing = layout._load_record_checked(store, work_id, page_number)
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
            state, reason, reason_code, markdown, reply_extra = exception_reply(exc)
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
    existing = layout._load_record_checked(store, work_id, page_number)
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
