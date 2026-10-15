"""
guards.py — the cost guards: DOCX without decompressing more than one part, XLSX
without decompressing more than its worksheets, images without decoding any pixel.

Pure Python (``zipfile``, ``re``); it never imports Docling. Two building blocks,
shared by the corpus inventory (``scripts/corpus_inventory.py``) and the DOCX cost
guard (spec D7):

    zip_index_stats(infos)    numbers from the ZIP index alone: entries, expanded and
                              compressed totals, the largest part, the overall ratio,
                              the worst ratio of any part of 1 MiB or more, duplicate
                              names and encrypted entries. No decompression.
    count_elements(stream)    one streamed pass over an XML part, counting paragraph
                              and table-cell element starts by local name, with an
                              optional byte cap and early stop at given counts.

``docx_stats(path)`` combines them for one DOCX: the index numbers plus the counts
from ``word/document.xml``.

``check_docx(path, limits)`` is the cost guard (spec D7). The page service runs it
before a DOCX reaches the parse worker; the first failed check wins:

  1. the ZIP index (``zip_bomb``), no decompression: more than ``max_entries``
     entries, a duplicate name, an encrypted entry, the expanded total over
     ``max_expanded_mb``, a part over ``max_part_mb``, the overall ratio over
     ``max_ratio``, or the ratio of a part of 1 MiB or more over ``max_ratio``;
  2. one streamed pass over ``word/document.xml``, capped at its declared size:
     a stream that disagrees with the index (longer than declared, bad CRC) is
     ``zip_bomb`` ``index mismatch``; more than ``max_paragraphs`` paragraphs or
     ``max_table_cells`` table cells is ``too_complex``.

A rejection returns ``(reason_code, detail, limits)``, where ``limits`` holds the
limit values behind it (the four ZIP limits, or the two count limits), stored in
the unit's record so the failure is final until one of them changes (spec D5).
Details hold numbers only, never a part name or document text.

``check_xlsx(path, limits)`` is the XLSX cost guard. It runs the same ZIP index
checks as the DOCX guard (``zip_bomb``), then streams every ``xl/worksheets/*.xml``
part once, capped at its declared size, counting cells and reading each cell's
reference (``r="AB12"``). Docling loads the whole workbook and walks each sheet's
used range, so two things cost: the number of cells (about 2.7 KB of memory each)
and the used-range area, rows x columns (about 0.2 KB each). A workbook with two
cells at A1 and XFD1048576 has a used range of 17 billion cells and never finishes.
More than ``xlsx_max_cells`` cells, or a summed used-range area over
``xlsx_max_grid_cells``, is ``too_complex``. ``xlsx_stats(path)`` gives the same
numbers for the corpus inventory, plus the formulas stored without a value.

``check_image(path, max_megapixels)`` is the image cost guard. It reads the
dimensions a PNG (``IHDR``, and ``acTL`` for an animated PNG's frame count) or a
JPEG (the first ``SOF`` segment) declares, and rejects an image over
``max_megapixels`` (width x height x frames) as ``too_complex`` before anything
decodes it: a file of a few MB can declare an image that needs gigabytes. A header
it cannot read passes; the parser then reports the file.

Counting. A paragraph is ``<p`` and a table cell ``<tc``, each with any namespace
prefix (or none), followed by whitespace, ``/`` or ``>``: ``<w:pPr>``, ``<w:pict>``
and ``<w:tcPr>`` do not match; DrawingML ``<a:p>`` does (it is parsed too, so
over-counting is the conservative side). The longest match is 37 bytes, so the pass
keeps at most the last 36 bytes after the last match as a carry into the next chunk:
a match split across two chunks is counted once and no match is counted twice.
"""

from __future__ import annotations

import re
import zipfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

CHUNK_BYTES = 1 << 20
LARGE_PART_BYTES = 1 << 20
DOCUMENT_XML = "word/document.xml"

ELEMENT_START = re.compile(rb"<(?:[A-Za-z_][\w.-]{0,31}:)?(p|tc)[\s/>]")
_MAX_MATCH = 37  # "<" + 32-byte prefix + ":" + "tc" + terminator
_CARRY = _MAX_MATCH - 1
_MB = 1_000_000  # the limits are in MB of 1,000,000 bytes, like the ConfigMap

REASON_ZIP_BOMB = "zip_bomb"
REASON_TOO_COMPLEX = "too_complex"

# A guard rejection: (reason code, detail, the limit values behind it).
GuardRejection = tuple[str, str, dict[str, int]]


@dataclass(frozen=True)
class ZipIndexStats:
    """What a ZIP's index says, before anything is decompressed."""

    entries: int
    expanded_bytes: int
    compressed_bytes: int
    largest_part_bytes: int
    ratio: float  # expanded / max(1, compressed), over every entry
    max_large_part_ratio: float  # worst file_size / max(1, compress_size), parts >= 1 MiB
    duplicate_names: int
    encrypted_entries: int


@dataclass(frozen=True)
class ElementCounts:
    """The result of one streamed pass over an XML part."""

    paragraphs: int
    table_cells: int
    bytes_read: int
    limit_exceeded: bool  # paragraphs or cells went over their maximum; the pass stopped
    truncated: bool  # the byte cap was reached with more of the stream left unread


@dataclass(frozen=True)
class DocxStats:
    """The index numbers of a DOCX plus the element counts of its main part."""

    index: ZipIndexStats
    paragraphs: int | None  # None when the file has no word/document.xml
    table_cells: int | None
    document_xml_bytes: int  # the declared expanded size of word/document.xml


def zip_index_stats(infos: Iterable[zipfile.ZipInfo]) -> ZipIndexStats:
    """Index-only numbers for a ZIP (``ZipFile.infolist()``); no decompression."""
    entries = expanded = compressed = largest = duplicates = encrypted = 0
    worst_large = 0.0
    seen: set[str] = set()
    for info in infos:
        entries += 1
        expanded += info.file_size
        compressed += info.compress_size
        largest = max(largest, info.file_size)
        if info.filename in seen:
            duplicates += 1
        seen.add(info.filename)
        if info.flag_bits & 0x1:
            encrypted += 1
        if info.file_size >= LARGE_PART_BYTES:
            worst_large = max(worst_large, info.file_size / max(1, info.compress_size))
    return ZipIndexStats(
        entries=entries,
        expanded_bytes=expanded,
        compressed_bytes=compressed,
        largest_part_bytes=largest,
        ratio=expanded / max(1, compressed),
        max_large_part_ratio=worst_large,
        duplicate_names=duplicates,
        encrypted_entries=encrypted,
    )


def count_elements(
    stream: IO[bytes],
    *,
    chunk_size: int = CHUNK_BYTES,
    max_bytes: int | None = None,
    max_paragraphs: int | None = None,
    max_table_cells: int | None = None,
) -> ElementCounts:
    """Count paragraph and table-cell starts in ``stream``, one chunk at a time.

    Reads at most ``max_bytes`` (all of it when None; one extra byte is read to tell
    a cut stream from one that ends at the cap). Stops after the chunk in which
    paragraphs exceed ``max_paragraphs`` or cells exceed ``max_table_cells``.
    Errors from the stream propagate: for a ``ZipFile.open`` entry, a stream longer
    than its declared size or a bad CRC raises ``zipfile.BadZipFile``.
    """
    paragraphs = cells = read = 0
    carry = b""
    while max_bytes is None or read < max_bytes:
        want = chunk_size if max_bytes is None else min(chunk_size, max_bytes - read)
        chunk = stream.read(want)
        if not chunk:
            return ElementCounts(paragraphs, cells, read, limit_exceeded=False, truncated=False)
        read += len(chunk)
        data = carry + chunk
        end = 0
        for match in ELEMENT_START.finditer(data):
            if match.group(1) == b"p":
                paragraphs += 1
            else:
                cells += 1
            end = match.end()
        carry = data[max(end, len(data) - _CARRY) :]
        if (max_paragraphs is not None and paragraphs > max_paragraphs) or (
            max_table_cells is not None and cells > max_table_cells
        ):
            return ElementCounts(paragraphs, cells, read, limit_exceeded=True, truncated=False)
    truncated = bool(stream.read(1))
    return ElementCounts(paragraphs, cells, read, limit_exceeded=False, truncated=truncated)


def docx_stats(path: str | Path, *, max_bytes: int | None = None) -> DocxStats:
    """Index numbers and ``word/document.xml`` element counts for the DOCX at ``path``.

    ``max_bytes`` caps the streamed pass (None reads the whole part). Raises
    ``zipfile.BadZipFile`` for a file that is not a ZIP, or whose main part's
    stream disagrees with its index.
    """
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        index = zip_index_stats(infos)
        main = next((info for info in infos if info.filename == DOCUMENT_XML), None)
        if main is None:
            return DocxStats(index, None, None, 0)
        with archive.open(main) as part:
            counts = count_elements(part, max_bytes=max_bytes)
    return DocxStats(index, counts.paragraphs, counts.table_cells, main.file_size)


@dataclass(frozen=True)
class DocxLimits:
    """The DOCX guard thresholds (spec D7). Field names are the keys stored in ``limits``."""

    docx_max_expanded_mb: int
    docx_max_part_mb: int
    docx_max_ratio: int
    docx_max_entries: int
    docx_max_paragraphs: int
    docx_max_table_cells: int

    def zip_thresholds(self) -> tuple[int, int, int, int]:
        """``(max entries, max expanded MB, max part MB, max ratio)`` for ``_index_problem``."""
        return (
            self.docx_max_entries,
            self.docx_max_expanded_mb,
            self.docx_max_part_mb,
            self.docx_max_ratio,
        )

    def zip_limits(self) -> dict[str, int]:
        """The four ZIP limits, stored with a ``zip_bomb`` record."""
        return {
            "docx_max_expanded_mb": self.docx_max_expanded_mb,
            "docx_max_part_mb": self.docx_max_part_mb,
            "docx_max_ratio": self.docx_max_ratio,
            "docx_max_entries": self.docx_max_entries,
        }

    def count_limits(self) -> dict[str, int]:
        """The two element-count limits, stored with a ``too_complex`` record."""
        return {
            "docx_max_paragraphs": self.docx_max_paragraphs,
            "docx_max_table_cells": self.docx_max_table_cells,
        }


def _index_problem(index: ZipIndexStats, limits: DocxLimits | XlsxLimits) -> str | None:
    """The first ZIP index check (spec D7 order) that ``index`` fails, as a detail."""
    max_entries, max_expanded_mb, max_part_mb, max_ratio = limits.zip_thresholds()
    if index.entries > max_entries:
        return f"{index.entries} entries, over {max_entries}"
    if index.duplicate_names:
        return f"{index.duplicate_names} duplicate entry name(s)"
    if index.encrypted_entries:
        return f"{index.encrypted_entries} encrypted entry(ies)"
    if index.expanded_bytes > max_expanded_mb * _MB:
        return f"expanded {index.expanded_bytes / _MB:.1f} MB, over {max_expanded_mb} MB"
    if index.largest_part_bytes > max_part_mb * _MB:
        return f"a part of {index.largest_part_bytes / _MB:.1f} MB, over {max_part_mb} MB"
    if index.ratio > max_ratio:
        return f"ratio {index.ratio:.0f}:1, over {max_ratio}:1"
    if index.max_large_part_ratio > max_ratio:
        return (
            f"a part of 1 MiB or more at ratio {index.max_large_part_ratio:.0f}:1, "
            f"over {max_ratio}:1"
        )
    return None


def check_docx(
    path: str | Path, limits: DocxLimits, *, chunk_size: int = CHUNK_BYTES
) -> GuardRejection | None:
    """The DOCX cost guard (spec D7): ``(reason_code, detail, limits)``, or None to parse.

    Reads the ZIP index, then at most the declared size of ``word/document.xml``
    (never more than ``docx_max_part_mb``), so a rejection costs well under a
    second. A file with no ``word/document.xml``, or one whose central directory
    cannot be read at all, passes: it is not costly, and the handler reports it.
    """
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return None
    with archive:
        infos = archive.infolist()
        problem = _index_problem(zip_index_stats(infos), limits)
        if problem is not None:
            return REASON_ZIP_BOMB, problem, limits.zip_limits()
        main = next((info for info in infos if info.filename == DOCUMENT_XML), None)
        if main is None:
            return None
        cap = min(main.file_size, limits.docx_max_part_mb * _MB)
        try:
            with archive.open(main) as part:
                counts = count_elements(
                    part,
                    chunk_size=chunk_size,
                    max_bytes=cap,
                    max_paragraphs=limits.docx_max_paragraphs,
                    max_table_cells=limits.docx_max_table_cells,
                )
        except (zipfile.BadZipFile, EOFError, zlib.error):
            # A stream longer than its declared size, cut short, or with a bad CRC.
            return REASON_ZIP_BOMB, "index mismatch", limits.zip_limits()
    if counts.limit_exceeded:
        return (
            REASON_TOO_COMPLEX,
            f"{counts.paragraphs} paragraphs and {counts.table_cells} table cells in the "
            f"first {counts.bytes_read / _MB:.1f} MB (limits {limits.docx_max_paragraphs} "
            f"and {limits.docx_max_table_cells})",
            limits.count_limits(),
        )
    if counts.truncated:  # more bytes than the index declared: never parse them
        return REASON_ZIP_BOMB, "index mismatch", limits.zip_limits()
    return None


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------

WORKSHEET_PREFIX = "xl/worksheets/"
# A cell's start tag, any namespace prefix: ``<c r="B2" s="1" t="s">`` or ``<c/>``.
# ``<col``, ``<cols`` and ``<cfRule`` do not match (a letter follows the ``c``).
CELL_START = re.compile(rb"<(?:[A-Za-z_][\w.-]{0,31}:)?c(\s[^<>]*)?/?>")
_CELL_REF = re.compile(rb'\br="([A-Z]{1,3})([0-9]{1,7})"')
# A formula with no stored value: ``<f>..</f>`` or ``<f .../>`` followed by the end
# of the cell or an empty ``<v>``. Excel stores the value; files written by
# libraries that never calculate often do not, and Docling then reads an empty cell.
FORMULA_NO_VALUE = re.compile(
    rb"(?:</(?:\w+:)?f>|<(?:\w+:)?f\b[^<>]*/>)\s*"
    rb"(?:</(?:\w+:)?c>|<(?:\w+:)?v\s*/>|<(?:\w+:)?v>\s*</(?:\w+:)?v>)"
)
# Bytes kept across a chunk boundary: room for one cell start tag or formula ending.
_XLSX_CARRY = 512


@dataclass(frozen=True)
class SheetCounts:
    """One streamed pass over a worksheet part."""

    cells: int
    max_row: int  # the largest row number of any cell reference (0: none)
    max_col: int  # the largest column number of any cell reference (0: none)
    formulas_without_value: int
    bytes_read: int
    limit_exceeded: bool  # cells went over ``max_cells``; the pass stopped
    truncated: bool  # the byte cap was reached with more of the stream left unread

    @property
    def grid_cells(self) -> int:
        """The used range's area, rows x columns, as Docling walks it."""
        return self.max_row * self.max_col


def _column_number(letters: bytes) -> int:
    number = 0
    for letter in letters:
        number = number * 26 + (letter - 64)
    return number


def count_sheet_cells(
    stream: IO[bytes],
    *,
    chunk_size: int = CHUNK_BYTES,
    max_bytes: int | None = None,
    max_cells: int | None = None,
) -> SheetCounts:
    """Count the cells of one worksheet part and the largest row and column they use.

    Reads at most ``max_bytes`` (all when None), and stops after the chunk in which
    the cells exceed ``max_cells``. Errors from the stream propagate, as in
    ``count_elements``.
    """
    cells = max_row = max_col = formulas = read = 0
    carry = b""
    while max_bytes is None or read < max_bytes:
        want = chunk_size if max_bytes is None else min(chunk_size, max_bytes - read)
        chunk = stream.read(want)
        if not chunk:
            return SheetCounts(cells, max_row, max_col, formulas, read, False, False)
        read += len(chunk)
        data = carry + chunk
        end = 0
        for match in CELL_START.finditer(data):
            cells += 1
            ref = _CELL_REF.search(match.group(1) or b"")
            if ref:
                max_col = max(max_col, _column_number(ref.group(1)))
                max_row = max(max_row, int(ref.group(2)))
            end = max(end, match.end())
        for match in FORMULA_NO_VALUE.finditer(data):
            formulas += 1
            end = max(end, match.end())
        carry = data[max(end, len(data) - _XLSX_CARRY) :]
        if max_cells is not None and cells > max_cells:
            return SheetCounts(cells, max_row, max_col, formulas, read, True, False)
    truncated = bool(stream.read(1))
    return SheetCounts(cells, max_row, max_col, formulas, read, False, truncated)


def _worksheets(infos: Iterable[zipfile.ZipInfo]) -> list[zipfile.ZipInfo]:
    return [
        info
        for info in infos
        if info.filename.startswith(WORKSHEET_PREFIX)
        and info.filename.endswith(".xml")
        and "/" not in info.filename[len(WORKSHEET_PREFIX) :]
    ]


@dataclass(frozen=True)
class XlsxStats:
    """The index numbers of an XLSX plus the counts of its worksheets."""

    index: ZipIndexStats
    sheets: int
    cells: int
    grid_cells: int  # summed over the sheets
    formulas_without_value: int


def xlsx_stats(path: str | Path, *, max_bytes: int | None = None) -> XlsxStats:
    """Index numbers and worksheet counts for the XLSX at ``path``.

    ``max_bytes`` caps the streamed pass of each worksheet (None reads it all).
    Raises ``zipfile.BadZipFile`` for a file that is not a ZIP, or whose worksheet
    streams disagree with its index.
    """
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        sheets = _worksheets(infos)
        totals = [0, 0, 0]
        for info in sheets:
            with archive.open(info) as part:
                counts = count_sheet_cells(part, max_bytes=max_bytes)
            totals[0] += counts.cells
            totals[1] += counts.grid_cells
            totals[2] += counts.formulas_without_value
    return XlsxStats(zip_index_stats(infos), len(sheets), *totals)


@dataclass(frozen=True)
class XlsxLimits:
    """The XLSX guard thresholds. Field names are the keys stored in ``limits``."""

    xlsx_max_expanded_mb: int
    xlsx_max_part_mb: int
    xlsx_max_ratio: int
    xlsx_max_entries: int
    xlsx_max_cells: int
    xlsx_max_grid_cells: int

    def zip_thresholds(self) -> tuple[int, int, int, int]:
        """``(max entries, max expanded MB, max part MB, max ratio)`` for ``_index_problem``."""
        return (
            self.xlsx_max_entries,
            self.xlsx_max_expanded_mb,
            self.xlsx_max_part_mb,
            self.xlsx_max_ratio,
        )

    def zip_limits(self) -> dict[str, int]:
        """The four ZIP limits, stored with a ``zip_bomb`` record."""
        return {
            "xlsx_max_expanded_mb": self.xlsx_max_expanded_mb,
            "xlsx_max_part_mb": self.xlsx_max_part_mb,
            "xlsx_max_ratio": self.xlsx_max_ratio,
            "xlsx_max_entries": self.xlsx_max_entries,
        }

    def count_limits(self) -> dict[str, int]:
        """The two size limits, stored with a ``too_complex`` record."""
        return {
            "xlsx_max_cells": self.xlsx_max_cells,
            "xlsx_max_grid_cells": self.xlsx_max_grid_cells,
        }


def check_xlsx(
    path: str | Path, limits: XlsxLimits, *, chunk_size: int = CHUNK_BYTES
) -> GuardRejection | None:
    """The XLSX cost guard: ``(reason_code, detail, limits)``, or None to parse.

    Reads the ZIP index, then each worksheet up to its declared size (never more
    than ``xlsx_max_part_mb``), stopping once the cells pass the limit. A file whose
    central directory cannot be read passes: the handler reports it.
    """
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return None
    cells = grid = 0
    with archive:
        infos = archive.infolist()
        problem = _index_problem(zip_index_stats(infos), limits)
        if problem is not None:
            return REASON_ZIP_BOMB, problem, limits.zip_limits()
        for info in _worksheets(infos):
            cap = min(info.file_size, limits.xlsx_max_part_mb * _MB)
            try:
                with archive.open(info) as part:
                    counts = count_sheet_cells(
                        part,
                        chunk_size=chunk_size,
                        max_bytes=cap,
                        max_cells=limits.xlsx_max_cells - cells,
                    )
            except (zipfile.BadZipFile, EOFError, zlib.error):
                return REASON_ZIP_BOMB, "index mismatch", limits.zip_limits()
            if counts.truncated:
                return REASON_ZIP_BOMB, "index mismatch", limits.zip_limits()
            cells += counts.cells
            grid += counts.grid_cells
            if counts.limit_exceeded or cells > limits.xlsx_max_cells:
                return (
                    REASON_TOO_COMPLEX,
                    f"more than {limits.xlsx_max_cells} cells",
                    limits.count_limits(),
                )
            if grid > limits.xlsx_max_grid_cells:
                return (
                    REASON_TOO_COMPLEX,
                    f"used ranges of {grid} cells (rows x columns), over "
                    f"{limits.xlsx_max_grid_cells}",
                    limits.count_limits(),
                )
    return None


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

# How much of an image the guard reads: a JPEG's frame header comes after its APPn
# segments (EXIF, ICC profile), each at most 64 KiB; a PNG's chunks before the
# first IDAT are small.
IMAGE_HEADER_BYTES = 1 << 20
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# JPEG start-of-frame markers: C0-CF except DHT (C4), JPG (C8) and DAC (CC).
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# JPEG markers with no length field: TEM, RST0-7 and SOI.
_JPEG_STANDALONE = frozenset({0x01, 0xD8, *range(0xD0, 0xD8)})


@dataclass(frozen=True)
class ImageHeader:
    """The size an image's header declares."""

    width: int
    height: int
    frames: int = 1

    @property
    def megapixels(self) -> float:
        return self.width * self.height * self.frames / 1_000_000


def _png_header(head: bytes) -> ImageHeader | None:
    """``IHDR``'s size and ``acTL``'s frame count (1 without one), or None."""
    if len(head) < 33 or head[12:16] != b"IHDR":
        return None
    width, height = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    frames = 1
    pos = 8
    while pos + 8 <= len(head):
        length = int.from_bytes(head[pos : pos + 4], "big")
        chunk = head[pos + 4 : pos + 8]
        if chunk == b"IDAT":
            break
        if chunk == b"acTL" and pos + 12 <= len(head):
            frames = max(1, int.from_bytes(head[pos + 8 : pos + 12], "big"))
            break
        pos += 12 + length
    return ImageHeader(width, height, frames)


def _jpeg_header(head: bytes) -> ImageHeader | None:
    """The size in the first start-of-frame segment, or None (none in ``head``)."""
    pos = 2
    while pos + 4 <= len(head):
        if head[pos] != 0xFF:
            return None
        marker = head[pos + 1]
        if marker == 0xFF:  # a fill byte before the marker
            pos += 1
            continue
        if marker in _JPEG_STANDALONE:
            pos += 2
            continue
        if marker in (0xD9, 0xDA):  # end of image, or scan data before any frame
            return None
        length = int.from_bytes(head[pos + 2 : pos + 4], "big")
        if marker in _JPEG_SOF:
            if pos + 9 > len(head):
                return None
            height = int.from_bytes(head[pos + 5 : pos + 7], "big")
            width = int.from_bytes(head[pos + 7 : pos + 9], "big")
            return ImageHeader(width, height)
        pos += 2 + length
    return None


def image_header(path: str | Path) -> ImageHeader | None:
    """The size a PNG or JPEG declares, read from at most ``IMAGE_HEADER_BYTES``.

    None for any other file, or a header that cannot be read.
    """
    with open(path, "rb") as f:
        return image_header_of(f.read(IMAGE_HEADER_BYTES))


def image_header_of(head: bytes) -> ImageHeader | None:
    """``image_header`` of the first bytes of a file (None: not found in ``head``)."""
    if head.startswith(_PNG_SIGNATURE):
        return _png_header(head)
    if head.startswith(b"\xff\xd8"):
        return _jpeg_header(head)
    return None


def check_image(path: str | Path, max_megapixels: int) -> GuardRejection | None:
    """The image cost guard: ``(reason_code, detail, limits)``, or None to parse.

    ``too_complex`` when the header declares more than ``max_megapixels`` (width x
    height x frames). Reads the header only; no pixel is decoded.
    """
    header = image_header(path)
    if header is None or header.megapixels <= max_megapixels:
        return None
    frames = f" x {header.frames} frames" if header.frames > 1 else ""
    return (
        REASON_TOO_COMPLEX,
        f"{header.width}x{header.height} pixels{frames}, {header.megapixels:.1f} megapixels "
        f"over the limit of {max_megapixels}",
        {"image_max_megapixels": max_megapixels},
    )
