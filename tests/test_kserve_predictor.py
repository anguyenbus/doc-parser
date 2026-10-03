"""
test_kserve_predictor.py — the KServe custom predictor (``parser_service.kserve_predictor``).

The model is driven directly and through KServe's own REST app, so the status
codes the remote executor relies on (200, 400, 409, 429) are checked end to end.
``parse_to_markdown`` is the fake from ``test_page_pipeline.py``; no models load
(``warmup=False``). Skipped when the kserve SDK (the ``serve`` extra) is missing.

Run ONLY these tests:
    uv run pytest tests/test_kserve_predictor.py
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from test_page_pipeline import THREE_PAGE, _install_fake, _result

pytest.importorskip("kserve")
pytestmark = pytest.mark.kserve

from fastapi import HTTPException  # noqa: E402
from kserve.errors import InvalidInput  # noqa: E402

from parser_service import kserve_predictor, page_pipeline  # noqa: E402
from parser_service.kserve_predictor import PageParserModel, parse_instance  # noqa: E402
from parser_service.page_pipeline import KSERVE_MODEL_NAME, file_id_for  # noqa: E402
from parser_service.store import LocalStore  # noqa: E402

FILE_ID = file_id_for(THREE_PAGE)


def _payload(**overrides: Any) -> dict[str, Any]:
    instance = {"file_id": FILE_ID, "page": 1, "escalate": True, "max_attempts": 3}
    instance.update(overrides)
    return {"instances": [instance]}


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalStore:
    _install_fake(monkeypatch, lambda n: _result(f"Page {n} text."))
    store = LocalStore(tmp_path)
    page_pipeline._split(store, FILE_ID, THREE_PAGE)
    return store


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"instances": []},
        {"instances": [{}, {}]},
        _payload(file_id="../etc"),
        _payload(file_id=""),
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
    assert parse_instance(_payload(page=7, escalate=False)) == (FILE_ID, 7, False, 3)


def test_predict_parses_the_page_into_the_store(store: LocalStore, tmp_path: Path) -> None:
    model = PageParserModel(KSERVE_MODEL_NAME, store, warmup=False)
    model.load()
    response = asyncio.run(model.predict(_payload(page=2)))

    [summary] = response["predictions"]
    assert summary["state"] == "ok" and summary["page"] == 2 and summary["attempt"] == 1
    assert (tmp_path / f"work/{FILE_ID}/results/0002.json").exists()


def test_a_second_page_while_busy_is_refused_with_429(store: LocalStore) -> None:
    model = PageParserModel(KSERVE_MODEL_NAME, store, warmup=False)
    model._slot.acquire()
    try:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(model.predict(_payload()))
        assert raised.value.status_code == 429
    finally:
        model._slot.release()
    assert not store.exists(f"work/{FILE_ID}/attempts/0001")


def test_load_warms_the_converter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        kserve_predictor, "parse_to_markdown", lambda path, escalate: calls.append((path, escalate))
    )
    model = PageParserModel(KSERVE_MODEL_NAME, LocalStore(tmp_path))
    assert model.load() and model.ready
    assert calls == [(kserve_predictor.WARMUP_PDF, False)]
    assert kserve_predictor.WARMUP_PDF.is_file()


def test_status_codes_through_the_kserve_app(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    from kserve.model_repository import ModelRepository
    from kserve.protocol.dataplane import DataPlane
    from kserve.protocol.model_repository_extension import ModelRepositoryExtension
    from kserve.protocol.rest.server import RESTServer

    model = PageParserModel(KSERVE_MODEL_NAME, store, warmup=False)
    model.load()
    repository = ModelRepository()
    repository.update(model)
    server = RESTServer(
        "kserve.model_server:app",
        DataPlane(model_registry=repository),
        ModelRepositoryExtension(model_registry=repository),
        http_port=0,
    )
    server.create_application()
    from kserve.model_server import app

    url = f"/v1/models/{KSERVE_MODEL_NAME}:predict"
    with TestClient(app) as client:
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

    monkeypatch.setattr(page_pipeline, "parse_to_markdown", slow)
    model = PageParserModel(KSERVE_MODEL_NAME, store, warmup=False)

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
