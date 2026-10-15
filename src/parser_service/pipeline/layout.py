"""
layout.py — The store layout: every key under ``work/`` and ``out/``, and the small helpers
that read and write records, attempts and JSON there.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from parser_service.formats.sniff import KIND_EXTENSIONS
from parser_service.parsing.elements import sha256_file
from parser_service.pipeline.config import PageRecordError, manifest_kind
from parser_service.storage.store import LocalStore, Store, StoreNotFoundError


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


def file_id_for(source: Path) -> str:
    """The id of a named source file's output in the store: ``<stem>-<sha256[:16]>``."""
    return _file_id(source.stem, sha256_file(source))


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
