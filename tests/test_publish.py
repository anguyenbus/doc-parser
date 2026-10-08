"""
test_publish.py — the ``published/`` hand-off (``parser_service.publish``).

Driven through ``parse_pdf_pages_in_store(..., publish=...)`` on ``MemoryStore``,
which records every write in order, so "written last" and "never rewritten" are
checked directly. ``parse_to_markdown`` is the fake from ``test_page_pipeline.py``.

Run ONLY these tests:
    uv run pytest tests/test_publish.py
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from test_page_pipeline import THREE_PAGE, _install_fake, _result
from test_page_pipeline_store import WORK_ID, MemoryStore, _fail_page_2_until

from parser_service import publish
from parser_service.page_pipeline import file_id_for, parse_pdf_pages_in_store
from parser_service.publish import PublishRequest, check_id, slugify

FILE_ID = file_id_for(THREE_PAGE)
DOC = "three-page"


def _run(store: MemoryStore, request: PublishRequest | None = None) -> dict[str, Any]:
    return parse_pdf_pages_in_store(
        THREE_PAGE, "unused", store=store, workers=0, publish=request or PublishRequest(DOC)
    )


def _json(store: MemoryStore, key: str) -> Any:
    return json.loads(store.read(key))


def _published(store: MemoryStore) -> list[str]:
    return [k for k in store.objects if k.startswith("published/")]


def _events(store: MemoryStore) -> list[dict[str, Any]]:
    return [_json(store, k) for k in sorted(_published(store)) if "/events/" in k]


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


def test_a_complete_file_is_published_with_the_manifest_written_last(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))

    result = _run(store, PublishRequest(DOC, "job-1", {"title": "Three pages"}))

    published = result["published"]
    assert published["type"] == "published"
    version = published["version"]
    prefix = f"published/v1/docs/{DOC}/{version}"
    doc_writes = [k for k in store.writes if k.startswith(prefix)]
    assert doc_writes[-1] == f"{prefix}/_manifest.json"  # the commit marker comes last
    assert sorted(doc_writes[:-1]) == sorted(
        [f"{prefix}/pages/000{n}.md" for n in (1, 2, 3)]
        + [f"{prefix}/document.md", f"{prefix}/document.json"]
    )
    # the event comes after the manifest, and status.json after everything
    event_key = published["event_key"]
    assert store.writes.index(event_key) > store.writes.index(f"{prefix}/_manifest.json")
    assert store.writes[-1] == f"out/{FILE_ID}/status.json"

    assert store.read(f"{prefix}/document.md") == store.read(f"out/{FILE_ID}/output.md")
    assert store.read(f"{prefix}/pages/0002.md") == b"text of page 2\n"
    manifest = _json(store, f"{prefix}/_manifest.json")
    for entry in manifest["files"]:
        data = store.read(f"{prefix}/{entry['path']}")
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["bytes"] == len(data)

    document = _json(store, f"{prefix}/document.json")
    assert document["state"] == "complete" and document["coverage"] == 1.0
    assert document["doc_id"] == DOC and document["job_id"] == "job-1"
    assert document["metadata"] == {"title": "Three pages"}
    assert document["work_id"] == WORK_ID and document["missing_pages"] == []
    markdown = store.read(f"{prefix}/document.md").decode()
    for page in document["pages"]:
        start, end = page["char_span"]
        assert markdown[start:end].startswith(f"==== page {page['page']} ===")
        assert page["file"] == f"pages/{page['page']:04d}.md"

    [event] = _events(store)
    assert event["type"] == "published" and event["version"] == version
    assert event["manifest"] == store.location(f"{prefix}/_manifest.json")
    assert event_key.startswith(f"published/v1/events/dt={manifest['published_at'][:10]}/")


def test_a_rerun_writes_nothing_new_under_published(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    first = _run(store)
    before = {k: store.objects[k] for k in _published(store)}
    store.writes.clear()

    second = _run(store)

    assert second["published"] == first["published"]
    assert {k: store.objects[k] for k in _published(store)} == before
    assert not [k for k in store.writes if k.startswith("published/")]


def test_an_incomplete_file_publishes_a_failed_event_and_no_document(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(monkeypatch, _fail_page_2_until({"now": False}))

    result = _run(store)

    assert result["state"] == "failed" and result["published"]["type"] == "failed"
    assert not [k for k in _published(store) if "/docs/" in k]
    [event] = _events(store)
    assert event["type"] == "failed" and event["missing_pages"] == [2]
    assert event["coverage"] == round(2 / 3, 4)
    assert event["not_ok_pages"][0]["reason"].startswith("exception: RuntimeError: boom")


def test_partial_versions_then_the_complete_version_after_repair(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    healthy = {"now": False}
    _install_fake(monkeypatch, _fail_page_2_until(healthy))

    partial = _run(store, PublishRequest(DOC, partial=True))["published"]

    assert partial["type"] == "partial" and partial["version"].endswith(".partial-0002")
    prefix = f"published/v1/docs/{DOC}/{partial['version']}"
    document = _json(store, f"{prefix}/document.json")
    assert document["state"] == "partial" and document["missing_pages"] == [2]
    assert not store.exists(f"{prefix}/pages/0002.md")
    assert "==== page 2 (missing: failed) ===" in store.read(f"{prefix}/document.md").decode()

    healthy["now"] = True
    complete = _run(store, PublishRequest(DOC, partial=True))["published"]

    assert complete["type"] == "published"
    assert partial["version"].startswith(complete["version"] + ".")
    assert store.exists(f"{prefix}/_manifest.json")  # the partial version stays as it was
    assert sorted(e["type"] for e in _events(store)) == ["partial", "published"]


def test_a_new_pipeline_config_is_a_new_version(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    monkeypatch.setenv("PARSER_ESCALATION_ENGINE", "vlm")
    first = _run(store)["published"]["version"]
    monkeypatch.setenv("PARSER_ESCALATION_ENGINE", "textract")
    second = _run(store)["published"]["version"]

    assert first != second
    assert {k.split("/")[4] for k in _published(store) if "/docs/" in k} == {first, second}


def test_deletion_is_an_event() -> None:
    store = MemoryStore()
    key = publish.publish_deletion(store, DOC, "job-2")
    event = _json(store, key)
    assert event["type"] == "deleted" and event["doc_id"] == DOC and event["job_id"] == "job-2"
    assert publish.publish_deletion(store, DOC, "job-2") == key  # a re-run adds nothing
    assert len(store.objects) == 1


@pytest.mark.parametrize("bad", ["", "Upper", "has space", "a/b", "../x", "-lead", "a..b", "x" * 129, 7])
def test_ids_must_be_safe_key_segments(bad: Any) -> None:
    with pytest.raises(ValueError):
        check_id(bad, "doc_id")
    with pytest.raises(ValueError):
        PublishRequest(DOC, job_id=bad)


def test_slugify_makes_a_valid_id() -> None:
    assert slugify("Annual Report (2025) FINAL.pdf") == "annual-report-2025-final.pdf"
    assert check_id(slugify("  Ünïcode name  "), "doc_id") == "n-code-name"
    with pytest.raises(ValueError):
        slugify("!!!")
