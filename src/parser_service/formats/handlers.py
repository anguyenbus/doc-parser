"""
handlers.py — parse one unit of a routed kind, and the handler versions (spec D3, D6).

A unit is one page of a PDF or one whole single-unit file (txt, docx, html, eml,
msg). ``parse_unit`` dispatches on the kind read from the manifest, never on the
file name, and runs in the page service's parse worker (``service.worker``) or, for
the local executor, in the page pipeline's own process.

Registry. ``HANDLERS`` maps each kind this image can parse to its handler; a kind
with no entry has no handler here, and ``settings.load_settings`` refuses to
enable it. A kind's task group adds its handler by adding an entry:

    HANDLERS["txt"] = _parse_txt                              # Group 4 (done)
    HANDLERS["docx"] = _parse_docx; HANDLERS["html"] = ...    # Group 5b (done)
    HANDLERS["eml"] = _parse_eml; HANDLERS["msg"] = _parse_msg  # Group 6 (done)

(in practice, in the ``HANDLERS`` literal below). A handler takes
``(path, page_number, escalate)`` and returns a reply
``(state, reason, reason_code, markdown, extra)``: the same shape the parse worker
sends back. ``reason_code`` None means "the text of ``reason`` before the first
``:``"; a deterministic failure puts the ``limits`` behind it in
``extra["limits"]`` (spec D5). A handler may raise: the caller turns an exception
into a ``failed`` record.

Versions. ``HANDLER_VERSIONS`` holds one version per handler (``email`` covers
``eml`` and ``msg``) and goes into the kind's ``pipeline_config``, so a bump gives
that kind a new config hash and new work, and leaves every other kind alone.
Bump a handler's version by hand whenever its output changes.

Imports nothing heavy at module level: ``settings`` reads ``HANDLERS`` at start-up
and must not load Docling. The TXT handler never loads Docling at all; the DOCX,
HTML and email handlers load it on their first call, inside the parse worker.

Empty output (spec D5). A single-unit handler whose conversion succeeds with empty
or whitespace-only markdown replies ``ok`` with route ``empty_document``, never
``failed`` ``empty_output``; PDF pages keep ``pdf.classify_page_result`` unchanged.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from parser_service.formats.result import Reply, apply_output_cap

# mail-parser logs attachment names at DEBUG, and Docling's email backend runs it
# before any handler code here does. Hold its logger at WARNING from import, so
# no attachment name or header reaches a log line (D6), even for the first email.
logging.getLogger("mailparser").setLevel(logging.WARNING)

# One version per handler. Bump by hand whenever the handler's output changes.
HANDLER_VERSIONS: dict[str, str] = {
    "pdf": "1",
    "txt": "1",
    "docx": "1",
    "html": "1",
    "email": "1",
}

# The handler (and version) of each routed kind: eml and msg share the email one.
HANDLER_NAMES: dict[str, str] = {
    "pdf": "pdf",
    "txt": "txt",
    "docx": "docx",
    "html": "html",
    "eml": "email",
    "msg": "email",
}

# A handler: (unit path, page number, escalate) -> reply.
UnitHandler = Callable[[Path, int, bool], "Reply"]


class NoHandlerError(LookupError):
    """The kind has no handler in this image."""


def _parse_pdf_page(path: Path, page_number: int, escalate: bool) -> Reply:
    """A PDF page: ``pdf.parse_page`` (blank probe, then ``parse_to_markdown``)."""
    # Imported on use: it loads the Docling path, which a process that only reads
    # settings (the page service's own process, the scripts' start-up check) avoids.
    from parser_service.formats import pdf

    state, reason, markdown, extra = pdf.parse_page(path, page_number, escalate)
    return state, reason, None, markdown, extra


ROUTE_EMPTY_DOCUMENT = "empty_document"
ROUTE_TXT_PASSTHROUGH = "txt_passthrough"


def _route(markdown: str, route: str) -> str:
    """A single-unit kind's route: ``empty_document`` when the text is blank (D5)."""
    return route if markdown.strip() else ROUTE_EMPTY_DOCUMENT


# --- TXT (spec D6) ------------------------------------------------------------

# Binary input: NUL bytes over 1% of the file, or other C0 control bytes over 5%.
# The control bytes are the sniffer's: every byte below 0x20 except tab, LF, VT,
# FF, CR and ESC (and NUL, counted on its own).
_TXT_MAX_NUL_SHARE = 0.01
_TXT_MAX_CONTROL_SHARE = 0.05
_TXT_CONTROL_BYTES = bytes(
    b for b in range(0x01, 0x20) if b not in (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x1B)
)
_UTF8_BOM = b"\xef\xbb\xbf"


def _decode_txt(data: bytes) -> tuple[str, str]:
    """``(text, encoding)`` in the fixed order of spec D6; the last step never fails.

    Strict UTF-8 (``utf-8-sig`` when the file starts with a BOM, which is dropped),
    then strict cp1252, then charset-normalizer's best match, then latin-1. The
    order is fixed so that short cp1252 text is never guessed as another code page.
    """
    try:
        text = data.decode("utf-8-sig")
        return text, "utf-8-sig" if data.startswith(_UTF8_BOM) else "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        pass
    from charset_normalizer import from_bytes

    best = from_bytes(data).best()
    if best is not None:
        return str(best), best.encoding
    return data.decode("latin-1"), "latin-1"


def _parse_txt(path: Path, page_number: int, escalate: bool) -> Reply:
    """A TXT document, kept verbatim: decode, normalise line ends to LF, drop NUL.

    NUL-heavy or binary input is ``failed`` ``unsupported_type`` with empty
    ``limits`` (final until the handler changes). An empty or blank file is
    ``ok`` with route ``empty_document``. ``extra.encoding`` names the decoder.
    """
    data = path.read_bytes()
    size = len(data)
    nul = data.count(b"\x00")
    controls = size - len(data.translate(None, _TXT_CONTROL_BYTES))
    if nul > _TXT_MAX_NUL_SHARE * size or controls > _TXT_MAX_CONTROL_SHARE * size:
        return (
            "failed",
            f"unsupported_type: binary input ({nul} NUL and {controls} other control bytes "
            f"in {size})",
            "unsupported_type",
            "",
            {"route": None, "limits": {}},
        )
    text, encoding = _decode_txt(data)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return (
        "ok",
        None,
        None,
        text,
        {"route": _route(text, ROUTE_TXT_PASSTHROUGH), "encoding": encoding},
    )


# --- DOCX and HTML (spec D6) -------------------------------------------------

ROUTE_DOCLING_KEPT = "docling-kept"
FLAG_IMAGES_NOT_OCRD = "images_not_ocrd"
# Exception text kept in a record's reason, as the parse worker keeps it (D9).
_MAX_REASON_CHARS = 500


# The smallest embedded image, in pixels (width x height), that sets a document's
# ``images_not_ocrd`` flag (spec D6). Read by the parse worker; a flag never
# changes a unit's state, so this is neither a limit nor part of the config.
IMAGES_MIN_PIXELS_ENV = "PARSER_IMAGES_MIN_PIXELS"
DEFAULT_IMAGES_MIN_PIXELS = 40_000


def images_min_pixels() -> int:
    """The ``images_not_ocrd`` threshold in pixels (``PARSER_IMAGES_MIN_PIXELS``), at call time."""
    return int(os.environ.get(IMAGES_MIN_PIXELS_ENV, str(DEFAULT_IMAGES_MIN_PIXELS)))


# Pixel count given to an embedded image too large for Pillow to open: over any threshold.
_BOMB_IMAGE_PIXELS = 1 << 40


def _docx_image_pixels(doc: Any) -> list[int]:
    """The pixel count of every body-layer picture of a DOCX that has embedded data.

    Furniture pictures (headers, footers) and pictures with no image data (a
    linked image, a format Docling could not read) are left out.
    """
    from docling_core.types.doc.common.content_layer import ContentLayer

    pixels: list[int] = []
    for picture in getattr(doc, "pictures", None) or ():
        image = getattr(picture, "image", None)
        if getattr(picture, "content_layer", None) != ContentLayer.BODY or image is None:
            continue
        size = getattr(image, "size", None)
        if size is not None:
            pixels.append(int(size.width) * int(size.height))
    return pixels


class _DataImageFinder(HTMLParser):
    """Collects the ``src`` of every ``<img>`` whose source is an inline ``data:`` URI."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        for name, value in attrs:
            if name == "src" and value and value.lstrip()[:5].lower() == "data:":
                self.sources.append(value.strip())


def _data_uri_image_pixels(uri: str) -> int | None:
    """The pixel count of the image in a ``data:`` URI (None: not a readable image).

    Only the image header is read; the pixels are never decoded.
    """
    import base64
    from urllib.parse import unquote_to_bytes

    header, sep, payload = uri[5:].partition(",")
    if not sep or not header.lower().startswith("image/"):
        return None
    try:
        if header.lower().endswith(";base64"):
            data = base64.b64decode("".join(payload.split()))
        else:
            data = unquote_to_bytes(payload)
    except ValueError:  # bad base64: not counted
        return None
    return _image_bytes_pixels(data)


def _image_bytes_pixels(data: bytes) -> int | None:
    """The pixel count of an encoded image (None: not a readable image).

    Only the image header is read; the pixels are never decoded.
    """
    from io import BytesIO

    from PIL import Image

    try:
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
    except Image.DecompressionBombError:
        return _BOMB_IMAGE_PIXELS
    except Exception:  # noqa: BLE001 — an unreadable image is not counted
        return None
    return int(width) * int(height)


def _html_image_pixels(path: Path) -> list[int]:
    """The pixel count of every ``<img>`` embedded as a ``data:`` URI in an HTML file.

    Docling never fetches an HTML image (see ``parsing.document``), so its
    pictures carry no data; embedded images are read from the source instead.
    Remote, local and ``cid:`` sources do not count.
    """
    data = path.read_bytes()
    if b"data:" not in data.lower():
        return []
    finder = _DataImageFinder()
    finder.feed(data.decode("utf-8", errors="replace"))
    finder.close()
    pixels = [_data_uri_image_pixels(uri) for uri in finder.sources]
    return [count for count in pixels if count is not None]


def _image_flags(pixels: list[int]) -> list[str]:
    """``["images_not_ocrd"]`` when an image has at least ``PARSER_IMAGES_MIN_PIXELS`` pixels."""
    threshold = images_min_pixels()
    return [FLAG_IMAGES_NOT_OCRD] if any(count >= threshold for count in pixels) else []


def _parse_whole_doc(kind: str, path: Path) -> Reply:
    """A DOCX, HTML or email document: Docling's whole-document markdown with the output fixes.

    No blank-page probe. Converts with ``kind``'s single-format converter.
    Success is ``ok`` with route ``docling-kept``, or ``empty_document`` when the
    text is blank (D5). A conversion failure is ``failed`` ``docling_failed``
    (retried under the attempt limit). ``extra.flags`` holds ``images_not_ocrd``
    when an embedded image is at least ``PARSER_IMAGES_MIN_PIXELS``; a flag never
    changes the state. An email (``eml``, ``msg``) also gets ``extra.email``, its
    headers and attachment list read from the message itself (``_email_facts``).
    """
    from parser_service.parsing import document  # Docling is imported lazily there

    container: dict[str, Any] = {"warnings": [], "elements": []}
    page_routes: list[dict[str, Any]] = []
    markdown, doc = document._convert_whole_doc(path, container, page_routes, kind)
    warnings = list(container["warnings"])
    if doc is None:
        detail = next(
            (str(w.get("message", "")) for w in warnings if w.get("code") == "docling_failed"),
            "conversion failed",
        )
        return (
            "failed",
            f"docling_failed: {detail}"[:_MAX_REASON_CHARS],
            "docling_failed",
            "",
            {"route": None, "warnings": warnings},
        )
    email: dict[str, Any] | None = None
    if kind in EMAIL_KINDS:
        email, pixels = _email_facts(kind, path)
    elif kind == "docx":
        pixels = _docx_image_pixels(doc)
    else:
        pixels = _html_image_pixels(path)
    route = _route(markdown, ROUTE_DOCLING_KEPT)
    extra: dict[str, Any] = {
        "route": route,
        "route_reason": None,
        "page_routes": [{**r, "route": route} for r in page_routes],
        "warnings": warnings,
        "flags": _image_flags(pixels),
    }
    if email is not None:
        extra["email"] = email
    return "ok", None, None, markdown, extra


def _parse_docx(path: Path, page_number: int, escalate: bool) -> Reply:
    """A DOCX document (the service's cost guard has already passed it)."""
    return _parse_whole_doc("docx", path)


def _parse_html(path: Path, page_number: int, escalate: bool) -> Reply:
    """An HTML document, converted with no network or local-file access."""
    return _parse_whole_doc("html", path)


# --- Email: EML and MSG (spec D6) -------------------------------------------

EMAIL_KINDS = ("eml", "msg")
# MAPI properties read from a .msg: PidTagRecipientType on a recipient row (2 is
# Cc, 3 Bcc; anything else is To, as Docling's email backend reads it) and
# PidTagAttachContentId on an attachment (an inline part of the body).
_MSG_RECIPIENT_TYPE = 0x0C15
_MSG_RECIPIENT_CC = 2
_MSG_RECIPIENT_BCC = 3
_MSG_ATTACH_CONTENT_ID = 0x3712
# RFC 5322 specials, minus ".": a display name holding one is quoted (as Docling does).
_ADDRESS_SPECIALS = re.compile(r'[][()<>@,:;"\\]')
_ADDRESS_ESCAPES = re.compile(r'[\\"]')


def _one_line(value: str) -> str:
    """A header value collapsed to one line of single-spaced text."""
    return " ".join(value.split())


def _decoded_header(value: str) -> str:
    """``value`` with any RFC 2047 encoded-words decoded (unchanged if they do not decode)."""
    from email.header import decode_header, make_header

    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 — a malformed encoded-word is kept as it is
        return value


def _format_addresses(addresses: Any) -> str | None:
    """``Name <address>`` pairs joined by ``, `` (None when there are none).

    The display names are decoded and quoted as Docling's email backend renders
    them in the markdown, so ``extra.email`` and the text agree.
    """
    formatted: list[str] = []
    for name, address in addresses or ():
        name = _one_line(_decoded_header(str(name or "")))
        address = _one_line(str(address or ""))
        if name and _ADDRESS_SPECIALS.search(name):
            name = '"' + _ADDRESS_ESCAPES.sub(r"\\\g<0>", name) + '"'
        if name and address:
            formatted.append(f"{name} <{address}>")
        elif name or address:
            formatted.append(name or address)
    return ", ".join(formatted) or None


def _iso_date(value: Any) -> str | None:
    """A message date as ISO 8601 (as in the markdown); None when absent or unparseable."""
    from datetime import datetime

    return value.isoformat() if isinstance(value, datetime) else None


def _text_or_none(value: Any) -> str | None:
    """A header text on one line; None when absent or blank."""
    return (_one_line(value) or None) if isinstance(value, str) else None


def _inline_image_parts(message: Any) -> list[bytes]:
    """The decoded bytes of every inline ``image/*`` part of a parsed MIME message.

    Inline means the part has a ``Content-ID`` or an ``inline`` disposition. An
    attached message (``message/rfc822``) is an attachment: its parts are not
    walked.
    """
    images: list[bytes] = []
    pending = [message]
    while pending:
        part = pending.pop()
        if part.is_multipart():
            if part.get_content_type() != "message/rfc822":
                pending.extend(reversed(part.get_payload()))
            continue
        inline = part.get("Content-ID") or part.get_content_disposition() == "inline"
        if part.get_content_maintype() == "image" and inline:
            data = part.get_payload(decode=True)
            if isinstance(data, bytes):
                images.append(data)
    return images


# An attached message (``message/rfc822``, ``message/global``, ...) is an
# attachment, but mail-parser walks into it, so Docling would put its body in the
# document. Before conversion each one is replaced by a stub with no content and
# this one header: its original size, for ``extra.email``.
_ATTACHED_MESSAGE_SIZE_HEADER = "X-Parser-Attached-Message-Bytes"
_ATTACHED_MESSAGE_SIZE = re.compile(rf"^{_ATTACHED_MESSAGE_SIZE_HEADER}: (\d+)$", re.MULTILINE)


def _detach_attached_messages(raw: bytes) -> bytes | None:
    """The EML with every attached message's content removed (None: it has none).

    Each nested ``message/*`` part (a delivery status report excepted) keeps its
    own headers, so the attachment list is unchanged, and gets a stub holding
    only its original size. The top-level message is never touched, and an
    email with no attached message is not re-serialised at all.
    """
    if b"message/" not in raw.lower():
        return None
    from email import message_from_bytes
    from email.message import Message

    root = message_from_bytes(raw)
    found = False
    pending = [] if not root.is_multipart() else list(root.get_payload())
    while pending:
        part = pending.pop()
        if not part.is_multipart():
            continue
        content_type = part.get_content_type()
        if part.get_content_maintype() == "message" and content_type != "message/delivery-status":
            size = 0
            for inner in part.get_payload():
                try:
                    size += len(inner.as_bytes())
                except Exception:  # noqa: BLE001 — an unprintable inner message counts 0
                    pass
            stub = Message()
            stub[_ATTACHED_MESSAGE_SIZE_HEADER] = str(size)
            part.set_payload([stub])
            found = True
        else:
            pending.extend(part.get_payload())
    return root.as_bytes() if found else None


def _base64_payload_bytes(payload: str) -> bytes:
    """A base64 attachment payload decoded as a MIME reader does: leniently.

    mail-parser keeps the wire text of a base64 part; bad padding or stray
    characters must not fail the document, so the stdlib's tolerant part
    decoder is used instead of a strict ``b64decode``.
    """
    from email.message import Message

    part = Message()
    part["Content-Transfer-Encoding"] = "base64"
    part.set_payload(payload)
    data = part.get_payload(decode=True)
    return data if isinstance(data, bytes) else b""


def _eml_facts(path: Path) -> tuple[dict[str, Any], list[int]]:
    """``(email, image pixels)`` of an EML, read with mail-parser as Docling reads it.

    The attachments are mail-parser's, in its order, so they match the
    ``Attachments`` lines of the markdown. ``size`` is the decoded byte length.
    """
    import mailparser

    mail = mailparser.parse_from_bytes(path.read_bytes())
    attachments: list[dict[str, Any]] = []
    for index, attachment in enumerate(mail.attachments or ()):
        payload = attachment.get("payload") or ""
        if attachment.get("binary"):
            size = len(_base64_payload_bytes(str(payload)))
        else:  # an attached message (a stub, see _detach_attached_messages), kept as text
            stub = _ATTACHED_MESSAGE_SIZE.search(str(payload))
            size = (
                int(stub.group(1))
                if stub
                else len(str(payload).encode("utf-8", errors="surrogateescape"))
            )
        attachments.append(
            {
                "name": _one_line(str(attachment.get("filename") or ""))
                or f"attachment-{index + 1}",
                "type": _text_or_none(attachment.get("mail_content_type")),
                "size": size,
            }
        )
    headers = {
        "from": _format_addresses(mail.from_),
        "to": _format_addresses(mail.to),
        "cc": _format_addresses(mail.cc),
        "date": _iso_date(mail.date),
        "subject": _text_or_none(mail.subject),
    }
    pixels = [_image_bytes_pixels(data) for data in _inline_image_parts(mail.message)]
    return {"headers": headers, "attachments": attachments}, [n for n in pixels if n is not None]


def _msg_facts(path: Path) -> tuple[dict[str, Any], list[int]]:
    """``(email, image pixels)`` of an Outlook MSG, read with python-oxmsg.

    No RTF body is recovered (spec non-goal). An attachment with a content id is
    an inline part of the body; an inline ``image/*`` one counts for the flag.
    """
    from oxmsg import Message

    message = Message.load(str(path))
    grouped: dict[str, list[tuple[str, str]]] = {"to": [], "cc": []}
    for recipient in message.recipients:
        kind = recipient.properties.int_prop_value(_MSG_RECIPIENT_TYPE)
        if kind == _MSG_RECIPIENT_BCC:
            continue
        pair = (recipient.name or "", recipient.email_address or "")
        grouped["cc" if kind == _MSG_RECIPIENT_CC else "to"].append(pair)
    sender = message.sender
    headers = {
        "from": _format_addresses(getaddresses([sender])) if sender else None,
        "to": _format_addresses(grouped["to"]),
        "cc": _format_addresses(grouped["cc"]),
        "date": _iso_date(message.sent_date),
        "subject": _text_or_none(message.subject),
    }
    attachments: list[dict[str, Any]] = []
    pixels: list[int] = []
    for index, attachment in enumerate(message.attachments):
        mime_type = attachment.mime_type or "application/octet-stream"
        attachments.append(
            {
                "name": _one_line(attachment.file_name or "") or f"attachment-{index + 1}",
                "type": mime_type,
                "size": attachment.size,
            }
        )
        content_id = attachment.properties.str_prop_value(_MSG_ATTACH_CONTENT_ID)
        if mime_type.lower().startswith("image/") and content_id and attachment.file_bytes:
            count = _image_bytes_pixels(attachment.file_bytes)
            if count is not None:
                pixels.append(count)
    return {"headers": headers, "attachments": attachments}, pixels


def _email_facts(kind: str, path: Path) -> tuple[dict[str, Any], list[int]]:
    """``extra.email`` (``{headers: {from, to, cc, date, subject}, attachments:
    [{name, type, size}]}``) and the inline images' pixel counts of an email.

    Docling's labels have no size, so the message is read again here. Neither
    the headers nor the attachment names are ever logged (D6): mail-parser logs
    attachment names at DEBUG, so its logger is held at WARNING from module import.
    """
    return _eml_facts(path) if kind == "eml" else _msg_facts(path)


def _parse_eml(path: Path, page_number: int, escalate: bool) -> Reply:
    """An EML email: headers, body (the text/plain part when present) and an attachment list.

    An attached message is listed, never converted: when there is one, the email
    is converted (and read for ``extra.email``) from a copy with its content
    removed (``_detach_attached_messages``).
    """
    import tempfile

    detached = _detach_attached_messages(path.read_bytes())
    if detached is None:
        return _parse_whole_doc("eml", path)
    with tempfile.TemporaryDirectory(prefix="eml-") as folder:
        copy = Path(folder) / "source.eml"
        copy.write_bytes(detached)
        return _parse_whole_doc("eml", copy)


def _parse_msg(path: Path, page_number: int, escalate: bool) -> Reply:
    """An Outlook MSG email, read through python-oxmsg; the same output as an EML."""
    return _parse_whole_doc("msg", path)


# The handlers in this image, by kind (eml and msg share the email handler version).
HANDLERS: dict[str, UnitHandler] = {
    "pdf": _parse_pdf_page,
    "txt": _parse_txt,
    "docx": _parse_docx,
    "html": _parse_html,
    "eml": _parse_eml,
    "msg": _parse_msg,
}


def handler_version(kind: str) -> str:
    """The version of ``kind``'s handler (read at call time; ``ValueError`` for an unknown kind)."""
    try:
        return HANDLER_VERSIONS[HANDLER_NAMES[kind]]
    except KeyError:
        raise ValueError(f"unknown kind {kind!r}") from None


def has_handler(kind: str) -> bool:
    """Whether this image can parse ``kind``."""
    return kind in HANDLERS


def parse_unit(
    kind: str,
    path: str | Path,
    page_number: int,
    escalate: bool,
    output_cap: int | None = None,
) -> Reply:
    """Parse one unit of ``kind`` with its handler; return the reply.

    ``output_cap`` (bytes, single-unit kinds only) replaces a reply whose markdown
    is over the cap with a ``failed`` ``output_too_large`` one. Raises
    ``NoHandlerError`` for a kind with no handler in this image.
    """
    handler = HANDLERS.get(kind)
    if handler is None:
        raise NoHandlerError(f"no handler for kind {kind!r} in this image")
    reply = handler(Path(path), page_number, escalate)
    if output_cap is not None:
        reply = apply_output_cap(kind, reply, output_cap)
    return reply
