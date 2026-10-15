"""
test_publish.py — the ``published/`` hand-off (``parser_service.delivery.publish``).

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

from parser_service.delivery import publish
from parser_service.delivery.publish import PublishRequest, check_id, slugify
from parser_service.page_pipeline import file_id_for, parse_pdf_pages_in_store

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


# ---------------------------------------------------------------------------
# kind, unit, flags, handler version and email (Group 3b)
# ---------------------------------------------------------------------------

EMAIL = {
    "headers": {
        "from": "Header Marker <marker@example.com>",
        "to": "someone@example.com",
        "cc": None,
        "date": "Tue, 14 Oct 2026 09:00:00 +1100",
        "subject": "SUBJECT-MARKER",
    },
    "attachments": [{"name": "ATTACHMENT-MARKER.pdf", "type": "application/pdf", "size": 1234}],
}


def _single_unit(
    monkeypatch: pytest.MonkeyPatch, kind: str, text: str, extra: dict[str, Any]
) -> None:
    """Enable ``kind`` with a test handler returning ``text`` and record fields ``extra``."""
    from test_page_pipeline import _enable_test_kind

    from parser_service.formats import handlers as unit_handlers

    _enable_test_kind(monkeypatch, kind)

    def handler(path: Any, page_number: int, escalate: bool) -> Any:
        return "ok", None, None, text, {"route": "docling-kept", **extra}

    monkeypatch.setitem(unit_handlers.HANDLERS, kind, handler)


def test_document_json_and_event_carry_kind_unit_flags_handler_version(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # A PDF gets the new fields too.
    _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    published = _run(store)["published"]
    prefix = f"published/v1/docs/{DOC}/{published['version']}"
    document = _json(store, f"{prefix}/document.json")
    [event] = _events(store)
    for body in (document, event):
        assert (body["kind"], body["unit"], body["flags"], body["handler_version"]) == (
            "pdf",
            "page",
            [],
            "1",
        )
        assert body["schema_version"] == 1 and "email" not in body

    # A failed event names each missing page's reason_code.
    failed_store = MemoryStore()
    _install_fake(monkeypatch, _fail_page_2_until({"now": False}))
    parse_pdf_pages_in_store(
        THREE_PAGE, "unused", store=failed_store, workers=0, publish=PublishRequest(DOC)
    )
    [failed] = _events(failed_store)
    assert failed["type"] == "failed" and failed["kind"] == "pdf"
    assert [(p["page"], p["reason_code"]) for p in failed["not_ok_pages"]] == [(2, "exception")]

    # An email: headers and attachments in document.json, never in the event; a flag
    # does not stop it publishing as ``published``.
    source = tmp_path / "mail.eml"
    source.write_bytes(
        b"From: a@example.com\r\nTo: b@example.com\r\nDate: Tue, 14 Oct 2026 09:00:00 +1100\r\n"
        b"Message-ID: <1@example.com>\r\nSubject: hi\r\n\r\nbody\r\n"
    )
    _single_unit(monkeypatch, "eml", "# hi\n\nbody", {"flags": ["images_not_ocrd"], "email": EMAIL})
    mail_store = MemoryStore()
    result = parse_pdf_pages_in_store(
        source, "unused", store=mail_store, workers=0, publish=PublishRequest("mail")
    )
    assert (result["state"], result["kind"], result["flags"]) == (
        "complete",
        "eml",
        ["images_not_ocrd"],
    )
    published = result["published"]
    assert published["type"] == "published"
    document = _json(mail_store, f"published/v1/docs/mail/{published['version']}/document.json")
    assert (document["kind"], document["unit"], document["flags"], document["handler_version"]) == (
        "eml",
        "document",
        ["images_not_ocrd"],
        "1",
    )
    assert document["email"] == EMAIL
    [event] = _events(mail_store)
    assert (event["kind"], event["unit"], event["flags"]) == (
        "eml",
        "document",
        ["images_not_ocrd"],
    )
    assert "email" not in event
    raw_event = mail_store.read(published["event_key"]).decode()
    for marker in ("marker@example.com", "SUBJECT-MARKER", "ATTACHMENT-MARKER"):
        assert marker not in raw_event

    # publish_document refuses an email for any other kind.
    with pytest.raises(ValueError, match="email"):
        publish.publish_document(
            MemoryStore(),
            PublishRequest(DOC),
            state="complete",
            source_name="a.pdf",
            source_sha256="0" * 64,
            work_id="w",
            config={},
            config_hash="c",
            page_count=1,
            markdown="",
            pages_meta=[],
            page_texts={},
            kind="pdf",
            email=EMAIL,
        )


def test_single_unit_document_keeps_page_marker_and_pages_0001(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_page_pipeline import DOCX

    _single_unit(monkeypatch, "docx", "# Title\n\nBody text.", {})

    result = parse_pdf_pages_in_store(
        DOCX, "unused", store=store, workers=0, publish=PublishRequest("memo")
    )

    assert (result["state"], result["unit"], result["page_count"]) == ("complete", "document", 1)
    prefix = f"published/v1/docs/memo/{result['published']['version']}"
    markdown = store.read(f"{prefix}/document.md").decode()
    assert markdown == "==== page 1 ===\n\n# Title\n\nBody text.\n"
    assert store.read(f"{prefix}/pages/0001.md") == b"# Title\n\nBody text.\n"
    document = _json(store, f"{prefix}/document.json")
    assert (document["kind"], document["unit"], document["flags"]) == ("docx", "document", [])
    assert document["page_marker"] == "==== page {number} ==="
    [page] = document["pages"]
    assert (page["page"], page["file"], page["char_span"]) == (
        1,
        "pages/0001.md",
        [0, len(markdown) - 1],
    )
    assert "email" not in document  # not an email kind

    file_id = result["file_id"]
    output = _json(store, f"out/{file_id}/output.json")
    assert (output["kind"], output["unit"], output["flags"]) == ("docx", "document", [])
    status = _json(store, f"out/{file_id}/status.json")
    assert (status["kind"], status["unit"], status["detected_type"], status["flags"]) == (
        "docx",
        "document",
        "docx",
        [],
    )
    record = _json(store, f"work/{result['work_id']}/results/0001.json")
    assert (record["kind"], record["unit"], record["flags"], record["handler_version"]) == (
        "docx",
        "document",
        [],
        "1",
    )
