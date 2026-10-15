"""
test_sniff.py — ``parser_service.formats.sniff``: the D1 table, the text rule and the
``.pdf`` exception.

Every fixture is bytes built here: ZIPs with ``zipfile``, an OLE2 header plus
directory entries, ``email.message`` output, magic numbers. No file I/O.

Run ONLY these tests:
    uv run pytest tests/test_sniff.py
"""

from __future__ import annotations

import io
import struct
import zipfile
from email.message import EmailMessage

import docx
import pytest

from parser_service.formats.sniff import SNIFF_BYTES, Sniff, sniff

_CONTENT_TYPES = (
    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
    'content-types"><Override PartName="/{part}" ContentType="application/vnd.openxml'
    'formats-officedocument.{main}+xml"/></Types>'
)


def _zip(parts: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _ooxml(part: str, main: str) -> bytes:
    content_types = _CONTENT_TYPES.format(part=part, main=main).encode()
    return _zip({"[Content_Types].xml": content_types, part: b"<x/>"})


DOCX = _ooxml("word/document.xml", "wordprocessingml.document.main")
XLSX = _ooxml("xl/workbook.xml", "spreadsheetml.sheet.main")
PPTX = _ooxml("ppt/presentation.xml", "presentationml.presentation.main")
ZIP = _zip({"readme.txt": b"hello", "data/a.csv": b"a,b\n"})


def _python_docx() -> bytes:
    document = docx.Document()
    document.add_paragraph("A paragraph.")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _large_docx_head() -> bytes:
    """The first 64 KiB of a DOCX whose word/document.xml starts beyond them: only the
    local header of [Content_Types].xml (and its deflated body) is in the head."""
    buffer = io.BytesIO()
    content_types = _CONTENT_TYPES.format(
        part="word/document.xml", main="wordprocessingml.document.main"
    )
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/media/image1.bin", bytes(range(256)) * 400, zipfile.ZIP_STORED)
        archive.writestr("word/document.xml", b"<w:document/>")
    data = buffer.getvalue()
    assert len(data) > SNIFF_BYTES
    return data[:SNIFF_BYTES]


def _ole2(*names: str) -> bytes:
    """An OLE2 header sector, then one directory sector: Root Entry plus ``names``."""
    header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504)
    entries = b""
    for index, name in enumerate(("Root Entry", *names)):
        raw = (name + "\x00").encode("utf-16-le")
        entry = raw.ljust(64, b"\x00") + struct.pack("<HB", len(raw), 5 if index == 0 else 2)
        entries += entry.ljust(128, b"\x00")
    return header + entries.ljust(512, b"\x00")


def _eml() -> bytes:
    message = EmailMessage()
    message["From"] = "Alice <alice@example.com>"
    message["To"] = "bob@example.com"
    message["Subject"] = "Quarterly figures"
    message["Date"] = "Tue, 14 Oct 2026 09:00:00 +1100"
    message["Message-ID"] = "<1@example.com>"
    message.set_content("Hello Bob,\n\nThe figures are attached.\n")
    return message.as_bytes()


EML = _eml()
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n"
HTML = b"<!DOCTYPE html>\n<html><head><title>t</title></head><body><p>Hi</p></body></html>\n"
TEXT = b"Meeting notes\n\n- item one\n- item two\n"


@pytest.mark.parametrize(
    ("head", "name", "expected"),
    [
        # pdf: header anywhere in the first 1024 bytes
        (PDF, "a.pdf", Sniff("pdf", "pdf", False)),
        (b"\n" * 900 + PDF, "a.pdf", Sniff("pdf", "pdf", False)),
        # docx, xlsx, pptx, zip
        (DOCX, "a.docx", Sniff("docx", "docx", False)),
        (_python_docx(), "a.docx", Sniff("docx", "docx", False)),
        (_large_docx_head(), "big.docx", Sniff("docx", "docx", False)),
        (XLSX, "a.xlsx", Sniff("xlsx", None, False)),
        (PPTX, "a.pptx", Sniff("pptx", None, False)),
        (ZIP, "a.zip", Sniff("zip", None, False)),
        # msg, doc, xls, ppt, ole2
        (
            _ole2("__substg1.0_0037001F", "__properties_version1.0"),
            "a.msg",
            Sniff("msg", "msg", False),
        ),
        (_ole2("WordDocument", "1Table"), "a.doc", Sniff("doc", None, False)),
        (_ole2("Workbook"), "a.xls", Sniff("xls", None, False)),
        (_ole2("Book"), "a.xls", Sniff("xls", None, False)),
        (_ole2("PowerPoint Document"), "a.ppt", Sniff("ppt", None, False)),
        (_ole2("Contents"), "a.bin", Sniff("ole2", None, False)),
        # png, jpeg: the content decides; a disagreeing name only sets mismatch
        (b"\x89PNG\r\n\x1a\n" + bytes(32), "a.png", Sniff("png", "png", False)),
        (b"\x89PNG\r\n\x1a\n" + bytes(32), "scan", Sniff("png", "png", False)),
        (b"\x89PNG\r\n\x1a\n" + bytes(32), "a.jpg", Sniff("png", "png", True)),
        (b"\xff\xd8\xff\xe0" + bytes(32), "a.jpg", Sniff("jpeg", "jpeg", False)),
        (b"\xff\xd8\xff\xe1" + bytes(32), "a.JPEG", Sniff("jpeg", "jpeg", False)),
        (b"\xff\xd8\xff\xe0" + bytes(32), "a.png", Sniff("jpeg", "jpeg", True)),
        # rtf, image (formats no kind handles)
        (b"{\\rtf1\\ansi Hello}", "a.rtf", Sniff("rtf", None, False)),
        (b"GIF89a" + bytes(32), "a.gif", Sniff("image", None, False)),
        (b"II*\x00" + bytes(32), "a.tif", Sniff("image", None, False)),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 " + bytes(32), "a.webp", Sniff("image", None, False)),
        # eml, html, text
        (EML, "a.eml", Sniff("eml", "eml", False)),
        (HTML, "a.html", Sniff("html", "html", False)),
        (TEXT, "a.txt", Sniff("text", "txt", False)),
        # empty
        (b"", "a.txt", Sniff("empty", "txt", False)),
        (b"", "a.bin", Sniff("empty", None, False)),
        # binary
        (bytes(range(256)) * 4, "a.bin", Sniff("binary", None, False)),
        (b"\x01\x02\x03\x04abc" * 100, "a.dat", Sniff("binary", None, False)),
        # renamed files: binary kinds trust the content, the mismatch is flagged
        (DOCX, "report.pdf", Sniff("docx", "docx", True)),
        (PDF, "report.docx", Sniff("pdf", "pdf", True)),
        (EML, "memo.txt", Sniff("eml", "eml", True)),
    ],
)
def test_sniff_table(head: bytes, name: str, expected: Sniff) -> None:
    assert sniff(head, name) == expected


@pytest.mark.parametrize(
    ("head", "name", "declared", "expected"),
    [
        (TEXT, "a.txt", None, Sniff("text", "txt", False)),
        (TEXT, "noext", "text/plain", Sniff("text", "txt", False)),
        (TEXT, "noext", "text/plain; charset=utf-8", Sniff("text", "txt", False)),
        (HTML, "a.html", None, Sniff("html", "html", False)),
        (HTML, "a.HTM", None, Sniff("html", "html", False)),
        (HTML, "noext", "text/html", Sniff("html", "html", False)),
        # the name wins over the content, and the disagreement is flagged
        (HTML, "page.txt", None, Sniff("html", "txt", True)),
        (TEXT, "page.html", None, Sniff("text", "html", True)),
        # the name wins over the declared type
        (TEXT, "a.txt", "text/html", Sniff("text", "txt", False)),
        # octet-stream and empty declared types are ignored
        (TEXT, "noext", "application/octet-stream", Sniff("text", None, False)),
        (TEXT, "noext", "", Sniff("text", None, False)),
        (HTML, "noext", "application/octet-stream", Sniff("html", None, False)),
    ],
)
def test_text_rule_name_or_declared_type_picks_the_kind(
    head: bytes, name: str, declared: str | None, expected: Sniff
) -> None:
    assert sniff(head, name, declared) == expected


@pytest.mark.parametrize(
    ("head", "name", "declared"),
    [
        # another extension gives no kind, even when text/plain is declared
        (b"a,b,c\n1,2,3\n", "data.csv", "text/plain"),
        (b'{"a": 1}\n', "data.json", "text/plain"),
        (b'<?xml version="1.0"?><root/>\n', "data.xml", None),
        (b"# Title\n\nSome text.\n", "notes.md", "text/plain"),
        (b"2026-10-14 INFO started\n", "app.log", None),
        # no extension and no declared type
        (TEXT, "README", None),
    ],
)
def test_text_like_files_with_other_extensions_have_no_kind(
    head: bytes, name: str, declared: str | None
) -> None:
    result = sniff(head, name, declared)
    assert result.detected_type == "text"
    assert result.kind is None
    assert result.mismatch is False


@pytest.mark.parametrize(
    ("head", "detected"),
    [(b"not really a pdf", "text"), (bytes(range(256)), "binary"), (XLSX, "xlsx")],
)
def test_pdf_name_without_header_still_routes_to_pdf(head: bytes, detected: str) -> None:
    result = sniff(head, "scan.PDF")
    assert result == Sniff(detected, "pdf", True)
    # An empty .pdf also reaches pypdf, as before.
    assert sniff(b"", "empty.pdf").kind == "pdf"
