"""
test_page_pipeline_store.py — the page pipeline on the store interface.

Every pipeline case runs twice: on ``LocalStore`` (a real temporary directory) and
on ``MemoryStore``, a dict behind the interface kept in this file. The pipeline
passing on a store with no files of its own proves it reads and writes only
through the interface.

Offline and fast: ``parse_to_markdown`` is the fake from ``test_page_pipeline.py``
and pages run in-process (``workers=0``).

Run ONLY these tests:
    uv run pytest tests/test_page_pipeline_store.py
"""

from __future__ import annotations

import json
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from test_page_pipeline import THREE_PAGE, _install_fake, _result
from test_store_contract import CONTRACT_CASES

from parser_service import page_pipeline
from parser_service.page_pipeline import file_id_for, parse_pdf_pages_in_store, work_id_for
from parser_service.storage.store import (
    LocalStore,
    Store,
    StoreNotFoundError,
    _validate_delete_prefix,
    _validate_list_prefix,
    validate_key,
)
from parser_service.version import PARSER_VERSION

FILE_ID = file_id_for(THREE_PAGE)
WORK_ID = work_id_for(THREE_PAGE)


class MemoryStore:
    """A dict behind the store interface. Records every write and every file scope."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.writes: list[str] = []
        self.scope_dirs: list[Path] = []
        self._lock = threading.Lock()

    def read(self, key: str) -> bytes:
        try:
            return self.objects[validate_key(key)]
        except KeyError as exc:
            raise StoreNotFoundError(key) from exc

    def write(self, key: str, data: bytes) -> None:
        with self._lock:
            self.objects[validate_key(key)] = bytes(data)
            self.writes.append(key)

    def write_if_absent(self, key: str, data: bytes) -> bool:
        with self._lock:
            if validate_key(key) in self.objects:
                return False
            self.objects[key] = bytes(data)
            self.writes.append(key)
            return True

    def exists(self, key: str) -> bool:
        return validate_key(key) in self.objects

    def list_keys(self, prefix: str = "") -> list[str]:
        _validate_list_prefix(prefix)
        return sorted(key for key in self.objects if key.startswith(prefix))

    def delete(self, key: str) -> None:
        self.objects.pop(validate_key(key), None)

    def delete_prefix(self, prefix: str) -> None:
        _validate_delete_prefix(prefix)
        for key in self.list_keys(prefix):
            del self.objects[key]

    def location(self, key: str) -> str:
        return f"mem://test/{validate_key(key)}"

    @contextmanager
    def local_file(self, key: str) -> Iterator[Path]:
        validate_key(key)
        with tempfile.TemporaryDirectory(prefix="parser-memstore-") as directory:
            self.scope_dirs.append(Path(directory))
            path = Path(directory) / key.rpartition("/")[2]
            path.write_bytes(self.read(key))
            yield path


@pytest.fixture(params=["local", "memory"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Store:
    if request.param == "local":
        return LocalStore(tmp_path / "location")
    return MemoryStore()


def _run(store: Store, **kwargs: Any) -> dict[str, Any]:
    """One in-process pipeline run of the three-page fixture on ``store``."""
    kwargs.setdefault("workers", 0)
    return parse_pdf_pages_in_store(THREE_PAGE, "unused-with-workers-0", store=store, **kwargs)


def _record(store: Store, page_number: int) -> dict[str, Any]:
    record = json.loads(store.read(page_pipeline._record_key(WORK_ID, page_number)))
    assert isinstance(record, dict)
    return record


def _manifest(store: Store) -> dict[str, Any]:
    manifest = json.loads(store.read(page_pipeline._manifest_key(WORK_ID)))
    assert isinstance(manifest, dict)
    return manifest


# ---------------------------------------------------------------------------
# The pipeline uses only the interface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CONTRACT_CASES, ids=lambda case: str(case.__name__))
def test_memory_store_contract(case: Callable[[Store], None]) -> None:
    case(MemoryStore())


def test_full_run_on_memory_store_gives_the_same_keys_as_the_local_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    memory, local = MemoryStore(), LocalStore(tmp_path / "location")

    in_memory = _run(memory)
    on_disk = _run(local)

    assert in_memory["state"] == on_disk["state"] == "complete"
    assert in_memory["output_key"] is not None
    assert memory.read(in_memory["output_key"]).decode() == (
        "==== page 1 ===\n\ntext of page 1\n\n"
        "==== page 2 ===\n\ntext of page 2\n\n"
        "==== page 3 ===\n\ntext of page 3\n"
    )
    assert memory.list_keys() == local.list_keys()
    assert in_memory["output_path"] == memory.location(in_memory["output_key"])
    assert Path(on_disk["output_path"]).read_bytes() == memory.read(in_memory["output_key"])


def test_page_file_scope_is_closed_after_success_and_after_a_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Path] = []

    def by_page(n: int) -> dict[str, Any]:
        if n == 2:
            raise RuntimeError("boom")
        return _result(f"text of page {n}")

    def fake(path: Path, escalate: bool = True) -> dict[str, Any]:
        assert [p.name for p in path.parent.iterdir()] == [path.name]  # only the page file
        seen.append(path.parent)
        return by_page(int(path.stem))

    monkeypatch.setattr(page_pipeline, "parse_to_markdown", fake)
    memory = MemoryStore()

    result = _run(memory)

    assert result["counts"] == {"ok": 2, "degraded": 0, "failed": 1, "pending": 0}
    assert len(seen) == 3 and memory.scope_dirs == seen
    assert not any(directory.exists() for directory in seen)


def test_store_error_fetching_a_page_propagates_and_leaves_it_pending(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    real_local_file = store.local_file

    def failing(key: str) -> Any:
        if key.endswith("0002.pdf"):
            raise ConnectionError("store unreachable")
        return real_local_file(key)

    with monkeypatch.context() as patch:
        patch.setattr(store, "local_file", failing)
        with pytest.raises(ConnectionError, match="store unreachable"):
            _run(store)

    assert calls == [1]
    assert not store.exists(page_pipeline._record_key(WORK_ID, 2))  # no ``failed`` record
    status = page_pipeline.build_status(store, WORK_ID, FILE_ID, _manifest(store), 3)
    assert [(p["page"], p["state"], p["attempts"]) for p in status["pages"]] == [
        (1, "ok", 1),
        (2, "pending", 1),  # the attempt was counted before the fetch
        (3, "pending", 0),
    ]

    calls.clear()
    assert _run(store)["state"] == "complete"  # a re-run resumes it
    assert calls == [2, 3]
    assert _record(store, 2)["attempt"] == 2


# ---------------------------------------------------------------------------
# The page result: one object, first write wins, archive before a re-drive
# ---------------------------------------------------------------------------


def _fail_page_2_until(healthy: dict[str, bool]) -> Callable[[int], dict[str, Any]]:
    def by_page(n: int) -> dict[str, Any]:
        if n == 2 and not healthy["now"]:
            raise RuntimeError("boom")
        return _result(f"text of page {n}")

    return by_page


def test_record_holds_the_markdown_and_stitch_does_not_read_the_md_copies(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    first = _run(store)
    document = store.read(first["output_key"])

    record = _record(store, 2)
    assert record["markdown"] == "text of page 2"
    assert record["parser_version"] == PARSER_VERSION
    assert (record["markdown_file"], record["n_chars"], record["attempt"]) == ("0002.md", 14, 1)
    assert store.read(page_pipeline._markdown_key(WORK_ID, 2)) == b"text of page 2"

    for page_number in (1, 2, 3):
        store.delete(page_pipeline._markdown_key(WORK_ID, page_number))
    store.delete(first["output_key"])
    calls.clear()
    second = _run(store)

    assert second["state"] == "complete" and calls == []
    assert store.read(second["output_key"]) == document


def test_duplicate_final_write_changes_nothing_and_returns_the_stored_record(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    _run(store)
    record_key = page_pipeline._record_key(WORK_ID, 1)
    markdown_key = page_pipeline._markdown_key(WORK_ID, 1)
    stored_record, stored_markdown = store.read(record_key), store.read(markdown_key)

    returned = page_pipeline._write_record(store, WORK_ID, 1, "failed", "late", "OTHER RUN", 2)

    assert returned == json.loads(stored_record)  # the caller's result is discarded
    assert store.read(record_key) == stored_record
    assert store.read(markdown_key) == stored_markdown


def test_refused_write_over_an_unparseable_record_raises(store: Store) -> None:
    store.write(page_pipeline._record_key(WORK_ID, 1), b"{ not json")
    assert page_pipeline._load_record(store, WORK_ID, 1) is None  # a plain read: "no record"

    with pytest.raises(page_pipeline.PageRecordError, match="cannot be read"):
        page_pipeline._write_record(store, WORK_ID, 1, "ok", None, "text", 1)
    assert store.read(page_pipeline._record_key(WORK_ID, 1)) == b"{ not json"
    assert not store.exists(page_pipeline._markdown_key(WORK_ID, 1))


def test_page_task_with_a_stored_record_returns_it_without_working(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    _run(store)
    calls.clear()

    returned = page_pipeline._process_page(store, WORK_ID, 1, True, 3)

    assert returned == _record(store, 1)
    assert calls == []
    assert page_pipeline._read_attempts(store, WORK_ID, 1) == 1


def test_rerun_archives_a_failed_record_and_runs_the_page_again(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    healthy = {"now": False}
    calls = _install_fake(monkeypatch, _fail_page_2_until(healthy))
    assert _run(store)["state"] == "failed"
    failed_record = store.read(page_pipeline._record_key(WORK_ID, 2))
    assert json.loads(failed_record)["state"] == "failed"

    calls.clear()
    healthy["now"] = True
    assert _run(store)["state"] == "complete"

    assert calls == [2]
    archive = page_pipeline._archive_key(WORK_ID, 2, 1)
    assert archive.endswith("results/0002.attempt-1.json")
    assert store.read(archive) == failed_record
    assert _record(store, 2)["state"] == "ok" and _record(store, 2)["attempt"] == 2
    assert store.read(page_pipeline._markdown_key(WORK_ID, 2)) == b"text of page 2"


def test_archive_step_resumes_after_a_crash_without_a_duplicate_archive(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    healthy = {"now": False}
    _install_fake(monkeypatch, _fail_page_2_until(healthy))
    _run(store)
    record_key = page_pipeline._record_key(WORK_ID, 2)
    first_failure = store.read(record_key)

    # A crash between the copy and the deletes: the archive exists, so do the record
    # and its .md copy. Running the step again finishes it and adds no archive.
    assert store.write_if_absent(page_pipeline._archive_key(WORK_ID, 2, 1), first_failure)
    _run(store)  # page 2 fails again: a second, different record

    archives = page_pipeline.page_archives(store, WORK_ID, 2)
    assert archives.keys == [page_pipeline._archive_key(WORK_ID, 2, 1)]
    assert store.read(archives.keys[0]) == first_failure
    second_failure = store.read(record_key)
    assert second_failure != first_failure and json.loads(second_failure)["attempt"] == 2

    # The step alone, run twice in a row, is complete after the first.
    page_pipeline._archive_for_retry(store, WORK_ID, 2)
    assert not store.exists(record_key)
    assert not store.exists(page_pipeline._markdown_key(WORK_ID, 2))
    archives = page_pipeline.page_archives(store, WORK_ID, 2)
    assert archives.count == 2 and store.read(archives.keys[1]) == second_failure


def test_ok_pages_and_pages_at_the_attempt_limit_are_neither_archived_nor_retried(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_fake(
        monkeypatch, lambda n: _result("", route="vlm") if n == 3 else _result(f"p{n}")
    )
    _run(store, max_attempts=1)
    records = {n: store.read(page_pipeline._record_key(WORK_ID, n)) for n in (1, 2, 3)}

    calls.clear()
    result = _run(store, max_attempts=1)

    assert calls == [] and result["state"] == "failed"
    assert {n: store.read(page_pipeline._record_key(WORK_ID, n)) for n in (1, 2, 3)} == records
    assert all(page_pipeline.page_archives(store, WORK_ID, n).count == 0 for n in (1, 2, 3))


def test_archive_step_leaves_an_ok_record_alone(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    _run(store)
    record_key = page_pipeline._record_key(WORK_ID, 1)
    stored = store.read(record_key)

    assert page_pipeline._archive_for_retry(store, WORK_ID, 1) is False
    assert store.read(record_key) == stored
    assert store.exists(page_pipeline._markdown_key(WORK_ID, 1))
    assert page_pipeline.page_archives(store, WORK_ID, 1).count == 0


def test_rerun_does_not_archive_a_page_that_turned_ok_after_it_was_read(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two runs of one file: this run reads page 2 as failed, then another run's ok
    result lands before this run archives it. The ok result must survive."""
    healthy = {"now": False}
    calls = _install_fake(monkeypatch, _fail_page_2_until(healthy))
    _run(store)
    ok_record = json.dumps({"page": 2, "state": "ok", "markdown": "from the other run"})
    real_load = page_pipeline._load_record_checked

    def load_then_other_run_finishes(s: Store, file_id: str, n: int) -> dict[str, Any] | None:
        record = real_load(s, file_id, n)
        if n == 2 and record is not None and record["state"] == "failed":
            s.delete(page_pipeline._record_key(file_id, 2))
            s.write(page_pipeline._record_key(file_id, 2), ok_record.encode())
        return record

    monkeypatch.setattr(page_pipeline, "_load_record_checked", load_then_other_run_finishes)
    calls.clear()
    healthy["now"] = True
    result = _run(store)

    assert calls == []
    assert result["state"] == "complete"
    assert store.read(page_pipeline._record_key(WORK_ID, 2)) == ok_record.encode()
    assert page_pipeline.page_archives(store, WORK_ID, 2).count == 0


def test_archive_numbering_continues_after_an_older_archive_expired(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    healthy = {"now": False}
    _install_fake(monkeypatch, _fail_page_2_until(healthy))
    _run(store)
    failed_record = store.read(page_pipeline._record_key(WORK_ID, 2))
    # attempt-1 has expired; attempt-2, a different earlier result, is still there.
    store.write(page_pipeline._archive_key(WORK_ID, 2, 2), b'{"state": "failed", "older": 1}')

    healthy["now"] = True
    assert _run(store)["state"] == "complete"

    archives = page_pipeline.page_archives(store, WORK_ID, 2)
    assert archives.keys == [page_pipeline._archive_key(WORK_ID, 2, k) for k in (2, 3)]
    assert (archives.count, archives.last_number) == (2, 3)
    assert store.read(archives.keys[-1]) == failed_record


def test_unparseable_record_is_refused_before_any_page_is_parsed(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    _run(store)
    store.write(page_pipeline._record_key(WORK_ID, 2), b"{ not json")
    attempts = page_pipeline._read_attempts(store, WORK_ID, 2)
    calls.clear()

    with pytest.raises(page_pipeline.PageRecordError, match="cannot be read"):
        _run(store)
    with pytest.raises(page_pipeline.PageRecordError, match="cannot be read"):
        page_pipeline._process_page(store, WORK_ID, 2, True, 3)

    assert calls == []
    assert page_pipeline._read_attempts(store, WORK_ID, 2) == attempts
    assert store.read(page_pipeline._record_key(WORK_ID, 2)) == b"{ not json"


def test_archive_helper_returns_keys_and_count_and_an_archive_is_not_a_record(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(monkeypatch, _fail_page_2_until({"now": False}))
    _run(store)
    manifest = _manifest(store)
    for k in (2, 10, 1):  # ordered by K as a number, not as text
        store.write(page_pipeline._archive_key(WORK_ID, 2, k), b"{}")
    store.write(page_pipeline._archive_key(WORK_ID, 2, 3).replace("-3.json", "-x.json"), b"{}")
    store.delete(page_pipeline._record_key(WORK_ID, 2))

    archives = page_pipeline.page_archives(store, WORK_ID, 2)
    assert archives.count == 3
    assert archives.keys == [page_pipeline._archive_key(WORK_ID, 2, k) for k in (1, 2, 10)]
    assert page_pipeline.page_archives(store, WORK_ID, 1) == page_pipeline.PageArchives([], 0)

    # Page 2 has archives and no record: it is pending, not finished.
    assert page_pipeline._load_record(store, WORK_ID, 2) is None
    status = page_pipeline.build_status(store, WORK_ID, FILE_ID, manifest, 3)
    assert [p["state"] for p in status["pages"]] == ["ok", "pending", "ok"]


# ---------------------------------------------------------------------------
# The work/ and out/ layout, the stored original, an expired work/
# ---------------------------------------------------------------------------

WORK = f"work/{WORK_ID}"
OUT = f"out/{FILE_ID}"


def _record_writes(store: Store, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Log the key of every write made through ``store`` from now on."""
    written: list[str] = []
    real_write, real_write_if_absent = store.write, store.write_if_absent

    def write(key: str, data: bytes) -> None:
        written.append(key)
        real_write(key, data)

    def write_if_absent(key: str, data: bytes) -> bool:
        written.append(key)
        return real_write_if_absent(key, data)

    monkeypatch.setattr(store, "write", write)
    monkeypatch.setattr(store, "write_if_absent", write_if_absent)
    return written


def test_every_key_is_under_its_prefix_and_nothing_is_flat(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    healthy = {"now": False}
    _install_fake(monkeypatch, _fail_page_2_until(healthy))
    assert _run(store, partial=True)["state"] == "failed"
    assert [k for k in store.list_keys(f"{OUT}/")] == [
        f"{OUT}/output.partial.json",
        f"{OUT}/output.partial.md",
        f"{OUT}/status.json",
    ]
    store.delete_prefix(f"{OUT}/")
    healthy["now"] = True
    assert _run(store)["state"] == "complete"

    pages = ("0001", "0002", "0003")
    assert store.list_keys() == sorted(
        [f"{OUT}/output.json", f"{OUT}/output.md", f"{OUT}/status.json"]
        + [f"{WORK}/manifest.json", f"{WORK}/source.pdf", f"{WORK}/results/0002.attempt-1.json"]
        + [f"{WORK}/pages/{n}.pdf" for n in pages]
        + [f"{WORK}/attempts/{n}" for n in pages]
        + [f"{WORK}/results/{n}.json" for n in pages]
        + [f"{WORK}/results/{n}.md" for n in pages]
    )
    assert store.list_keys(f"{FILE_ID}/") == []  # the flat layout is gone


def test_split_stores_the_original_first_and_the_manifest_last(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = _record_writes(store, monkeypatch)

    manifest = page_pipeline._split(store, WORK_ID, THREE_PAGE)

    assert written == [
        f"{WORK}/source.pdf",
        f"{WORK}/pages/0001.pdf",
        f"{WORK}/pages/0002.pdf",
        f"{WORK}/pages/0003.pdf",
        f"{WORK}/manifest.json",
    ]
    assert store.read(f"{WORK}/source.pdf") == THREE_PAGE.read_bytes()
    assert manifest["page_count"] == 3 and manifest == _manifest(store)


def test_missing_page_files_are_recreated_and_the_manifest_is_not_rewritten(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pypdf import PdfReader

    calls = _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    _run(store)
    manifest_bytes = store.read(f"{WORK}/manifest.json")
    page_3 = store.read(f"{WORK}/pages/0003.pdf")
    # Expiry is per object: two page files and page 3's result are gone, the manifest is not.
    for key in ("pages/0001.pdf", "pages/0003.pdf", "results/0003.json", "attempts/0003"):
        store.delete(f"{WORK}/{key}")
    written = _record_writes(store, monkeypatch)
    calls.clear()

    result = _run(store)

    assert result["state"] == "complete" and calls == [3]
    assert written[:2] == [f"{WORK}/pages/0001.pdf", f"{WORK}/pages/0003.pdf"]
    assert f"{WORK}/manifest.json" not in written and f"{WORK}/pages/0002.pdf" not in written
    assert store.read(f"{WORK}/manifest.json") == manifest_bytes
    with store.local_file(f"{WORK}/pages/0003.pdf") as path:
        assert len(PdfReader(str(path)).pages) == 1
    assert len(store.read(f"{WORK}/pages/0003.pdf")) == len(page_3)


def test_missing_stored_original_is_restored(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    _run(store)
    store.delete(f"{WORK}/source.pdf")
    written = _record_writes(store, monkeypatch)

    _run(store)

    assert store.read(f"{WORK}/source.pdf") == THREE_PAGE.read_bytes()
    assert f"{WORK}/manifest.json" not in written and not any("/pages/" in k for k in written)


def test_restart_removes_both_prefixes_of_this_file_only(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    _run(store)
    for stray in (f"{WORK}/candidates/0001.json", f"{OUT}/stale.md", "work/other-file/keep"):
        store.write(stray, b"-")
    calls.clear()

    result = _run(store, restart=True)

    assert result["state"] == "complete" and sorted(calls) == [1, 2, 3]
    assert not store.exists(f"{WORK}/candidates/0001.json")
    assert not store.exists(f"{OUT}/stale.md")
    assert store.exists("work/other-file/keep")
    assert _record(store, 1)["attempt"] == 1  # the attempt counters went too


def test_result_reports_work_and_out_locations_from_the_store(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))

    result = _run(store)

    assert result["file_id"] == FILE_ID and result["work_id"] == WORK_ID
    assert result["work_dir"] == store.location(WORK)
    assert result["out_dir"] == store.location(OUT)
    assert result["status_path"] == store.location(f"{OUT}/status.json")
    assert result["output_key"] == f"{OUT}/output.md"
    assert result["output_path"] == store.location(f"{OUT}/output.md")
    assert result["partial_path"] is None and result["partial_key"] is None


def test_the_same_bytes_under_another_name_share_the_work_but_not_the_output(
    store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    renamed = tmp_path / "inputs" / "renamed copy.pdf"
    renamed.parent.mkdir()
    renamed.write_bytes(THREE_PAGE.read_bytes())

    first = _run(store)
    second = parse_pdf_pages_in_store(renamed, "unused-with-workers-0", store=store, workers=0)

    assert first["state"] == second["state"] == "complete"
    assert sorted(calls) == [1, 2, 3]  # parsed once, for the first name
    assert first["work_id"] == second["work_id"] == WORK_ID
    assert second["file_id"] == f"renamed copy-{WORK_ID[:16]}" != first["file_id"]
    assert [k for k in store.list_keys("work/") if k.endswith("manifest.json")] == [
        f"{WORK}/manifest.json"
    ]
    out2 = f"out/{second['file_id']}"
    assert store.read(f"{out2}/output.md") == store.read(f"{OUT}/output.md")
    for key in (f"{out2}/status.json", f"{out2}/output.json"):
        meta = json.loads(store.read(key))
        assert meta["source_name"] == "renamed copy.pdf" and meta["work_id"] == WORK_ID
    assert json.loads(store.read(f"{OUT}/status.json"))["source_name"] == THREE_PAGE.name


def test_restart_under_one_name_restarts_the_shared_work(
    store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    renamed = tmp_path / "renamed.pdf"
    renamed.write_bytes(THREE_PAGE.read_bytes())
    _run(store)
    calls.clear()

    parse_pdf_pages_in_store(renamed, "unused-with-workers-0", store=store, workers=0, restart=True)

    assert sorted(calls) == [1, 2, 3]
    assert store.exists(f"{OUT}/output.md")  # the other name's output is left alone


def test_unknown_keys_under_work_do_not_break_a_run(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    healthy = {"now": False}
    _install_fake(monkeypatch, _fail_page_2_until(healthy))
    for key in ("candidates/0001.json", "results/0002.candidate.json", "pages/notes.txt"):
        store.write(f"{WORK}/{key}", b"{}")

    assert _run(store)["state"] == "failed"
    healthy["now"] = True
    assert _run(store)["state"] == "complete"

    assert store.read(f"{WORK}/candidates/0001.json") == b"{}"
    assert page_pipeline.page_archives(store, WORK_ID, 2).count == 1


# ---------------------------------------------------------------------------
# Gap fill: whole-workflow cases
# ---------------------------------------------------------------------------


def test_rerun_redrives_a_degraded_page(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    engine_up = {"now": False}

    def by_page(n: int) -> dict[str, Any]:
        if n == 2 and not engine_up["now"]:
            return _result("docling text", route="vlm-fallback-docling", reason="throttled")
        return _result(f"text of page {n}", route="vlm" if n == 2 else "docling-kept")

    calls = _install_fake(monkeypatch, by_page)
    assert _run(store)["state"] == "failed"
    degraded = store.read(f"{WORK}/results/0002.json")
    assert json.loads(degraded)["state"] == "degraded"

    calls.clear()
    engine_up["now"] = True
    result = _run(store)

    assert result["state"] == "complete" and calls == [2]
    assert store.read(f"{WORK}/results/0002.attempt-1.json") == degraded
    assert _record(store, 2)["markdown"] == "text of page 2"
    assert b"docling text" not in store.read(result["output_key"])
    assert store.read(f"{WORK}/results/0002.md") == b"text of page 2"


def test_worker_task_opens_its_own_store_from_the_location_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a pool worker runs, called here in-process: location, region, file id."""
    calls = _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    monkeypatch.setattr(page_pipeline, "_WORKER_STORES", {})
    location = str(tmp_path / "location")
    local = LocalStore(location)
    page_pipeline._split(local, WORK_ID, THREE_PAGE)

    record = page_pipeline._process_page_task(location, None, WORK_ID, 2, True, 3)

    assert calls == [2] and record["state"] == "ok"
    assert _record(local, 2) == record
    assert list(page_pipeline._WORKER_STORES) == [(location, None)]  # opened once, then reused
    page_pipeline._process_page_task(location, None, WORK_ID, 3, True, 3)
    assert len(page_pipeline._WORKER_STORES) == 1


def test_path_split_accepts_only_the_directory_work_dir_for_returns(tmp_path: Path) -> None:
    work_dir = page_pipeline.work_dir_for(THREE_PAGE, tmp_path)
    assert work_dir == tmp_path / "work" / WORK_ID

    with pytest.raises(ValueError, match="not a work directory"):
        page_pipeline.split_pdf(THREE_PAGE, tmp_path / FILE_ID)  # the old flat layout
    assert list(tmp_path.iterdir()) == []

    page_pipeline.split_pdf(THREE_PAGE, work_dir)
    assert sorted(p.name for p in work_dir.iterdir()) == [
        "attempts",
        "manifest.json",
        "pages",
        "results",
        "source.pdf",
    ]
