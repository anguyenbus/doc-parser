"""
test_page_worker.py — the page service's parse worker and the timeout record (spec D4).

Real worker processes, spawned as in production, but with fake module-level parse
targets (``spawn`` does not carry monkeypatches) that answer, sleep, raise, die,
or block in native code with SIGTERM ignored. No Docling is loaded; deadlines are
1-2 s. Every worker is killed in teardown, and each test checks that it left no
child process behind.

Run ONLY these tests:
    uv run pytest tests/test_page_worker.py
"""

from __future__ import annotations

import ctypes
import multiprocessing
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from parser_service import page_pipeline
from parser_service.page_pipeline import split_pdf, work_dir_for, work_id_for
from parser_service.service.worker import (
    ParseWorker,
    Reply,
    WorkerDied,
    WorkerTimeout,
)
from parser_service.storage.store import LocalStore

FIXTURES = Path(__file__).parent / "fixtures"
THREE_PAGE = FIXTURES / "three_page_digital.pdf"
DEADLINE_S = 1
PREVIOUS_PID_ENV = "TEST_PAGE_WORKER_PREVIOUS_PID"


# ---------------------------------------------------------------------------
# Module-level targets: they run in the spawned worker
# ---------------------------------------------------------------------------


def _scripted_target(kind: str, unit_path: str, page_number: int, escalate: bool) -> Reply:
    """Chosen by request: page 1 answers, page 2 hangs, page 3 raises."""
    if page_number == 2:
        time.sleep(60)
    if page_number == 3:
        raise RuntimeError("boom on page 3")
    return "ok", None, None, f"page {page_number} by {os.getpid()}", {"route": "docling-kept"}


def _dying_target(kind: str, unit_path: str, page_number: int, escalate: bool) -> Reply:
    """The worker dies mid-parse, as under the OOM killer (no reply, no exception)."""
    os._exit(3)


def _native_hang_target(kind: str, unit_path: str, page_number: int, escalate: bool) -> Reply:
    """Ignore SIGTERM, start a grandchild, then block in libc ``sleep`` (native code).

    Writes ``"<worker pid> <grandchild pid>"`` to ``unit_path`` first.
    """
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    grandchild = subprocess.Popen(["sleep", "60"])
    Path(unit_path).write_text(f"{os.getpid()} {grandchild.pid}")
    ctypes.CDLL(None).sleep(60)
    return "ok", None, None, "never", {}


def _check_previous_worker_reaped() -> None:
    """Warm-up hook: refuse to start while the previous worker still exists.

    ``/proc/<pid>`` exists for a running process AND for an unreaped zombie, so
    this passes only when the previous worker was killed and joined first.
    """
    previous = os.environ.get(PREVIOUS_PID_ENV)
    if previous and Path(f"/proc/{previous}").exists():
        raise RuntimeError(f"previous worker {previous} still exists")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def make_worker() -> Iterator[Callable[..., ParseWorker]]:
    handles: list[ParseWorker] = []

    def make(target: Any, warmup: Any = None, **kwargs: Any) -> ParseWorker:
        handle = ParseWorker(target, warmup, start_timeout_s=60, **kwargs)
        handles.append(handle)
        return handle

    yield make
    for handle in handles:
        handle.kill()
    assert multiprocessing.active_children() == []


@pytest.fixture
def split_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[LocalStore, str]:
    """The three-page fixture split into a local store; the PDF deadline set to 1 s."""
    monkeypatch.setenv("PARSER_DEADLINE_PDF_PAGE_S", str(DEADLINE_S))
    work_dir = work_dir_for(THREE_PAGE, tmp_path)
    work_dir.mkdir(parents=True)
    split_pdf(THREE_PAGE, work_dir)
    return LocalStore(tmp_path), work_id_for(THREE_PAGE)


def _exists(pid: int) -> bool:
    """True while ``pid`` runs; a zombie (dead, not yet reaped) counts as gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rpartition(")")[2].split()[0] != "Z"


def _wait_gone(pid: int, seconds: float = 3.0) -> bool:
    give_up = time.monotonic() + seconds
    while _exists(pid):
        if time.monotonic() > give_up:
            return False
        time.sleep(0.02)
    return True


def _record_file(store: LocalStore, work_id: str, page_number: int) -> Path:
    return store.root / page_pipeline._record_key(work_id, page_number)


def _attempts(store: LocalStore, work_id: str, page_number: int) -> int:
    return page_pipeline._read_attempts(store, work_id, page_number)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_deadline_kills_worker_and_writes_timeout_record(
    make_worker: Callable[..., ParseWorker], split_store: tuple[LocalStore, str]
) -> None:
    store, work_id = split_store
    worker = make_worker(_scripted_target)
    pid = worker.start()

    started = time.monotonic()
    record = page_pipeline._process_page(store, work_id, 2, False, 3, runner=worker)
    elapsed = time.monotonic() - started

    assert DEADLINE_S <= elapsed < DEADLINE_S + 5
    assert record["state"] == "failed"
    assert record["reason_code"] == "timeout"
    assert record["reason"] == f"timeout: no result after {DEADLINE_S} s (pdf)"
    assert record["limits"] == {"deadline_s": DEADLINE_S}
    assert record["parse_worker_pid"] is None
    assert record["worker_pid"] == os.getpid()  # the service process wrote it
    assert record["markdown"] == ""
    assert _record_file(store, work_id, 2).exists()
    assert _attempts(store, work_id, 2) == 1  # counted before the parse, not changed
    # Killed and reaped: no worker, not even a zombie.
    assert not worker.ready and worker.pid is None
    assert not Path(f"/proc/{pid}").exists()
    # Final under the same deadline (D5): a re-run would not archive it.
    assert page_pipeline._is_final(record, page_pipeline.current_limits("pdf"))


def test_next_request_after_timeout_succeeds_on_a_new_worker(
    make_worker: Callable[..., ParseWorker], split_store: tuple[LocalStore, str]
) -> None:
    store, work_id = split_store
    lost: list[str] = []
    worker = make_worker(_scripted_target, on_lost=lost.append)
    first = worker.start()
    timed_out = page_pipeline._process_page(store, work_id, 2, False, 3, runner=worker)
    assert timed_out["reason_code"] == "timeout"
    assert lost == ["timeout"]

    second = worker.start()  # what the page service's respawn does
    record = page_pipeline._process_page(store, work_id, 1, False, 3, runner=worker)

    assert second != first
    assert record["state"] == "ok"
    assert record["parse_worker_pid"] == second
    assert record["markdown"] == f"page 1 by {second}"
    assert record["reason_code"] is None
    assert "limits" not in record


def test_old_worker_is_reaped_before_the_new_one_starts(
    make_worker: Callable[..., ParseWorker], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each new worker's warm-up fails if the previous one still exists (running
    # or as an unreaped zombie), so a start that overlapped would raise here.
    worker = make_worker(_scripted_target, _check_previous_worker_reaped)
    first = worker.start()

    # A restart of a live, idle worker.
    monkeypatch.setenv(PREVIOUS_PID_ENV, str(first))
    second = worker.start()
    assert second != first and not Path(f"/proc/{first}").exists()

    # A respawn after a timeout.
    monkeypatch.setenv(PREVIOUS_PID_ENV, str(second))
    with pytest.raises(WorkerTimeout):
        worker.run(("pdf", "unused", 2, False, None), DEADLINE_S)
    assert not Path(f"/proc/{second}").exists()
    third = worker.start()
    assert third not in (first, second) and worker.ready


def test_hung_native_call_is_killed(
    make_worker: Callable[..., ParseWorker], tmp_path: Path
) -> None:
    worker = make_worker(_native_hang_target)
    pid = worker.start()
    pids_file = tmp_path / "pids"
    outcome: list[BaseException] = []

    def call() -> None:
        try:
            worker.run(("pdf", str(pids_file), 1, False, None), 2.0)
        except BaseException as exc:  # noqa: BLE001 — inspected below
            outcome.append(exc)

    caller = threading.Thread(target=call)
    started = time.monotonic()
    caller.start()
    while not pids_file.exists() or not pids_file.read_text():
        assert time.monotonic() - started < 10, "the target never reached its native sleep"
        time.sleep(0.02)
    worker_pid, grandchild = (int(n) for n in pids_file.read_text().split())
    assert worker_pid == pid

    # SIGTERM does not stop it: the call is in native code with SIGTERM ignored.
    os.kill(pid, signal.SIGTERM)
    time.sleep(0.3)
    assert _exists(pid)

    caller.join(10)
    assert not caller.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], WorkerTimeout)
    assert time.monotonic() - started < 2.0 + 5
    # The whole process group was SIGKILLed: the worker is reaped and the
    # grandchild is gone too.
    assert not Path(f"/proc/{pid}").exists()
    assert _wait_gone(grandchild)


def test_timeout_record_loses_to_an_existing_record(
    make_worker: Callable[..., ParseWorker], split_store: tuple[LocalStore, str]
) -> None:
    store, work_id = split_store
    worker = make_worker(_scripted_target)
    worker.start()

    def other_pod_writes() -> None:
        page_pipeline._write_record(store, work_id, 2, "ok", None, "from another pod", 1)

    writer = threading.Timer(0.3, other_pod_writes)
    writer.start()
    try:
        record = page_pipeline._process_page(store, work_id, 2, False, 3, runner=worker)
    finally:
        writer.join()

    assert record["state"] == "ok" and record["markdown"] == "from another pod"
    assert "reason_code" in record and record["reason_code"] is None
    stored = page_pipeline._load_record(store, work_id, 2)
    assert stored == record  # first write wins; the timeout was discarded
    assert not worker.ready  # the worker was still killed at the deadline


def test_worker_exception_becomes_a_failed_reply_and_the_worker_lives(
    make_worker: Callable[..., ParseWorker], split_store: tuple[LocalStore, str]
) -> None:
    store, work_id = split_store
    worker = make_worker(_scripted_target)
    pid = worker.start()

    failed = page_pipeline._process_page(store, work_id, 3, False, 3, runner=worker)
    assert failed["state"] == "failed"
    assert failed["reason_code"] == "exception"
    assert failed["reason"] == "exception: RuntimeError: boom on page 3"
    assert failed["parse_worker_pid"] == pid
    assert "limits" not in failed

    ok = page_pipeline._process_page(store, work_id, 1, False, 3, runner=worker)
    assert ok["state"] == "ok" and ok["parse_worker_pid"] == pid
    assert worker.ready and worker.pid == pid


def test_worker_death_raises_worker_died_and_writes_no_record(
    make_worker: Callable[..., ParseWorker], split_store: tuple[LocalStore, str]
) -> None:
    store, work_id = split_store
    lost: list[str] = []
    worker = make_worker(_dying_target, on_lost=lost.append)
    pid = worker.start()

    with pytest.raises(WorkerDied) as died:
        page_pipeline._process_page(store, work_id, 1, False, 3, runner=worker)

    assert died.value.pid == pid and died.value.exitcode == 3
    assert not _record_file(store, work_id, 1).exists()
    assert _attempts(store, work_id, 1) == 1  # counted; the driver's retry path decides
    assert lost == ["died"]
    assert not worker.ready and not Path(f"/proc/{pid}").exists()
