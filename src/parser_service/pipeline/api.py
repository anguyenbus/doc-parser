"""
api.py — The public entry points: ``parse_file_in_store`` and ``parse_pdf_pages``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from parser_service.delivery.publish import EMAIL_KINDS, PublishRequest, publish_document
from parser_service.formats.result import (
    STATE_OK,
)
from parser_service.observability import (
    log_event,
    truncate,
)
from parser_service.parsing.elements import sha256_file
from parser_service.pipeline import layout
from parser_service.pipeline.config import (
    FILE_COMPLETE,
    _is_final,
    config_hash,
    current_limits,
    default_max_attempts,
    default_workers,
    logger,
    pipeline_config,
    raw_cap_bytes,
    remote_request_timeout,
    unit_of,
)
from parser_service.pipeline.intake import (
    INTAKE_TOO_LARGE,
    _file_event_fields,
    _intake,
    _intake_rejection,
    _log_file_done,
    _reject_at_intake,
    _sniff_source,
)
from parser_service.pipeline.layout import (
    _file_id,
    _load_record,
    _out_prefix,
    _put_json,
    _read_attempts,
    _status_key,
    _work_id,
    _work_prefix,
)
from parser_service.pipeline.local_runner import _run_pages
from parser_service.pipeline.remote_runner import EXECUTOR_KSERVE, EXECUTOR_LOCAL, _run_pages_remote
from parser_service.pipeline.status import _stitch_markdown, build_status, stitch
from parser_service.pipeline.units import _archive_for_retry, _finalize_exhausted
from parser_service.storage.store import Store, open_store


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
        raise ValueError(
            f"executor must be {EXECUTOR_LOCAL} or {EXECUTOR_KSERVE}, got {executor!r}"
        )
    if executor == EXECUTOR_KSERVE and not endpoint:
        raise ValueError("the kserve executor needs the page service endpoint")
    workers = default_workers() if workers is None else workers
    max_attempts = default_max_attempts() if max_attempts is None else max_attempts
    if store is None:
        store = open_store(location, region=region)

    started = time.perf_counter()
    sha256 = sha256_file(source)
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
        record = layout._load_record_checked(store, work_id, page_number)
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
