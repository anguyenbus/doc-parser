"""
test_kserve_predictor.py — the KServe custom predictor (``parser_service.service.predictor``).

The model is driven directly and through KServe's own REST app, so the status
codes the remote executor relies on (200, 400, 409, 429, 500, 503) are checked
end to end. Most tests use ``InProcessWorker``, which has the parse worker's
interface but parses in this process, so the fake ``parse_to_markdown`` from
``test_page_pipeline.py`` applies and no models load. The worker tests (spec D4)
spawn real ``ParseWorker`` processes with the fake module-level targets of
``test_page_worker.py`` and the warm-ups below; deadlines are 1 s. Only
``test_service_process_does_not_import_docling`` (marked ``slow``) loads Docling,
in a worker of a fresh service process. Skipped when the kserve SDK (the
``serve`` extra) is missing.

Run ONLY these tests:
    uv run pytest tests/test_kserve_predictor.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from test_page_pipeline import THREE_PAGE, _install_fake, _result
from test_page_worker import _dying_target, _scripted_target

pytest.importorskip("kserve")
pytestmark = pytest.mark.kserve

from fastapi import HTTPException  # noqa: E402
from kserve.errors import InvalidInput  # noqa: E402
from prometheus_client import REGISTRY  # noqa: E402

from parser_service.formats import pdf  # noqa: E402
from parser_service.formats.result import Reply, exception_reply  # noqa: E402
from parser_service.pipeline import config, intake, layout  # noqa: E402
from parser_service.pipeline.intake import work_id_for  # noqa: E402
from parser_service.pipeline.remote_runner import KSERVE_MODEL_NAME  # noqa: E402
from parser_service.service import worker as page_worker  # noqa: E402
from parser_service.service.predictor import PageParserModel, parse_instance  # noqa: E402
from parser_service.service.worker import ParseWorker, Request, WorkerNotReady  # noqa: E402
from parser_service.storage.store import LocalStore  # noqa: E402

WORK_ID = work_id_for(THREE_PAGE)
HOLD_ENV = "TEST_KSERVE_HOLD_WARMUP"
FAIL_ENV = "TEST_KSERVE_FAIL_WARMUP"
GRANDCHILD_ENV = "TEST_KSERVE_GRANDCHILD_FILE"


# ---------------------------------------------------------------------------
# Module-level warm-ups: they run in the spawned worker, steered by files
# ---------------------------------------------------------------------------


def _held_warmup() -> None:
    """Wait (up to 30 s) while the file named by ``HOLD_ENV`` exists: a slow warm-up."""
    hold = os.environ.get(HOLD_ENV)
    give_up = time.monotonic() + 30
    while hold and Path(hold).exists() and time.monotonic() < give_up:
        time.sleep(0.05)


def _failing_warmup() -> None:
    """Fail while the file named by ``FAIL_ENV`` exists: a worker that never gets ready."""
    flag = os.environ.get(FAIL_ENV)
    if flag and Path(flag).exists():
        raise RuntimeError("warm-up failed")


def _grandchild_warmup() -> None:
    """Start a grandchild in the worker's group; write its PID to ``GRANDCHILD_ENV``."""
    grandchild = subprocess.Popen(["sleep", "60"])
    Path(os.environ[GRANDCHILD_ENV]).write_text(str(grandchild.pid))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class InProcessWorker:
    """The parse worker's interface, parsing in this process (the test fakes apply)."""

    def __init__(self, on_lost: Callable[[str], None]) -> None:
        self.on_lost = on_lost
        self.ready = False
        self.pid: int | None = None
        self.starts = 0

    def start(self) -> int:
        self.starts += 1
        self.pid, self.ready = os.getpid(), True
        return self.pid

    def run(self, request: Request, deadline_s: float) -> Reply:
        if not self.ready:
            raise WorkerNotReady("stopped")
        kind, unit_path, page_number, escalate, _cap = request
        try:
            state, reason, markdown, extra = pdf.parse_page(Path(unit_path), page_number, escalate)
        except Exception as exc:  # noqa: BLE001 — as the real worker does
            return exception_reply(exc)
        return state, reason, None, markdown, extra

    def kill(self) -> None:
        self.pid, self.ready = None, False


def _in_process_model(store: LocalStore) -> PageParserModel:
    return PageParserModel(KSERVE_MODEL_NAME, store, worker_factory=InProcessWorker)


def _never_terminate() -> None:
    raise AssertionError("the service would have sent itself SIGTERM")


@pytest.fixture
def make_model(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., PageParserModel]]:
    """Models with real parse workers; the PDF deadline is 1 s. All stopped in teardown."""
    monkeypatch.setenv("PARSER_DEADLINE_PDF_PAGE_S", "1")
    models: list[PageParserModel] = []

    def make(
        target: Any = _scripted_target,
        warmup: Any = None,
        terminate: Callable[[], None] = _never_terminate,
    ) -> PageParserModel:
        model = PageParserModel(
            KSERVE_MODEL_NAME,
            store,
            worker_factory=lambda on_lost: ParseWorker(
                target, warmup, start_timeout_s=60, on_lost=on_lost
            ),
            terminate=terminate,
        )
        models.append(model)
        return model

    yield make
    for model in models:
        model.stop()
    assert multiprocessing.active_children() == []


def _wait_until(condition: Callable[[], bool], seconds: float = 30.0) -> None:
    give_up = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < give_up, "condition not reached in time"
        time.sleep(0.05)


def _exists(pid: int) -> bool:
    """True while ``pid`` runs; a zombie (dead, not yet reaped) counts as gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):  # gone, or reaped while being read
        return False
    return stat.rpartition(")")[2].split()[0] != "Z"


def _wait_gone(pid: int, seconds: float = 5.0) -> bool:
    give_up = time.monotonic() + seconds
    while _exists(pid):
        if time.monotonic() > give_up:
            return False
        time.sleep(0.02)
    return True


def _counter(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@contextmanager
def _kserve_client(model: PageParserModel) -> Iterator[Any]:
    """A test client of KServe's REST routes and error handlers serving ``model``.

    A fresh app each time: KServe's module-level app takes its middleware once.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from kserve.model_repository import ModelRepository
    from kserve.protocol.dataplane import DataPlane
    from kserve.protocol.model_repository_extension import ModelRepositoryExtension
    from kserve.protocol.rest.server import RESTServer

    repository = ModelRepository()
    repository.update(model)
    server = RESTServer(
        "kserve.model_server:app",
        DataPlane(model_registry=repository),
        ModelRepositoryExtension(model_registry=repository),
        http_port=0,
    )
    app = FastAPI()
    server._register_endpoints(app)
    server._add_exception_handlers(app)
    with TestClient(app) as client:
        yield client


URL = f"/v1/models/{KSERVE_MODEL_NAME}:predict"


def _payload(**overrides: Any) -> dict[str, Any]:
    instance = {"work_id": WORK_ID, "page": 1, "escalate": True, "max_attempts": 3}
    instance.update(overrides)
    return {"instances": [instance]}


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalStore:
    _install_fake(monkeypatch, lambda n: _result(f"Page {n} text."))
    store = LocalStore(tmp_path)
    intake._split(store, WORK_ID, THREE_PAGE)
    return store


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"instances": []},
        {"instances": [{}, {}]},
        _payload(work_id="../etc"),
        _payload(work_id=""),
        _payload(work_id="three_page-0123456789abcdef"),  # a file id, not a work id
        _payload(work_id="A" * 64),
        _payload(page=0),
        _payload(page=True),
        _payload(page="1"),
        _payload(escalate="yes"),
        _payload(max_attempts=0),
    ],
)
def test_bad_requests_are_invalid_input(payload: dict[str, Any]) -> None:
    with pytest.raises(InvalidInput):
        parse_instance(payload)


def test_parse_instance_reads_one_page() -> None:
    assert parse_instance(_payload(page=7, escalate=False)) == (WORK_ID, 7, False, 3)


def test_predict_parses_the_page_into_the_store(store: LocalStore, tmp_path: Path) -> None:
    model = _in_process_model(store)
    model.load()
    response = asyncio.run(model.predict(_payload(page=2)))

    [summary] = response["predictions"]
    assert summary["state"] == "ok" and summary["page"] == 2 and summary["attempt"] == 1
    assert (tmp_path / f"work/{WORK_ID}/results/0002.json").exists()


def test_a_second_page_while_busy_is_refused_with_429(store: LocalStore) -> None:
    model = _in_process_model(store)
    model.load()
    model._slot.acquire()
    try:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(model.predict(_payload()))
        assert raised.value.status_code == 429
    finally:
        model._slot.release()
    assert not store.exists(f"work/{WORK_ID}/attempts/0001")


def test_load_starts_the_worker(tmp_path: Path) -> None:
    model = _in_process_model(LocalStore(tmp_path))
    assert not model.ready and not asyncio.run(model.healthy())
    assert model.load() and model.ready and asyncio.run(model.healthy())
    assert isinstance(model._worker, InProcessWorker) and model._worker.starts == 1
    model.start()  # KServe's start hook leaves readiness to the worker
    model.stop()
    assert not asyncio.run(model.healthy())
    # The warm-up (in the worker) parses the bundled page.
    assert page_worker.WARMUP_PDF.is_file()


def test_status_codes_through_the_kserve_app(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _in_process_model(store)
    model.load()
    url = URL
    with _kserve_client(model) as client:
        assert client.get(f"/v1/models/{KSERVE_MODEL_NAME}").json()["ready"] is True

        ok = client.post(url, json=_payload(page=3))
        assert ok.status_code == 200 and ok.json()["predictions"][0]["state"] == "ok"

        assert client.post(url, json=_payload(page=9)).status_code == 400
        assert client.post(url, json={"instances": []}).status_code == 400

        model._slot.acquire()
        try:
            assert client.post(url, json=_payload(page=1)).status_code == 429
        finally:
            model._slot.release()

        monkeypatch.setenv("PARSER_ESCALATION_ENGINE", "textract")
        assert client.post(url, json=_payload(page=1)).status_code == 409


def test_one_page_at_a_time_under_concurrent_requests(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = threading.Event(), threading.Event()

    def slow(path: Path, escalate: bool = True) -> dict[str, Any]:
        started.set()
        release.wait(5)
        return _result("slow")

    monkeypatch.setattr(pdf, "parse_to_markdown", slow)
    model = _in_process_model(store)
    model.load()

    async def both() -> list[Any]:
        first = asyncio.create_task(model.predict(_payload(page=1)))
        await asyncio.to_thread(started.wait, 5)
        try:
            await model.predict(_payload(page=2))
            second: Any = "accepted"
        except HTTPException as exc:
            second = exc.status_code
        release.set()
        return [await first, second]

    first, second = asyncio.run(both())
    assert first["predictions"][0]["page"] == 1
    assert second == 429


# ---------------------------------------------------------------------------
# The parse worker in the page service (spec D4, sub-step 2)
# ---------------------------------------------------------------------------


def _record(store: LocalStore, page: int) -> Path:
    return store.root / layout._record_key(WORK_ID, page)


def _attempts(store: LocalStore, page: int) -> int:
    return layout._read_attempts(store, WORK_ID, page)


def test_timeout_returns_200_with_a_failed_summary(
    store: LocalStore,
    make_model: Callable[..., PageParserModel],
    caplog: pytest.LogCaptureFixture,
) -> None:
    model = make_model()
    assert model.load()
    timeouts = _counter("parser_timeouts_total", kind="pdf")
    restarts = _counter("parser_worker_restarts_total", cause="timeout")
    worker_pid = model._worker.pid

    with caplog.at_level(logging.INFO), _kserve_client(model) as client:
        response = client.post(URL, json=_payload(page=2))  # page 2 hangs in the worker

    assert response.status_code == 200
    [summary] = response.json()["predictions"]
    assert summary["state"] == "failed" and summary["reason_code"] == "timeout"
    assert summary["reason"] == "timeout: no result after 1 s (pdf)"
    record = json.loads(_record(store, 2).read_text())
    assert record["reason_code"] == "timeout" and record["limits"] == {"deadline_s": 1}
    assert _counter("parser_timeouts_total", kind="pdf") == timeouts + 1
    assert _counter("parser_worker_restarts_total", cause="timeout") == restarts + 1
    events = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith("{")]
    [timeout_event] = [e for e in events if e["event"] == "unit_timeout"]
    assert timeout_event["unit"] == 2 and timeout_event["deadline_s"] == 1
    assert timeout_event["parse_worker_pid"] == worker_pid and timeout_event["kind"] == "pdf"
    # The respawned worker serves the next page.
    _wait_until(model._is_ready)
    assert model._worker.pid != worker_pid
    [ok] = asyncio.run(model.predict(_payload(page=1)))["predictions"]
    assert ok["state"] == "ok"


def test_requests_during_respawn_get_503_and_cost_no_attempt(
    store: LocalStore,
    make_model: Callable[..., PageParserModel],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hold = tmp_path / "hold-warmup"
    monkeypatch.setenv(HOLD_ENV, str(hold))
    model = make_model(warmup=_held_warmup)
    assert model.load()

    hold.touch()  # the next warm-up waits until the file is gone
    [timed_out] = asyncio.run(model.predict(_payload(page=2)))["predictions"]
    assert timed_out["reason_code"] == "timeout"

    assert not model.ready and not asyncio.run(model.healthy())
    with pytest.raises(HTTPException) as raised:
        asyncio.run(model.predict(_payload(page=1)))
    assert raised.value.status_code == 503
    assert not store.exists(f"work/{WORK_ID}/attempts/0001")
    assert model._slot.acquire(blocking=False)  # the slot was never taken
    model._slot.release()
    with _kserve_client(model) as client:
        assert client.get(f"/v1/models/{KSERVE_MODEL_NAME}").status_code == 503
        assert client.post(URL, json=_payload(page=1)).status_code == 503
    assert not store.exists(f"work/{WORK_ID}/attempts/0001")

    hold.unlink()
    _wait_until(model._is_ready)
    [ok] = asyncio.run(model.predict(_payload(page=1)))["predictions"]
    assert ok["state"] == "ok" and ok["attempt"] == 1


def test_worker_death_returns_500_writes_no_record_and_driver_resends(
    store: LocalStore, make_model: Callable[..., PageParserModel]
) -> None:
    """The service side. The driver resending on that 500 (or finalizing the page at
    the limit) is ``test_remote_executor.py::test_a_500_after_a_worker_death_is_sent_again_or_finalized``.
    """
    model = make_model(target=_dying_target)
    assert model.load()
    died = _counter("parser_worker_restarts_total", cause="died")
    worker_pid = model._worker.pid
    assert worker_pid is not None

    with _kserve_client(model) as client:
        response = client.post(URL, json=_payload(page=2))

    assert response.status_code == 500
    assert "died" in response.text
    assert not _record(store, 2).exists()
    assert _attempts(store, 2) == 1
    assert _counter("parser_worker_restarts_total", cause="died") == died + 1
    assert _wait_gone(worker_pid)
    _wait_until(model._is_ready)
    assert model._worker.pid != worker_pid


def test_three_failed_spawns_send_sigterm(
    store: LocalStore,
    make_model: Callable[..., PageParserModel],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flag = tmp_path / "fail-warmup"
    monkeypatch.setenv(FAIL_ENV, str(flag))
    terminated = threading.Event()
    model = make_model(warmup=_failing_warmup, terminate=terminated.set)
    assert model.load()
    failed = _counter("parser_worker_restarts_total", cause="start_failed")

    flag.touch()  # every new worker fails its warm-up from now on
    [timed_out] = asyncio.run(model.predict(_payload(page=2)))["predictions"]
    assert timed_out["reason_code"] == "timeout"

    assert terminated.wait(60)
    respawn = model._respawn_thread
    assert respawn is not None
    respawn.join(10)
    assert not respawn.is_alive()
    assert _counter("parser_worker_restarts_total", cause="start_failed") == failed + 3
    assert not model._is_ready() and model._worker.pid is None
    assert multiprocessing.active_children() == []


def test_shutdown_kills_the_worker_process_group(
    store: LocalStore,
    make_model: Callable[..., PageParserModel],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grandchild_file = tmp_path / "grandchild"
    monkeypatch.setenv(GRANDCHILD_ENV, str(grandchild_file))
    model = make_model(warmup=_grandchild_warmup)
    assert model.load()
    worker_pid = model._worker.pid
    assert worker_pid is not None
    grandchild_pid = int(grandchild_file.read_text())
    assert _exists(worker_pid) and _exists(grandchild_pid)
    assert os.getpgid(grandchild_pid) == worker_pid

    model.stop()  # what KServe calls on SIGTERM, after the server drained

    assert _wait_gone(worker_pid) and _wait_gone(grandchild_pid)
    assert not asyncio.run(model.healthy())
    with pytest.raises(HTTPException) as raised:
        asyncio.run(model.predict(_payload(page=1)))
    assert raised.value.status_code == 503


_SERVICE_SCRIPT = r"""
import asyncio, json, sys
from pathlib import Path

from parser_service.pipeline import config
from parser_service.pipeline import intake
from parser_service.pipeline import layout
from parser_service.service.predictor import PageParserModel
from parser_service.pipeline.intake import work_id_for
from parser_service.pipeline.remote_runner import KSERVE_MODEL_NAME
from parser_service.storage.store import LocalStore

source, root = Path(sys.argv[1]), Path(sys.argv[2])
store = LocalStore(root)
work_id = work_id_for(source)
intake._split(store, work_id, source)
model = PageParserModel(KSERVE_MODEL_NAME, store, terminate=lambda: sys.exit("terminate"))
try:
    assert model.load(), "the worker did not get ready"
    worker_pid = model._worker.pid
    payload = {"instances": [{"work_id": work_id, "page": 1, "escalate": False, "max_attempts": 1}]}
    [summary] = asyncio.run(model.predict(payload))["predictions"]
    loaded = sorted(
        name for name in sys.modules
        if name.partition(".")[0] in ("docling", "torch")
    )
    print(json.dumps({"summary": summary, "loaded": loaded, "worker_pid": worker_pid}))
finally:
    model.stop()
"""


@pytest.mark.slow
def test_service_process_does_not_import_docling(tmp_path: Path) -> None:
    """A fresh service process: ``load`` (the real Docling warm-up, in the worker)
    and one real page request. This test process may hold Docling from other
    tests, so the check runs in its own process."""
    result = subprocess.run(
        [sys.executable, "-c", _SERVICE_SCRIPT, str(THREE_PAGE), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["summary"]["state"] == "ok", report
    assert report["loaded"] == [], report["loaded"][:20]
    assert _wait_gone(report["worker_pid"], 10)


def test_disabled_kind_gives_409_and_page_over_count_gives_400(
    store: LocalStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec D3: a kind not in this pod's PARSER_ENABLED_KINDS is refused with 409,
    and a page past the manifest's page_count with 400; neither counts an attempt."""
    monkeypatch.setenv("PARSER_ENABLED_KINDS", "pdf")  # docx switched off on this pod
    docx = Path(__file__).parent / "fixtures" / "doc.docx"
    docx_work = intake.work_id_for(docx)
    assert docx_work.endswith(config.config_hash(config.pipeline_config("docx")))
    intake._intake(store, docx_work, docx, "docx")
    model = _in_process_model(store)
    assert model.load()

    with pytest.raises(HTTPException) as raised:
        asyncio.run(model.predict(_payload(work_id=docx_work, page=1)))
    assert raised.value.status_code == 409
    assert "not in this page service's PARSER_ENABLED_KINDS" in str(raised.value.detail)
    assert not (tmp_path / "work" / docx_work / "attempts").exists()

    with pytest.raises(InvalidInput):
        asyncio.run(model.predict(_payload(page=4)))  # the PDF has 3 pages
    assert not (tmp_path / "work" / WORK_ID / "attempts").exists()

    # Enabled on the pod: the request reaches the worker as a docx unit.
    monkeypatch.setenv("PARSER_ENABLED_KINDS", "pdf,docx")
    from parser_service import settings

    monkeypatch.setitem(settings.DEADLINE_ENV, "docx", ("TEST_DEADLINE_DOCX_S", 60))
    [summary] = asyncio.run(model.predict(_payload(work_id=docx_work, page=1)))["predictions"]
    # InProcessWorker parses PDFs only: the docx unit fails, as a record, on this pod.
    assert (summary["kind"], summary["state"]) == ("docx", "failed")
