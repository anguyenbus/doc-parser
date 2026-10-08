"""
test_remote_executor.py — the page pipeline with pages sent to a page service.

A stdlib HTTP server in a thread stands in for the KServe InferenceService. By
default it answers like the real predictor, calling ``process_page_request`` on
the same store; a test can script refusals, crashes and errors per request.
``parse_to_markdown`` is the fake from ``test_page_pipeline.py``, so these tests
are offline and fast.

Run ONLY these tests:
    uv run pytest tests/test_remote_executor.py
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from test_page_pipeline import THREE_PAGE, _install_fake, _result

from parser_service import page_pipeline
from parser_service.page_pipeline import (
    PipelineConfigMismatch,
    RemotePageError,
    file_id_for,
    kserve_predict_url,
    parse_pdf_pages_in_store,
    process_page_request,
    work_id_for,
)
from parser_service.store import LocalStore

FILE_ID = file_id_for(THREE_PAGE)
WORK_ID = work_id_for(THREE_PAGE)

# What the scripted server does with one request: serve it like the predictor,
# answer with a status code, or count an attempt and drop the connection (a pod
# killed mid-page).
SERVE = "serve"
CRASH = "crash"
Behaviour = Callable[[int, int], "str | int"]  # (page, n-th request for it) -> action


class PageService:
    """The scripted page service. Records every request it receives."""

    def __init__(self, store: LocalStore, behaviour: Behaviour | None = None) -> None:
        self.store = store
        self.behaviour = behaviour or (lambda page, n: SERVE)
        self.requests: list[dict[str, Any]] = []
        self.client_ports: list[int] = []
        self.connection_headers: list[str | None] = []
        self._lock = threading.Lock()
        # One page parsed at a time, as on a real pod: the server answers on many
        # threads, and pdfium (the blank-page probe) is not thread-safe.
        self._parse_lock = threading.Lock()

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", 0))
        instance = json.loads(handler.rfile.read(length))["instances"][0]
        page = instance["page"]
        with self._lock:
            self.requests.append(instance)
            self.client_ports.append(handler.client_address[1])
            self.connection_headers.append(handler.headers.get("Connection"))
            nth = sum(1 for r in self.requests if r["page"] == page)
        action = self.behaviour(page, nth)
        if action == CRASH:
            attempts = page_pipeline._attempts_key(instance["work_id"], page)
            current = page_pipeline._read_attempts(self.store, instance["work_id"], page)
            self.store.write(attempts, str(current + 1).encode())
            handler.close_connection = True
            handler.connection.shutdown(2)
            return
        if isinstance(action, int):
            self._reply(handler, action, {"error": f"scripted {action}"})
            return
        try:
            with self._parse_lock:
                summary = process_page_request(
                    self.store, instance["work_id"], page, instance["escalate"], instance["max_attempts"]
                )
        except PipelineConfigMismatch as exc:
            self._reply(handler, 409, {"error": str(exc)})
            return
        self._reply(handler, 200, {"predictions": [summary]})

    @staticmethod
    def _reply(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)


@contextmanager
def running(service: PageService) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 — the stdlib's name
            assert self.path == "/v1/models/page-parser:predict"
            service.handle(self)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(page_pipeline, "_sleep", lambda seconds: None)


@pytest.fixture
def fake_parse(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    return _install_fake(monkeypatch, lambda n: _result(f"Page {n} text."))


def _remote(store_dir: Path, endpoint: str, **kwargs: Any) -> dict[str, Any]:
    return parse_pdf_pages_in_store(
        THREE_PAGE, str(store_dir), executor="kserve", endpoint=endpoint, **kwargs
    )


def test_predict_url() -> None:
    assert kserve_predict_url("http://svc.ns/") == "http://svc.ns/v1/models/page-parser:predict"


def test_remote_run_matches_a_local_run(tmp_path: Path, fake_parse: list[int]) -> None:
    local = parse_pdf_pages_in_store(THREE_PAGE, str(tmp_path / "local"), workers=0)
    service = PageService(LocalStore(tmp_path / "remote"))
    with running(service) as endpoint:
        remote = _remote(tmp_path / "remote", endpoint, concurrency=3)

    assert remote["state"] == local["state"] == "complete"
    assert sorted(r["page"] for r in service.requests) == [1, 2, 3]
    out = f"out/{FILE_ID}/output.md"
    assert (tmp_path / "remote" / out).read_bytes() == (tmp_path / "local" / out).read_bytes()


def test_every_request_is_a_new_connection(tmp_path: Path, fake_parse: list[int]) -> None:
    service = PageService(LocalStore(tmp_path), lambda page, n: 429 if n == 1 else SERVE)
    with running(service) as endpoint:
        _remote(tmp_path, endpoint, concurrency=1)

    assert len(service.requests) == 6
    assert len(set(service.client_ports)) == 6
    assert set(service.connection_headers) == {"close"}


def test_a_busy_refusal_costs_no_attempt(tmp_path: Path, fake_parse: list[int]) -> None:
    service = PageService(
        LocalStore(tmp_path), lambda page, n: 429 if page == 2 and n <= 3 else SERVE
    )
    with running(service) as endpoint:
        result = _remote(tmp_path, endpoint, max_attempts=1)

    assert result["state"] == "complete"
    record = json.loads((tmp_path / f"work/{WORK_ID}/results/0002.json").read_text())
    assert record["attempt"] == 1
    assert (tmp_path / f"work/{WORK_ID}/attempts/0002").read_text() == "1"


def test_a_page_whose_pod_died_is_sent_again(tmp_path: Path, fake_parse: list[int]) -> None:
    service = PageService(
        LocalStore(tmp_path), lambda page, n: CRASH if page == 3 and n == 1 else SERVE
    )
    with running(service) as endpoint:
        result = _remote(tmp_path, endpoint, max_attempts=3)

    assert result["state"] == "complete"
    record = json.loads((tmp_path / f"work/{WORK_ID}/results/0003.json").read_text())
    assert record["attempt"] == 2


def test_a_page_that_kills_every_pod_is_finalized_failed(
    tmp_path: Path, fake_parse: list[int]
) -> None:
    service = PageService(LocalStore(tmp_path), lambda page, n: CRASH if page == 2 else SERVE)
    with running(service) as endpoint:
        result = _remote(tmp_path, endpoint, max_attempts=2)

    assert result["state"] == "failed"
    assert [r["page"] for r in service.requests].count(2) == 2
    [not_ok] = result["not_ok_pages"]
    assert not_ok["page"] == 2 and not_ok["state"] == "failed"
    assert not_ok["reason"].startswith("no_result_after_2_attempts")
    assert not (tmp_path / f"out/{FILE_ID}/output.md").exists()


def test_a_refused_request_stops_the_run_loudly(tmp_path: Path, fake_parse: list[int]) -> None:
    service = PageService(LocalStore(tmp_path), lambda page, n: 400)
    with running(service) as endpoint, pytest.raises(RemotePageError, match="400"):
        _remote(tmp_path, endpoint)
    assert fake_parse == []


def test_errors_that_never_start_the_page_are_bounded(
    tmp_path: Path, fake_parse: list[int]
) -> None:
    service = PageService(LocalStore(tmp_path), lambda page, n: 500 if page == 1 else SERVE)
    with running(service) as endpoint, pytest.raises(RemotePageError, match="without the page"):
        _remote(tmp_path, endpoint, max_attempts=2, concurrency=1)
    assert [r["page"] for r in service.requests].count(1) == 2 + page_pipeline._REMOTE_EXTRA_ERRORS + 1


def test_endless_busy_refusals_hit_the_deadline(
    tmp_path: Path, fake_parse: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(page_pipeline, "_REMOTE_BUSY_DEADLINE", 0.2)
    service = PageService(LocalStore(tmp_path), lambda page, n: 503)
    with running(service) as endpoint, pytest.raises(RemotePageError, match="refused it for"):
        _remote(tmp_path, endpoint)


def test_an_unreachable_service_is_reported(tmp_path: Path, fake_parse: list[int]) -> None:
    with pytest.raises(RemotePageError, match="without the page"):
        _remote(tmp_path, "http://127.0.0.1:9", max_attempts=1, concurrency=1)


def test_kserve_needs_an_endpoint(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="endpoint"):
        parse_pdf_pages_in_store(THREE_PAGE, str(tmp_path), executor="kserve")
    with pytest.raises(ValueError, match="executor"):
        parse_pdf_pages_in_store(THREE_PAGE, str(tmp_path), executor="ray")


# --- the service side -------------------------------------------------------


def _split(tmp_path: Path) -> LocalStore:
    store = LocalStore(tmp_path)
    page_pipeline._split(store, WORK_ID, THREE_PAGE)
    return store


def test_page_request_is_idempotent(tmp_path: Path, fake_parse: list[int]) -> None:
    store = _split(tmp_path)
    first = process_page_request(store, WORK_ID, 2, True, 3)
    second = process_page_request(store, WORK_ID, 2, True, 3)

    assert first == second
    assert first["state"] == "ok" and first["page"] == 2 and first["attempt"] == 1
    assert first["worker_host"]
    assert fake_parse == [2]


def test_page_request_refuses_an_unknown_file_or_page(tmp_path: Path, fake_parse: list[int]) -> None:
    store = LocalStore(tmp_path)
    with pytest.raises(ValueError, match="no manifest"):
        process_page_request(store, WORK_ID, 1, True, 3)
    page_pipeline._split(store, WORK_ID, THREE_PAGE)
    with pytest.raises(ValueError, match="not page 4"):
        process_page_request(store, WORK_ID, 4, True, 3)
    assert fake_parse == []


def test_page_request_refuses_another_pipeline_config(
    tmp_path: Path, fake_parse: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _split(tmp_path)
    monkeypatch.setenv("PARSER_ESCALATION_ENGINE", "textract")
    with pytest.raises(PipelineConfigMismatch):
        process_page_request(store, WORK_ID, 1, True, 3)
    assert fake_parse == []
