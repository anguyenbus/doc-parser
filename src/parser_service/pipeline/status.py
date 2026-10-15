"""
status.py — Rebuild ``status.json`` from the records and stitch the document, fail-closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from parser_service.formats.result import (
    STATE_DEGRADED,
    STATE_FAILED,
    STATE_OK,
    STATE_PENDING,
)
from parser_service.pipeline.config import (
    FILE_COMPLETE,
    FILE_FAILED,
    FILE_IN_PROGRESS,
    PAGE_MARKER,
    _flag_list,
    _is_final,
    current_limits,
    manifest_kind,
    unit_of,
)
from parser_service.pipeline.layout import (
    _load_record,
    _output_key,
    _put_json,
    _put_text,
    _read_attempts,
    _status_key,
)
from parser_service.storage.store import Store


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
        record = (
            (records or {}).get(page_number)
            if records is not None
            else _load_record(store, work_id, page_number)
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
            records.get(page_number)
            if records is not None
            else _load_record(store, work_id, page_number)
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
            records.get(page["page"])
            if records is not None
            else _load_record(store, work_id, page["page"])
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
