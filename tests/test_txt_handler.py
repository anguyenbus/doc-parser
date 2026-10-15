"""
test_txt_handler.py — the TXT passthrough handler and the output cap (spec D6, D5, sub-step 4).

Fixtures are bytes built in the test. The handler runs through ``parse_unit`` (as
the parse worker does), through the local executor in this process, and once in a
real spawned parse worker for the output cap. No Docling is loaded on the TXT path.

Run ONLY these tests:
    uv run pytest tests/test_txt_handler.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_parse_pages_script import _main, script  # noqa: F401  (fixture)

from parser_service.delivery.jobs import job_status_key
from parser_service.formats.handlers import parse_unit
from parser_service.formats.result import Reply
from parser_service.pipeline import api, config, intake, layout, remote_runner, units
from parser_service.service.worker import ParseWorker, parse_request
from parser_service.storage.store import LocalStore


@pytest.fixture
def txt_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """txt enabled explicitly, whatever the environment holds."""
    monkeypatch.setenv("PARSER_ENABLED_KINDS", "pdf,txt")


def _parse(tmp_path: Path, data: bytes, name: str = "notes.txt") -> Reply:
    path = tmp_path / name
    path.write_bytes(data)
    return parse_unit("txt", path, 1, False)


@pytest.mark.parametrize(
    ("data", "encoding"),
    [
        pytest.param("café costs 5 €\nline 2\n".encode(), "utf-8", id="utf8"),
        pytest.param(b"\xef\xbb\xbf" + "naïve “quote”\n".encode(), "utf-8-sig", id="utf8_bom"),
        pytest.param("café costs 5 € – “ok”\n".encode("cp1252"), "cp1252", id="cp1252"),
        pytest.param(b"one\r\ntwo\rthree\r\n\r\nend", "utf-8", id="crlf"),
    ],
)
def test_txt_round_trip(tmp_path: Path, data: bytes, encoding: str) -> None:
    """The decoded text, re-encoded with the detected codec, is the input with LF line ends."""
    state, reason, reason_code, markdown, extra = _parse(tmp_path, data)
    assert (state, reason, reason_code) == ("ok", None, None)
    assert extra == {"route": "txt_passthrough", "encoding": encoding}
    assert "\r" not in markdown and "﻿" not in markdown
    assert markdown.encode(encoding) == data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def test_short_cp1252_is_not_misread_as_cp775(tmp_path: Path) -> None:
    """Short cp1252 text is decoded by the fixed cp1252 step, never by a guess."""
    data = "Café – “Zoë”".encode("cp1252")
    _state, _reason, _code, markdown, extra = _parse(tmp_path, data)
    assert (markdown, extra["encoding"]) == ("Café – “Zoë”", "cp1252")
    # Bytes that are not cp1252 (0x81 is undefined there) fall through to
    # charset-normalizer, and to latin-1 at worst: the handler never fails on them.
    state, _reason, _code, _markdown, extra = _parse(tmp_path, b"abc \x81\x8d def\n")
    assert state == "ok" and extra["encoding"] not in ("utf-8", "cp1252")


def test_nul_heavy_input_is_rejected(tmp_path: Path) -> None:
    """Over 1% NUL is binary (UTF-16 too): failed, unsupported_type, empty limits, final."""
    text = b"plain text " * 90  # 990 bytes
    for data in (text + b"\x00" * 11, "plain text\n".encode("utf-16-le")):
        state, reason, reason_code, markdown, extra = _parse(tmp_path, data)
        assert (state, reason_code, markdown) == ("failed", "unsupported_type", "")
        assert reason is not None and reason.startswith("unsupported_type: binary input")
        assert extra["limits"] == {}
    # A NUL share at or under 1% is text: the NULs are stripped.
    state, _reason, _code, markdown, _extra = _parse(tmp_path, text[:-1] + b"\x00" * 9 + b"\n")
    assert state == "ok" and "\x00" not in markdown

    # Through the local executor: a final record (empty limits match any limits now).
    store = LocalStore(tmp_path / "store")
    source = tmp_path / "in" / "nul.txt"
    source.parent.mkdir()
    source.write_bytes(text + b"\x00" * 11)
    work_id = intake.work_id_for(source, "txt")
    intake._intake(store, work_id, source, "txt")
    record = units._process_page(store, work_id, 1, False, 3, kind="txt")
    assert (record["state"], record["reason_code"], record["limits"]) == (
        "failed",
        "unsupported_type",
        {},
    )
    assert config._is_final(record, config.current_limits("txt"))


def test_binary_input_is_rejected(tmp_path: Path) -> None:
    """Other C0 control bytes over 5% (no NUL) are binary; tab, FF, VT and ESC are not."""
    text = b"x" * 940
    state, _reason, reason_code, _markdown, extra = _parse(tmp_path, text + b"\x01\x02\x03" * 20)
    assert (state, reason_code, extra["limits"]) == ("failed", "unsupported_type", {})
    state, _reason, _code, _markdown, _extra = _parse(tmp_path, text + b"\t\x0b\x0c\x1b" * 20)
    assert state == "ok"


def test_empty_file_is_ok_with_empty_document_route(tmp_path: Path, txt_enabled: None) -> None:
    """An empty (or blank) TXT is ok with route empty_document, not failed empty_output."""
    for data in (b"", b" \r\n\t\n"):
        state, _reason, reason_code, _markdown, extra = _parse(tmp_path, data)
        assert (state, reason_code, extra["route"]) == ("ok", None, "empty_document")

    source = tmp_path / "in" / "empty.txt"
    source.parent.mkdir()
    source.write_bytes(b"")
    result = api.parse_pdf_pages(source, tmp_path / "store", workers=0, escalate=False)
    assert (result["state"], result["kind"]) == ("complete", "txt")
    status = json.loads(Path(result["status_path"]).read_text())
    assert status["unit"] == "document"
    [unit] = status["pages"]
    assert (unit["state"], unit["route"]) == ("ok", "empty_document")


def test_oversized_txt_fails_alone_with_too_large(
    script: object,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A TXT over PARSER_MAX_TXT_MB fails at intake with no work; the job's other file parses."""
    monkeypatch.setenv("PARSER_ENABLED_KINDS", "pdf,txt")  # after the script fixture
    monkeypatch.setenv("PARSER_MAX_TXT_MB", "1")
    inbox = tmp_path / "inbox" / "sys" / "audit-1"
    inbox.mkdir(parents=True)
    (inbox / "big.txt").write_bytes(b"plain text line\n" * 70_000)  # 1.12 MB
    (inbox / "small.txt").write_bytes(b"short memo\n")
    docs = [{"doc_id": "big", "file": "big.txt"}, {"doc_id": "small", "file": "small.txt"}]
    (inbox / "job.json").write_text(json.dumps({"job_id": "audit-1", "docs": docs}))
    location = tmp_path / "store"
    args = ("--job", str(inbox / "job.json"), "--work-dir", str(location), "--workers", "0")

    assert _main(script, monkeypatch, *args, "--no-escalate", "--index", "0") == 2
    assert _main(script, monkeypatch, *args, "--no-escalate", "--index", "1") == 0

    status = json.loads((location / job_status_key("audit-1")).read_text())
    assert (status["documents_total"], status["documents_ok"]) == (2, 1)
    big, small = status["docs"]
    assert (big["kind"], big["work_id"]) == ("txt", None)
    [unit] = big["not_ok_pages"]
    assert unit["reason_code"] == "too_large"
    assert small["state"] == "complete"
    assert [p.name for p in (location / "work").rglob("source.*")] == ["source.txt"]


def test_output_over_cap_fails_with_output_too_large(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, txt_enabled: None
) -> None:
    """Markdown over PARSER_MAX_OUTPUT_MB: a final output_too_large record, and the text
    never crosses the worker's pipe."""
    monkeypatch.setenv("PARSER_MAX_OUTPUT_MB", "1")
    store = LocalStore(tmp_path / "store")
    source = tmp_path / "in" / "long.txt"
    source.parent.mkdir()
    source.write_bytes(b"0123456789abcdef\n" * 70_000)  # 1.19 MB of text
    work_id = intake.work_id_for(source, "txt")
    intake._intake(store, work_id, source, "txt")

    worker = ParseWorker(parse_request, None, start_timeout_s=60)
    try:
        worker.start()
        # The worker's own reply: no text in it.
        reply = worker.run(("txt", str(source), 1, False, 1_000_000), 30)
        assert reply[:4] == (
            "failed",
            f"output_too_large: {source.stat().st_size} bytes of markdown, over the cap of 1000000",
            "output_too_large",
            "",
        )
        # The page service path: the cap comes from PARSER_MAX_OUTPUT_MB.
        remote_runner.process_page_request(store, work_id, 1, False, 3, runner=worker)
    finally:
        worker.kill()
    record = layout._load_record(store, work_id, 1)
    assert record is not None
    assert (record["state"], record["reason_code"], record["markdown"]) == (
        "failed",
        "output_too_large",
        "",
    )
    assert record["limits"] == {"max_output_mb": 1}
    assert config._is_final(record, config.current_limits("txt"))
    # Raising the cap releases the record (D5).
    monkeypatch.setenv("PARSER_MAX_OUTPUT_MB", "2")
    assert not config._is_final(record, config.current_limits("txt"))
    # PDF pages still get no cap.
    assert config.output_cap_bytes("pdf") is None


def test_txt_never_calls_docling(tmp_path: Path) -> None:
    """A fresh interpreter parses a TXT unit as the worker does, and never imports Docling."""
    path = tmp_path / "notes.txt"
    path.write_bytes("café\r\nline 2\n".encode("cp1252"))
    code = (
        "import sys\n"
        "from parser_service.formats.result import apply_output_cap\n"
        "from parser_service.service.worker import parse_request\n"
        f"reply = apply_output_cap('txt', parse_request('txt', {str(path)!r}, 1, False), 10**6)\n"
        "assert reply[0] == 'ok', reply\n"
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in ('docling', 'torch'))\n"
        "print(','.join(heavy) or 'none')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip() == "none"
