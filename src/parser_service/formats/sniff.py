"""
sniff.py — detect a file's type from its first bytes and route it to a kind.

One pure function, ``sniff(head, name, declared_type)``: no I/O, no Docling, no
boto3. ``head`` is the start of the file (``SNIFF_BYTES``, 64 KiB, is what callers
read; a longer or shorter buffer is fine, and a whole small file is best). The
result is the ``detected_type`` (what the bytes are), the routed ``kind`` (what the
page pipeline would parse it as, or None) and ``mismatch`` (the name or the declared
type disagrees with the content).

Rules, in order (spec D1; the first that matches wins):

    empty            zero bytes                      -> txt if the name or the
                                                        declared type says txt
    pdf              ``%PDF-`` in the first 1024 bytes -> pdf
    docx/xlsx/pptx/zip  ZIP local header; the part names (or [Content_Types].xml)
                     say Word, Excel, PowerPoint or none of them -> docx, xlsx, else none
    msg/doc/xls/ppt/ole2  OLE2 header; the directory entries say MSG (``__substg1.0_``
                     streams), Word, Excel, PowerPoint or none of them -> msg, else none
    png, jpeg        PNG or JPEG magic                -> png, jpeg
    rtf, image       ``{\\rtf``; GIF, TIFF or WebP magic -> none
    eml              text whose header block has >= 3 ``Name: value`` lines, From and
                     one of Date / Message-ID / Received / MIME-Version -> eml
    html             text with an HTML marker in the first 4 KiB -> the text rule
    text             no NUL, < 5% C0 control bytes -> the text rule
    binary           anything else                   -> none

Binary kinds (pdf, docx, xlsx, msg, eml, png, jpeg) trust the content; a disagreeing extension only
sets ``mismatch``. Text and html content take the kind from the extension or the
declared type (``.txt`` / ``text/plain`` -> txt, ``.html`` / ``.htm`` / ``text/html``
-> html); when that disagrees with the content, the name wins and ``mismatch`` is
set; any other name gives no kind. Last, a ``.pdf`` name whose content gave no kind
still routes to ``pdf`` (pypdf decides, as before).

Only the head is read, so a ZIP's central directory (at the end of the file) is
usually absent: the part names come from the local file headers in the head, the
central directory when present, and the ``[Content_Types].xml`` overrides when that
part is inflatable from the head. An OLE2 file's directory is read from the
128-byte directory entries that fall inside the head.
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath

SNIFF_BYTES = 64 * 1024

# Every kind the page pipeline can route to, and the fixed extension of its stored
# source file (spec D2: ``source.<ext>``).
KIND_EXTENSIONS: dict[str, str] = {
    "pdf": "pdf",
    "docx": "docx",
    "xlsx": "xlsx",
    "html": "html",
    "txt": "txt",
    "eml": "eml",
    "msg": "msg",
    "png": "png",
    "jpeg": "jpg",
}
KINDS: tuple[str, ...] = tuple(KIND_EXTENSIONS)

# The extensions that agree with a binary kind's content.
_BINARY_KIND_EXTENSIONS: dict[str, frozenset[str]] = {
    "pdf": frozenset({"pdf"}),
    "docx": frozenset({"docx"}),
    "xlsx": frozenset({"xlsx", "xlsm"}),
    "msg": frozenset({"msg"}),
    "eml": frozenset({"eml"}),
    "png": frozenset({"png"}),
    "jpeg": frozenset({"jpg", "jpeg", "jpe", "jfif"}),
}
_TXT_EXTENSIONS = frozenset({"txt"})
_HTML_EXTENSIONS = frozenset({"html", "htm"})
_IGNORED_DECLARED_TYPES = frozenset({"", "application/octet-stream"})

_PDF_MAGIC = b"%PDF-"
_ZIP_LOCAL = b"PK\x03\x04"
_ZIP_CENTRAL = b"PK\x01\x02"
_ZIP_EMPTY = b"PK\x05\x06"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
# Image formats no kind handles.
_IMAGE_MAGICS: tuple[bytes, ...] = (
    b"GIF87a",
    b"GIF89a",
    b"II*\x00",
    b"MM\x00*",
)
_HTML_MARKERS: tuple[bytes, ...] = (b"<!doctype html", b"<html", b"<head", b"<body")

# C0 control bytes that count against text: everything below 0x20 except tab, LF,
# VT, FF, CR and ESC.
_ALLOWED_C0 = frozenset({0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x1B})
_CONTROL_BYTES = bytes(b for b in range(0x20) if b not in _ALLOWED_C0)
_MAX_CONTROL_SHARE = 0.05

_HEADER_LINE = re.compile(rb"^([!-9;-~]+):[ \t]*(.*)$")
_EML_REQUIRED = b"from"
_EML_ANY_OF = frozenset({b"date", b"message-id", b"received", b"mime-version"})
_EML_MIN_HEADERS = 3


@dataclass(frozen=True)
class Sniff:
    """What a file's bytes are, and where the page pipeline would route it."""

    detected_type: str
    kind: str | None
    mismatch: bool


def extension_of(name: str) -> str:
    """The lower-case extension of ``name`` without the dot, or ``""`` for none."""
    return PurePosixPath(name.replace("\\", "/")).suffix.lower().lstrip(".")


def sniff(head: bytes, name: str, declared_type: str | None = None) -> Sniff:
    """Detect ``head``'s type and route it to a kind (see the module docstring).

    ``declared_type`` is the S3 ``Content-Type`` (parameters such as ``charset`` are
    ignored); ``application/octet-stream``, empty or None means "not declared".
    """
    ext = extension_of(name)
    declared = _media_type(declared_type)
    detected = detect_type(head)

    if detected in _BINARY_KIND_EXTENSIONS:
        kind: str | None = detected
        mismatch = bool(ext) and ext not in _BINARY_KIND_EXTENSIONS[detected]
    elif detected in ("text", "html"):
        kind = _text_kind(ext, declared)
        mismatch = kind is not None and kind != ("html" if detected == "html" else "txt")
    elif detected == "empty":
        kind = "txt" if _says_txt(ext, declared) else None
        mismatch = False
    else:
        kind, mismatch = None, False

    if kind is None and ext == "pdf":
        # A .pdf name always reaches pypdf, as before the sniffer existed.
        return Sniff(detected, "pdf", detected != "empty")
    return Sniff(detected, kind, mismatch)


def detect_type(head: bytes) -> str:
    """The D1 detected type of ``head``, ignoring the name."""
    if not head:
        return "empty"
    if _PDF_MAGIC in head[:1024]:
        return "pdf"
    if head.startswith((_ZIP_LOCAL, _ZIP_EMPTY)):
        return _zip_type(head)
    if head.startswith(_OLE2_MAGIC):
        return _ole2_type(head)
    if head.startswith(b"{\\rtf"):
        return "rtf"
    if head.startswith(_PNG_MAGIC):
        return "png"
    if head.startswith(_JPEG_MAGIC):
        return "jpeg"
    if head.startswith(_IMAGE_MAGICS) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image"
    text = head[:SNIFF_BYTES]
    if not _is_text(text):
        return "binary"
    if _is_eml(text):
        return "eml"
    lowered = text[:4096].lower()
    if any(marker in lowered for marker in _HTML_MARKERS):
        return "html"
    return "text"


# ---------------------------------------------------------------------------
# Text rule
# ---------------------------------------------------------------------------


def _media_type(declared_type: str | None) -> str:
    media = (declared_type or "").split(";", 1)[0].strip().lower()
    return "" if media in _IGNORED_DECLARED_TYPES else media


def _says_txt(ext: str, declared: str) -> bool:
    return ext in _TXT_EXTENSIONS or (not ext and declared == "text/plain")


def _text_kind(ext: str, declared: str) -> str | None:
    """The kind a text-like file's name, or else its declared type, gives."""
    if ext in _TXT_EXTENSIONS:
        return "txt"
    if ext in _HTML_EXTENSIONS:
        return "html"
    if ext:
        # Another extension (.csv, .json, .md ...) is not ours, whatever is declared.
        return None
    if declared == "text/plain":
        return "txt"
    if declared == "text/html":
        return "html"
    return None


def _is_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    controls = len(data) - len(data.translate(None, _CONTROL_BYTES))
    return controls < _MAX_CONTROL_SHARE * len(data)


def _is_eml(data: bytes) -> bool:
    """An RFC 5322 header block: every line before the first blank line is a header
    or a folded continuation, with enough of the right names."""
    block = re.split(rb"\r?\n\r?\n", data, maxsplit=1)
    lines = block[0].splitlines()
    if len(block) == 1 and len(data) >= SNIFF_BYTES and lines:
        lines = lines[:-1]  # the last line may be cut by the head
    names: list[bytes] = []
    for line in lines:
        if line[:1] in (b" ", b"\t") and names:
            continue
        match = _HEADER_LINE.match(line)
        if match is None:
            return False
        names.append(match.group(1).lower())
    return (
        len(names) >= _EML_MIN_HEADERS
        and _EML_REQUIRED in names
        and any(name in _EML_ANY_OF for name in names)
    )


# ---------------------------------------------------------------------------
# ZIP containers
# ---------------------------------------------------------------------------


def _zip_type(head: bytes) -> str:
    names, content_types = _zip_names(head)
    if "word/document.xml" in names or b"wordprocessingml.document.main" in content_types:
        return "docx"
    if "xl/workbook.xml" in names or b"spreadsheetml.sheet.main" in content_types:
        return "xlsx"
    if "ppt/presentation.xml" in names or b"presentationml.presentation.main" in content_types:
        return "pptx"
    return "zip"


def _zip_names(head: bytes) -> tuple[set[str], bytes]:
    """Part names visible in ``head``, and the ``[Content_Types].xml`` text if readable.

    A whole small ZIP is read with ``zipfile``. Otherwise the local file headers and
    any central directory headers inside ``head`` are scanned for names.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(head)) as archive:
            names = set(archive.namelist())
            content_types = b""
            if "[Content_Types].xml" in names:
                with archive.open("[Content_Types].xml") as part:
                    content_types = part.read(SNIFF_BYTES)
            return names, content_types
    except (zipfile.BadZipFile, ValueError, OSError, EOFError, NotImplementedError):
        pass

    names = set()
    content_types = b""
    for match in re.finditer(re.escape(_ZIP_LOCAL), head):
        start = match.start()
        fixed = head[start + 4 : start + 30]
        if len(fixed) < 26:
            continue
        _, _, method, _, _, _, size, _, name_len, extra_len = struct.unpack("<HHHHHIIIHH", fixed)
        name = head[start + 30 : start + 30 + name_len].decode("utf-8", "replace")
        names.add(name)
        if name == "[Content_Types].xml" and not content_types:
            data_start = start + 30 + name_len + extra_len
            content_types = _inflate(
                head[data_start : data_start + size] if size else head[data_start:], method
            )
    for match in re.finditer(re.escape(_ZIP_CENTRAL), head):
        start = match.start()
        fixed = head[start + 28 : start + 30]
        if len(fixed) < 2:
            continue
        (name_len,) = struct.unpack("<H", fixed)
        names.add(head[start + 46 : start + 46 + name_len].decode("utf-8", "replace"))
    return names, content_types


def _inflate(data: bytes, method: int) -> bytes:
    """As much of a stored or deflated part as ``data`` holds; ``b""`` on error."""
    if method == 0:
        return data
    if method != zipfile.ZIP_DEFLATED:
        return b""
    try:
        return zlib.decompressobj(-15).decompress(data, SNIFF_BYTES)
    except zlib.error:
        return b""


# ---------------------------------------------------------------------------
# OLE2 (Compound File Binary) containers
# ---------------------------------------------------------------------------

_OLE2_ENTRY = 128
_OLE2_STREAM_OR_STORAGE = frozenset({1, 2, 5})


def _ole2_type(head: bytes) -> str:
    names = _ole2_names(head)
    if any(name.startswith("__substg1.0_") for name in names):
        return "msg"
    if "WordDocument" in names:
        return "doc"
    if "Workbook" in names or "Book" in names:
        return "xls"
    if "PowerPoint Document" in names:
        return "ppt"
    return "ole2"


def _ole2_names(head: bytes) -> set[str]:
    """Names of the directory entries that fall inside ``head``.

    Directory sectors are sector-aligned and entries are 128 bytes, so every entry
    starts on a 128-byte boundary after the 512-byte header. A slot is taken as an
    entry only when its name length, object type and UTF-16 name are all valid.
    """
    names: set[str] = set()
    for start in range(512, len(head) - _OLE2_ENTRY + 1, _OLE2_ENTRY):
        name_len, obj_type = struct.unpack_from("<HB", head, start + 64)
        if obj_type not in _OLE2_STREAM_OR_STORAGE or not 2 <= name_len <= 64 or name_len % 2:
            continue
        raw = head[start : start + name_len]
        if raw[-2:] != b"\x00\x00":
            continue
        try:
            name = raw[:-2].decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        if name and "\x00" not in name:
            names.add(name)
    return names
