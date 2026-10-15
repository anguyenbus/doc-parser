"""
test_email_handler.py — the EML and MSG handler (spec D6 Email, D8 ``email``, sub-step 6).

EML fixtures are built at test time with stdlib ``email`` (or as raw bytes where
the test is about the wire format). The one committed fixture is
``tests/fixtures/email_plain_and_html.msg``, a small synthetic Outlook message
with a plain and an HTML body and one attachment (origin:
``tests/fixtures/README.md``). The handler runs through ``parse_unit`` (as the
parse worker does), through a real spawned parse worker, and through the local
executor with publish.

Run ONLY these tests:
    uv run pytest tests/test_email_handler.py
"""

from __future__ import annotations

import base64
import io
import json
import logging
import quopri
import shutil
import socket
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from test_page_pipeline_store import MemoryStore

from parser_service.delivery.publish import PublishRequest
from parser_service.formats.handlers import parse_unit
from parser_service.parsing import document as markdown_pipeline
from parser_service.pipeline import api, intake, layout, remote_runner
from parser_service.service.worker import ParseWorker, parse_request
from parser_service.storage.store import LocalStore

MSG_FIXTURE = Path(__file__).parent / "fixtures" / "email_plain_and_html.msg"

# Headers every built EML starts with (enough for the sniffer to route it to eml).
_HEADERS = (
    b"From: Ann Sender <ann@example.invalid>\r\n"
    b"To: Bob Reader <bob@example.invalid>\r\n"
    b"Date: Tue, 14 Oct 2026 09:00:00 +1100\r\n"
    b"Message-ID: <fixture-1@example.invalid>\r\n"
    b"Subject: Quarterly update\r\n"
    b"MIME-Version: 1.0\r\n"
)


@pytest.fixture
def email_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """eml and msg enabled explicitly, whatever the environment holds."""
    monkeypatch.setenv("PARSER_ENABLED_KINDS", "pdf,eml,msg")


def _message(subject: str = "Quarterly update") -> EmailMessage:
    message = EmailMessage()
    message["From"] = "Ann Sender <ann@example.invalid>"
    message["To"] = "Bob Reader <bob@example.invalid>, carol@example.invalid"
    message["Cc"] = "Dan Copy <dan@example.invalid>"
    message["Date"] = "Tue, 14 Oct 2026 09:00:00 +1100"
    message["Message-ID"] = "<fixture-1@example.invalid>"
    message["Subject"] = subject
    return message


def _write(tmp_path: Path, data: bytes, name: str = "source.eml") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "teal").save(buf, "PNG")
    return buf.getvalue()


def _record_html_conversions(monkeypatch: pytest.MonkeyPatch) -> list[tuple[bytes, Any]]:
    """Record the bytes and effective options of every HTML backend Docling builds."""
    from docling.backend.html_backend import HTMLDocumentBackend

    seen: list[tuple[bytes, Any]] = []
    original = HTMLDocumentBackend.__init__

    def init(self: Any, in_doc: Any, path_or_stream: Any, options: Any = None) -> None:
        original(self, in_doc, path_or_stream, options)
        data = path_or_stream.getvalue() if hasattr(path_or_stream, "getvalue") else b""
        seen.append((data, self.options))

    monkeypatch.setattr(HTMLDocumentBackend, "__init__", init)
    return seen


# ---------------------------------------------------------------------------
# Bodies, headers and charsets
# ---------------------------------------------------------------------------


def test_multipart_alternative_uses_the_plain_part(tmp_path: Path) -> None:
    """Docling's output: subject as title, From/To/Cc/Date lines, the text/plain body.
    The HTML alternative is used only when there is no text/plain part."""
    message = _message()
    message.set_content("PLAIN-PART-MARKER first paragraph.\n\nSecond paragraph.\n")
    message.add_alternative(
        "<p>HTML-PART-MARKER</p><table><tr><td>a</td><td>b</td></tr></table>", subtype="html"
    )
    state, reason, reason_code, markdown, extra = parse_unit(
        "eml", _write(tmp_path, message.as_bytes()), 1, False
    )

    assert (state, reason, reason_code) == ("ok", None, None)
    assert markdown == (
        "# Quarterly update\n\n"
        "From: Ann Sender &lt;ann@example.invalid&gt;\n\n"
        "To: Bob Reader &lt;bob@example.invalid&gt;, carol@example.invalid\n\n"
        "Cc: Dan Copy &lt;dan@example.invalid&gt;\n\n"
        "Date: 2026-10-13T22:00:00+00:00\n\n"
        "PLAIN-PART-MARKER first paragraph.\n\n"
        "Second paragraph."
    )
    assert (extra["route"], extra["flags"]) == ("docling-kept", [])
    assert extra["email"] == {
        "headers": {
            "from": "Ann Sender <ann@example.invalid>",
            "to": "Bob Reader <bob@example.invalid>, carol@example.invalid",
            "cc": "Dan Copy <dan@example.invalid>",
            "date": "2026-10-13T22:00:00+00:00",
            "subject": "Quarterly update",
        },
        "attachments": [],
    }

    html_only = _message()
    html_only.set_content("<p>HTML-ONLY-MARKER body</p>", subtype="html")
    _s, _r, _c, markdown, _e = parse_unit(
        "eml", _write(tmp_path, html_only.as_bytes(), "html_only.eml"), 1, False
    )
    assert "HTML-ONLY-MARKER body" in markdown


def test_rfc2047_headers_are_decoded(tmp_path: Path) -> None:
    """Encoded-words (B and Q, several charsets) and an RFC 2231 file name are decoded,
    in the markdown and in ``extra.email``."""
    subject = "Über das Ende ✓"
    encoded = base64.b64encode(subject.encode()).decode()
    data = (
        b"From: =?utf-8?q?J=C3=BCrgen_M=C3=BCller?= <jurgen@example.invalid>\r\n"
        b"To: =?iso-8859-1?q?Fran=E7ois?= <francois@example.invalid>, plain@example.invalid\r\n"
        b"Cc: =?utf-8?b?5bGx55Sw?= <yamada@example.invalid>\r\n"
        + f"Subject: =?utf-8?b?{encoded}?= and =?iso-8859-1?q?caf=E9?=\r\n".encode()
        + b"Date: Tue, 14 Oct 2026 09:00:00 +1100\r\n"
        b"Message-ID: <fixture-2@example.invalid>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B1"\r\n\r\n'
        b"--B1\r\nContent-Type: text/plain; charset=us-ascii\r\n\r\nbody\r\n"
        b"--B1\r\nContent-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename*=utf-8''r%C3%A9sum%C3%A9.pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n"
        b"--B1--\r\n"
    )
    _state, _reason, _code, markdown, extra = parse_unit("eml", _write(tmp_path, data), 1, False)

    headers = extra["email"]["headers"]
    assert headers["subject"] == f"{subject} and café"
    assert headers["from"] == "Jürgen Müller <jurgen@example.invalid>"
    assert headers["to"] == "François <francois@example.invalid>, plain@example.invalid"
    assert headers["cc"] == "山田 <yamada@example.invalid>"
    assert extra["email"]["attachments"] == [
        {"name": "résumé.pdf", "type": "application/pdf", "size": 9}
    ]
    assert markdown.startswith(f"# {subject} and café\n\nFrom: Jürgen Müller &lt;")
    assert "To: François &lt;francois@example.invalid&gt;" in markdown
    assert "Cc: 山田 &lt;yamada@example.invalid&gt;" in markdown
    assert "- résumé.pdf (application/pdf)" in markdown
    assert "=?" not in markdown


_TEXT_LATIN = "Grüße aus Köln, café crème"
_TEXT_CP1252 = "Smart “quotes” – €5 naïve"
_TEXT_UTF8 = "Ünïcödé ✓ 日本語 – €"


@pytest.mark.parametrize(
    ("charset", "transfer", "text"),
    [
        pytest.param("iso-8859-1", "8bit", _TEXT_LATIN, id="iso-8859-1"),
        pytest.param("windows-1252", "quoted-printable", _TEXT_CP1252, id="windows-1252"),
        pytest.param("utf-8", "quoted-printable", _TEXT_UTF8, id="utf-8-qp"),
        pytest.param("utf-8", "base64", _TEXT_UTF8, id="utf-8-base64"),
    ],
)
def test_charsets(tmp_path: Path, charset: str, transfer: str, text: str) -> None:
    """A body in each charset and transfer encoding is decoded to the same text."""
    raw = f"{text}\n".encode(charset)
    body = {
        "8bit": raw,
        "quoted-printable": quopri.encodestring(raw),
        "base64": base64.encodebytes(raw),
    }[transfer]
    data = (
        _HEADERS
        + f"Content-Type: text/plain; charset={charset}\r\n".encode()
        + f"Content-Transfer-Encoding: {transfer}\r\n\r\n".encode()
        + body
    )
    state, _reason, _code, markdown, _extra = parse_unit("eml", _write(tmp_path, data), 1, False)
    assert state == "ok"
    assert markdown.endswith(f"\n\n{text}")


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------

_MALFORMED: dict[str, bytes] = {
    # multipart with no boundary parameter
    "no_boundary": _HEADERS + b"Content-Type: multipart/mixed\r\n\r\nloose body text\r\n",
    # no closing boundary, and an attachment whose base64 is broken
    "unterminated": _HEADERS
    + (
        b'Content-Type: multipart/mixed; boundary="XX"\r\n\r\n'
        b"--XX\r\nContent-Type: text/plain\r\n\r\nfirst part\r\n"
        b"--XX\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="a.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0x!!!not base64###\r\n"
    ),
    # a base64 body that is not base64, and an unknown charset
    "bad_base64": _HEADERS
    + (
        b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        b"@@@not*base64@@@\r\n"
    ),
    "unknown_charset": _HEADERS
    + b"Content-Type: text/plain; charset=x-no-such-charset\r\n\r\nplain \xff\xfe text\r\n",
    # no header block at all, and an empty file
    "no_headers": b"this is not a header\r\n\x00\x01\x02: :\r\n\r\nbody\r\n",
    "empty": b"",
}


def test_malformed_mime_gives_a_record_not_a_dead_worker(
    tmp_path: Path, email_enabled: None
) -> None:
    """Malformed MIME is parsed leniently (``ok``); an unreadable MSG is ``failed``
    ``docling_failed``. Either way the worker replies and stays alive, and the page
    service writes a record."""
    corrupt_msg = _write(
        tmp_path, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00garbage" * 50, "corrupt.msg"
    )
    worker = ParseWorker(parse_request, None, start_timeout_s=60)
    try:
        pid = worker.start()
        for name, data in _MALFORMED.items():
            path = _write(tmp_path, data, f"{name}.eml")
            state, reason, _code, markdown, extra = worker.run(
                ("eml", str(path), 1, False, None), 60
            )
            assert (name, state, reason) == (name, "ok", None)
            assert extra["route"] == ("empty_document" if name == "empty" else "docling-kept")
            assert set(extra["email"]) == {"headers", "attachments"}
        _s, _r, _c, _m, extra = worker.run(
            ("eml", str(tmp_path / "unterminated.eml"), 1, False, None), 60
        )
        assert extra["email"]["attachments"] == [
            {"name": "a.pdf", "type": "application/pdf", "size": 24}
        ]
        state, reason, reason_code, markdown, _extra = worker.run(
            ("msg", str(corrupt_msg), 1, False, None), 60
        )
        assert (state, reason_code, markdown) == ("failed", "docling_failed", "")
        assert reason is not None and reason.startswith("docling_failed: ")
        assert worker.pid == pid and worker.ready  # the same worker, still serving

        # The page service path writes one record for a malformed EML.
        store = LocalStore(tmp_path / "store")
        source = tmp_path / "unterminated.eml"
        work_id = intake.work_id_for(source, "eml")
        intake._intake(store, work_id, source, "eml")
        remote_runner.process_page_request(store, work_id, 1, False, 3, runner=worker)
        assert worker.pid == pid
    finally:
        worker.kill()
    record = layout._load_record(store, work_id, 1)
    assert record is not None
    assert (record["state"], record["kind"], record["unit"]) == ("ok", "eml", "document")
    assert record["parse_worker_pid"] == pid
    assert record["email"]["attachments"][0]["name"] == "a.pdf"


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


def _with_attachments() -> tuple[EmailMessage, list[dict[str, Any]]]:
    """An email with five attachments (each holding a marker) and a large inline image."""
    message = _message("With attachments")
    message.set_content("Body text.\n")
    message.add_alternative('<p>Body <img src="cid:chart@example.invalid"></p>', subtype="html")
    list(message.iter_parts())[1].add_related(
        _png(300, 300), maintype="image", subtype="png", cid="<chart@example.invalid>"
    )
    forwarded = EmailMessage()
    forwarded["From"] = "eve@example.invalid"
    forwarded["Subject"] = "forwarded"
    forwarded.set_content("ATT-FORWARDED-MARKER body of the attached message\n")
    files = [
        ("report.pdf", "application", "pdf", b"%PDF-1.4\nATT-PDF-MARKER\n%%EOF\n"),
        ("page.html", "text", "html", b"<html><body><h1>ATT-HTML-MARKER</h1></body></html>"),
        ("notes.txt", "text", "plain", "ATT-TXT-MARKER café\n".encode()),
        ("blob.bin", "application", "octet-stream", b"\x00\x01ATT-BIN-MARKER\xff" * 10),
    ]
    for name, maintype, subtype, data in files:
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    message.add_attachment(forwarded, filename="forwarded.eml")
    expected = [
        {"name": "<chart@example.invalid>", "type": "image/png", "size": len(_png(300, 300))},
        *(
            {"name": name, "type": f"{maintype}/{subtype}", "size": len(data)}
            for name, maintype, subtype, data in files
        ),
        {"name": "forwarded.eml", "type": "message/rfc822", "size": len(forwarded.as_bytes())},
    ]
    return message, expected


def test_attachments_are_listed_and_not_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, email_enabled: None
) -> None:
    """Each attachment is one ``name (type)`` line; no attachment content reaches the
    markdown or a Docling conversion (an attached message included); document.json
    holds the decoded sizes. A large inline image sets ``images_not_ocrd``."""
    from docling.document_converter import DocumentConverter

    converted: list[str] = []
    original_convert = DocumentConverter.convert

    def convert(self: Any, source: Any, *args: Any, **kwargs: Any) -> Any:
        converted.append(Path(str(source)).name)
        return original_convert(self, source, *args, **kwargs)

    monkeypatch.setattr(DocumentConverter, "convert", convert)
    html_seen = _record_html_conversions(monkeypatch)
    message, expected = _with_attachments()
    source = _write(tmp_path, message.as_bytes(), "mail.eml")

    store = MemoryStore()
    result = api.parse_pdf_pages_in_store(
        source, "unused", store=store, workers=0, publish=PublishRequest("mail")
    )

    assert (result["state"], result["kind"], result["flags"]) == (
        "complete",
        "eml",
        ["images_not_ocrd"],
    )
    prefix = f"published/v1/docs/mail/{result['published']['version']}"
    document_md = store.read(f"{prefix}/document.md").decode()
    for line in (
        "- &lt;chart@example.invalid&gt; (image/png)",
        "- report.pdf (application/pdf)",
        "- page.html (text/html)",
        "- notes.txt (text/plain)",
        "- blob.bin (application/octet-stream)",
        "- forwarded.eml (message/rfc822)",
    ):
        assert line in document_md
    assert "ATT-" not in document_md
    assert converted == ["source.eml"]  # one conversion, of the email itself
    assert all(b"ATT-" not in data for data, _options in html_seen)
    document = json.loads(store.read(f"{prefix}/document.json"))
    assert document["email"]["attachments"] == expected

    # A small inline image does not set the flag.
    small = _message()
    small.set_content("Body.\n")
    small.add_attachment(_png(10, 10), maintype="image", subtype="png", disposition="inline")
    _s, _r, _c, _m, extra = parse_unit("eml", _write(tmp_path, small.as_bytes()), 1, False)
    assert extra["flags"] == []


def test_msg_with_plain_and_html_bodies(tmp_path: Path) -> None:
    """The committed synthetic MSG: python-oxmsg reads both bodies; the handler uses the
    plain one, lists the attachment and never converts it."""
    from oxmsg import Message

    message = Message.load(str(MSG_FIXTURE))
    assert message.body is not None and "PLAIN-BODY-MARKER" in message.body
    assert message.html_body is not None and "HTML-ONLY-MARKER" in message.html_body
    [attachment] = message.attachments
    assert (attachment.file_name, attachment.mime_type, attachment.size) == (
        "notes.txt",
        "text/plain",
        47,
    )

    source = tmp_path / "source.msg"
    shutil.copyfile(MSG_FIXTURE, source)
    state, reason, reason_code, markdown, extra = parse_unit("msg", source, 1, False)

    assert (state, reason, reason_code) == ("ok", None, None)
    assert markdown == (
        "# Synthetic fixture: plain and HTML bodies\n\n"
        "From: Fixture Sender &lt;sender@example.invalid&gt;\n\n"
        "To: Fixture To &lt;to@example.invalid&gt;\n\n"
        "Cc: Fixture Cc &lt;cc@example.invalid&gt;\n\n"
        "Date: 2026-10-14T09:30:00+00:00\n\n"
        "SYNTHETIC test message.\n\n"
        "PLAIN-BODY-MARKER: the plain text part.\n\n"
        "### Attachments\n\n"
        "- notes.txt (text/plain)"
    )
    assert extra["email"] == {
        "headers": {
            "from": "Fixture Sender <sender@example.invalid>",
            "to": "Fixture To <to@example.invalid>",
            "cc": "Fixture Cc <cc@example.invalid>",
            "date": "2026-10-14T09:30:00+00:00",
            "subject": "Synthetic fixture: plain and HTML bodies",
        },
        "attachments": [{"name": "notes.txt", "type": "text/plain", "size": 47}],
    }
    assert (extra["route"], extra["flags"]) == ("docling-kept", [])


# ---------------------------------------------------------------------------
# No network, and no headers outside the record and document.json
# ---------------------------------------------------------------------------


def test_email_conversion_makes_no_network_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An HTML-only email with remote ``img``, ``link``, ``script``, ``iframe`` and ``cid:``
    sources, and the MSG, convert with every connect and name lookup refused."""
    attempts: list[str] = []

    def _refuse(name: str) -> Callable[..., Any]:
        def _raise(*args: Any, **kwargs: Any) -> Any:
            attempts.append(name)
            raise OSError(f"network disabled in this test ({name})")

        return _raise

    monkeypatch.setattr(socket.socket, "connect", _refuse("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse("connect_ex"))
    monkeypatch.setattr(socket, "create_connection", _refuse("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", _refuse("getaddrinfo"))
    monkeypatch.setattr(socket, "gethostbyname", _refuse("gethostbyname"))
    monkeypatch.setattr(markdown_pipeline, "_kind_converters", {})  # fresh email converters
    html_seen = _record_html_conversions(monkeypatch)

    message = _message("Remote resources")
    message.set_content(
        '<html><head><link rel="stylesheet" href="http://example.invalid/site.css">'
        '<script src="https://example.invalid/app.js"></script></head><body>'
        "<h1>Notice</h1><p>HTML body text.</p>"
        '<img src="http://example.invalid/remote.png"><img src="//example.invalid/r.png">'
        '<img src="cid:part1@example.invalid"><img src="file:///etc/hosts">'
        '<iframe src="https://example.invalid/frame"></iframe></body></html>',
        subtype="html",
    )
    state, _reason, _code, markdown, extra = parse_unit(
        "eml", _write(tmp_path, message.as_bytes()), 1, False
    )
    assert state == "ok" and "HTML body text." in markdown
    assert extra["flags"] == []  # remote, cid: and local images do not count
    source = tmp_path / "source.msg"
    shutil.copyfile(MSG_FIXTURE, source)
    assert parse_unit("msg", source, 1, False)[0] == "ok"
    assert attempts == []

    # The email backend converted the HTML body (only the EML's: the MSG has a plain
    # one) with every fetch option off.
    assert html_seen and all(b"HTML body text." in data for data, _options in html_seen)
    for _data, options in html_seen:
        assert (options.enable_remote_fetch, options.enable_local_fetch, options.fetch_images) == (
            False,
            False,
            False,
        )
    # Both converters are email-only, with the attachment list on.
    for kind in ("eml", "msg"):
        converter = markdown_pipeline._document_converter(kind)
        [(input_format, option)] = converter.format_to_options.items()
        assert input_format.value == "email"
        assert option.backend_options.list_attachments is True


def test_headers_in_document_json_not_in_event(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, email_enabled: None
) -> None:
    """Headers and attachment names go to the record and document.json, never to an
    event or a log line (DEBUG included)."""
    caplog.set_level(logging.DEBUG)
    message = _message("SUBJECT-MARKER")
    message.replace_header("From", "Header Marker <marker@example.invalid>")
    message.set_content("Body.\n")
    message.add_attachment(
        b"%PDF-1.4\n", maintype="application", subtype="pdf", filename="ATTACHMENT-MARKER.pdf"
    )
    eml = _write(tmp_path, message.as_bytes(), "mail.eml")
    msg = tmp_path / "mail.msg"
    shutil.copyfile(MSG_FIXTURE, msg)
    secrets = {
        "mail": ["marker@example.invalid", "Header Marker", "SUBJECT-MARKER", "ATTACHMENT-MARKER"],
        "outlook": ["sender@example.invalid", "Fixture Sender", "Synthetic fixture", "notes.txt"],
    }

    for doc_id, source in (("mail", eml), ("outlook", msg)):
        store = MemoryStore()
        result = api.parse_pdf_pages_in_store(
            source, "unused", store=store, workers=0, publish=PublishRequest(doc_id)
        )
        published = result["published"]
        assert published["type"] == "published"
        document = store.read(
            f"published/v1/docs/{doc_id}/{published['version']}/document.json"
        ).decode()
        record = store.read(f"work/{result['work_id']}/results/0001.json").decode()
        event = store.read(published["event_key"]).decode()
        assert "email" in json.loads(document) and "email" in json.loads(record)
        assert "email" not in json.loads(event)
        for secret in secrets[doc_id]:
            assert secret in document and secret in record
            assert secret not in event

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "unit_done" in logged  # the structured events were captured
    for secret in secrets["mail"] + secrets["outlook"]:
        assert secret not in logged
