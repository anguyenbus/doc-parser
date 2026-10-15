"""
test_guards.py — ``parser_service.guards``: DOCX index numbers, element counts and the
DOCX cost guard (spec D7), run by the page pipeline before the parser (spec D4 step 4).

Fixtures are built at test time with python-docx and ``zipfile`` in ``tmp_path``; the
bombs are session-scoped. Rejection paths are timed against the spec's 1.0 s.

Run ONLY these tests:
    uv run pytest tests/test_guards.py
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import struct
import subprocess
import sys
import time
import warnings
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import docx
import pytest

from parser_service import page_pipeline, settings, unit_handlers
from parser_service.guards import (
    ELEMENT_START,
    DocxLimits,
    check_docx,
    count_elements,
    docx_stats,
)
from parser_service.page_worker import Reply, Request
from parser_service.store import LocalStore

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_ROWS, _COLS, _PARAGRAPHS = 4, 3, 7


@pytest.fixture
def docx_path(tmp_path: Path) -> Path:
    """Seven body paragraphs (two with paragraph properties) and a 4 x 3 table."""
    document = docx.Document()
    document.add_heading("Title", level=1)
    for index in range(_PARAGRAPHS - 2):
        document.add_paragraph(f"Paragraph {index}")
    document.add_paragraph("Last", style="List Bullet")
    table = document.add_table(rows=_ROWS, cols=_COLS)
    for row in table.rows:
        for cell in row.cells:
            cell.text = "x"
    path = tmp_path / "counts.docx"
    document.save(str(path))
    return path


def test_docx_stats_counts(docx_path: Path) -> None:
    with zipfile.ZipFile(docx_path) as archive:
        infos = archive.infolist()
        xml = archive.read("word/document.xml")
    tree = ElementTree.fromstring(xml)
    expected_paragraphs = len(list(tree.iter(f"{_W}p")))
    expected_cells = len(list(tree.iter(f"{_W}tc")))
    # The oracle agrees with what the fixture built: every cell holds one paragraph.
    assert expected_cells == _ROWS * _COLS
    assert expected_paragraphs >= _PARAGRAPHS + _ROWS * _COLS
    assert b"<w:pPr>" in xml and b"<w:tcPr>" in xml

    stats = docx_stats(docx_path)

    assert stats.paragraphs == expected_paragraphs
    assert stats.table_cells == expected_cells
    assert stats.document_xml_bytes == len(xml)
    expanded = sum(info.file_size for info in infos)
    compressed = sum(info.compress_size for info in infos)
    assert stats.index.entries == len(infos)
    assert stats.index.expanded_bytes == expanded
    assert stats.index.compressed_bytes == compressed
    assert stats.index.largest_part_bytes == max(info.file_size for info in infos)
    assert stats.index.ratio == pytest.approx(expanded / compressed)
    assert stats.index.max_large_part_ratio == 0.0  # no part of 1 MiB or more
    assert stats.index.duplicate_names == 0
    assert stats.index.encrypted_entries == 0

    # The same counts come out of tiny chunks, and a byte cap stops the pass early.
    with zipfile.ZipFile(docx_path) as archive, archive.open("word/document.xml") as part:
        tiny = count_elements(part, chunk_size=7)
    assert (tiny.paragraphs, tiny.table_cells) == (expected_paragraphs, expected_cells)
    assert (tiny.bytes_read, tiny.truncated, tiny.limit_exceeded) == (len(xml), False, False)
    with zipfile.ZipFile(docx_path) as archive, archive.open("word/document.xml") as part:
        capped = count_elements(part, max_bytes=100)
    assert capped.bytes_read == 100 and capped.truncated

    # A ZIP with no word/document.xml has index numbers but no counts.
    other = docx_path.with_name("other.zip")
    with zipfile.ZipFile(other, "w") as archive:
        archive.writestr("a.txt", "hello")
    no_main = docx_stats(other)
    assert (no_main.paragraphs, no_main.table_cells, no_main.index.entries) == (None, None, 1)


# ---------------------------------------------------------------------------
# The DOCX cost guard (Group 5a)
# ---------------------------------------------------------------------------

# The spec's provisional ConfigMap values (Contracts table).
LIMITS = DocxLimits(
    docx_max_expanded_mb=300,
    docx_max_part_mb=200,
    docx_max_ratio=100,
    docx_max_entries=10_000,
    docx_max_paragraphs=50_000,
    docx_max_table_cells=100_000,
)
_REJECT_BUDGET_S = 1.0  # spec: a rejection takes under 1.0 s of wall time
_PARA = b"<w:p><w:r><w:t>%s</w:t></w:r></w:p>"


def _timed_check(path: Path, limits: DocxLimits = LIMITS) -> tuple[Any, float]:
    started = time.perf_counter()
    result = check_docx(path, limits)
    return result, time.perf_counter() - started


def _blank_docx() -> dict[str, bytes]:
    """The parts of an empty python-docx document, by name."""
    buffer = io.BytesIO()
    docx.Document().save(buffer)
    with zipfile.ZipFile(buffer) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def _docx_with_body(path: Path, body: bytes, *, compresslevel: int = 6) -> Path:
    """A python-docx document whose ``word/document.xml`` body is ``body``."""
    parts = _blank_docx()
    xml = parts["word/document.xml"]
    head = xml[: xml.index(b"<w:body>") + len(b"<w:body>")]
    parts["word/document.xml"] = head + body + b"</w:body></w:document>"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=compresslevel) as out:
        for name, data in parts.items():
            out.writestr(name, data)
    return path


def _patch_index(path: Path, name: str, *, file_size: int | None = None, flags: int = 0) -> None:
    """Rewrite what the ZIP index says about ``name``: its expanded size (local header
    and central directory) and extra flag bits (central directory), bytes kept."""
    raw = bytearray(path.read_bytes())
    info = zipfile.ZipFile(path).getinfo(name)
    if file_size is not None:
        raw[info.header_offset + 22 : info.header_offset + 26] = struct.pack("<I", file_size)
    at = raw.find(b"PK\x01\x02")
    while at != -1:
        name_len = struct.unpack_from("<H", raw, at + 28)[0]
        if raw[at + 46 : at + 46 + name_len] == name.encode():
            if file_size is not None:
                raw[at + 24 : at + 28] = struct.pack("<I", file_size)
            struct.pack_into("<H", raw, at + 8, struct.unpack_from("<H", raw, at + 8)[0] | flags)
        at = raw.find(b"PK\x01\x02", at + 46)
    path.write_bytes(bytes(raw))


@pytest.fixture(scope="session")
def bombs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The bomb fixtures and one normal DOCX, built once per session."""
    root = tmp_path_factory.mktemp("bombs")
    made: dict[str, Path] = {}

    # 20,000 entries: a normal document plus empty parts.
    made["entries"] = path = _docx_with_body(root / "entries.docx", _PARA % b"x")
    with zipfile.ZipFile(path, "a") as archive:
        for index in range(20_000):
            archive.writestr(f"word/pad/part{index}.xml", b"")

    # A 400 MB part at about 1000:1 (zeros at deflate level 9): 0.4 MB on disk.
    made["part"] = path = _docx_with_body(root / "part.docx", _PARA % b"x")
    zero = bytes(1 << 20)
    with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        with archive.open("word/media/image1.bin", "w", force_zip64=True) as part:
            for _ in range(400):
                part.write(zero)

    # The 20 MB layout of planning/evidence/bomb_test.py: one repeated plain
    # paragraph, deflate level 9 (about 175:1 in the evidence run).
    para = _PARA % (b"records retention backlog " * 4)
    made["bomb_test"] = _docx_with_body(
        root / "bomb_test.docx", para * (20_000_000 // len(para)), compresslevel=9
    )

    # The same element count at a sane ratio: short paragraphs of random hex text.
    text = os.urandom(10_000_000).hex().encode()
    body = b"".join(_PARA % text[i : i + 16] for i in range(0, 20_000_000 // 48 * 16, 16))
    made["elements"] = _docx_with_body(root / "elements.docx", body, compresslevel=1)

    # A normal document: headings, paragraphs and a table.
    document = docx.Document()
    document.add_heading("Report", level=1)
    for index in range(200):
        document.add_paragraph(f"Paragraph {index} of the report.")
    table = document.add_table(rows=20, cols=5)
    for row in table.rows:
        for cell in row.cells:
            cell.text = "v"
    made["normal"] = root / "normal.docx"
    document.save(str(made["normal"]))
    return made


def test_zip_index_bomb_is_rejected_under_one_second(bombs: dict[str, Path]) -> None:
    """20,000 entries; a 400 MB part; 1000:1. Each ZIP check fires on the index alone,
    in D7 order (raising the earlier limits shows the next one)."""
    with zipfile.ZipFile(bombs["part"]) as archive:
        info = archive.getinfo("word/media/image1.bin")
    assert info.file_size == 400 * (1 << 20) and info.file_size / info.compress_size > 900
    entries = len(zipfile.ZipFile(bombs["entries"]).infolist())
    assert entries > 20_000
    cases = [
        ("entries", LIMITS, f"{entries} entries, over 10000"),
        ("part", LIMITS, "expanded 4"),
        ("part", replace(LIMITS, docx_max_expanded_mb=1000), "a part of 419.4 MB, over 200 MB"),
        (
            "part",
            replace(LIMITS, docx_max_expanded_mb=1000, docx_max_part_mb=1000),
            "ratio 9",  # the overall ratio (about 950:1 with the other parts)
        ),
    ]
    for name, limits, detail in cases:
        result, seconds = _timed_check(bombs[name], limits)
        assert result is not None
        code, text, stored = result
        assert (code, stored) == ("zip_bomb", limits.zip_limits()), (name, text)
        assert text.startswith(detail), text
        assert seconds < _REJECT_BUDGET_S, (name, seconds)

    # The per-part ratio check alone: a 2 MiB part of zeros beside incompressible bytes.
    big = bombs["part"].with_name("one_part.docx")
    _docx_with_body(big, _PARA % b"x")
    with zipfile.ZipFile(big, "a", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/media/noise.bin", os.urandom(200_000))
        archive.writestr("word/media/zeros.bin", bytes(2 << 20))
    result, seconds = _timed_check(big)
    assert result is not None and result[0] == "zip_bomb"
    assert result[1].startswith("a part of 1 MiB or more at ratio"), result[1]
    assert seconds < _REJECT_BUDGET_S


def test_lying_index_is_rejected_as_zip_bomb(bombs: dict[str, Path], tmp_path: Path) -> None:
    """A declared size smaller than the stream passes the index, but the byte-capped
    pass ends on a bad CRC: zip_bomb, detail ``index mismatch``."""
    path = _docx_with_body(tmp_path / "lie.docx", _PARA % b"y" * 100_000)
    _patch_index(path, "word/document.xml", file_size=1_000)
    assert zipfile.ZipFile(path).getinfo("word/document.xml").file_size == 1_000

    result, seconds = _timed_check(path)

    assert result == ("zip_bomb", "index mismatch", LIMITS.zip_limits())
    assert seconds < _REJECT_BUDGET_S


def test_high_element_count_is_rejected_under_one_second(bombs: dict[str, Path]) -> None:
    """20 MB of paragraphs: too_complex from the streamed count, within the first MB."""
    # At a sane ratio the index passes, and the count stops the pass early.
    with zipfile.ZipFile(bombs["elements"]) as archive:
        info = archive.getinfo("word/document.xml")
    assert (
        info.file_size > 19_000_000 and info.file_size / info.compress_size < LIMITS.docx_max_ratio
    )
    result, seconds = _timed_check(bombs["elements"])
    assert result is not None
    code, detail, stored = result
    assert (code, stored) == ("too_complex", LIMITS.count_limits())
    match = re.fullmatch(
        r"(\d+) paragraphs and 0 table cells in the first ([\d.]+) MB \(limits 50000 and 100000\)",
        detail,
    )
    assert match is not None, detail
    # The pass stops at the end of the 1 MiB chunk in which the limit was passed.
    assert int(match.group(1)) > 50_000 and float(match.group(2)) < 5
    assert seconds < _REJECT_BUDGET_S

    # The evidence layout (663 s in Docling): its ratio is caught first; with the
    # ratio raised (as for the live test run) the count catches it.
    result, seconds = _timed_check(bombs["bomb_test"])
    assert result is not None and result[0] == "zip_bomb" and result[1].startswith("ratio ")
    assert seconds < _REJECT_BUDGET_S
    result, seconds = _timed_check(bombs["bomb_test"], replace(LIMITS, docx_max_ratio=1000))
    assert result is not None and result[0] == "too_complex"
    assert seconds < _REJECT_BUDGET_S

    # Table cells have their own limit.
    cells = bombs["elements"].with_name("cells.docx")
    _docx_with_body(cells, b"<w:tbl><w:tr>" + b"<w:tc><w:p/></w:tc>" * 1_001 + b"</w:tr></w:tbl>")
    result = check_docx(cells, replace(LIMITS, docx_max_table_cells=1_000))
    assert result is not None and result[0] == "too_complex"
    assert result[1].startswith("1001 paragraphs and 1001 table cells"), result[1]


def test_count_survives_chunk_boundaries() -> None:
    """Every chunk size, 7 bytes included, gives the whole-buffer count: a match split
    across chunks is counted once, none twice (the longest match is 37 bytes)."""
    prefix = "q" * 32
    xml = (
        b"<w:document><w:body>"
        + b"<w:p><w:r><w:t>a</w:t></w:r></w:p>" * 9
        + f"<{prefix}:p/><{prefix}:tc>x</{prefix}:tc>".encode()
        + b"<w:tbl><w:tr><w:tc><w:p/></w:tc><w:tc><w:p/></w:tc></w:tr></w:tbl>"
        + b"<w:p\n w:rsidR='1'><w:pPr/></w:p><p>" * 5
        + b"</w:body></w:document>"
    )
    expected_p = sum(1 for m in ELEMENT_START.finditer(xml) if m.group(1) == b"p")
    expected_tc = sum(1 for m in ELEMENT_START.finditer(xml) if m.group(1) == b"tc")
    assert (expected_p, expected_tc) == (9 + 1 + 2 + 10, 1 + 2)
    for chunk_size in [7, *range(1, 41)]:
        counts = count_elements(io.BytesIO(xml), chunk_size=chunk_size)
        assert (counts.paragraphs, counts.table_cells) == (expected_p, expected_tc), chunk_size
        assert (counts.bytes_read, counts.limit_exceeded, counts.truncated) == (
            len(xml),
            False,
            False,
        )
    # The early stop still sees every match up to the limit.
    stopped = count_elements(io.BytesIO(xml), chunk_size=7, max_paragraphs=4)
    assert stopped.limit_exceeded and 4 < stopped.paragraphs <= expected_p


def test_count_ignores_pPr_and_tcPr_and_any_prefix() -> None:
    """``<w:pPr>``, ``<w:pict>``, ``<w:tcPr>`` and closing tags do not match; any prefix
    (a renamed one, DrawingML ``<a:p>``, none) does."""
    no = [
        b"<w:pPr>",
        b"<w:pict>",
        b"<w:tcPr>",
        b"<w:tcW/>",
        b"<w:proofErr/>",
        b"</w:p>",
        b"</w:tc>",
    ]
    yes_p = [b"<w:p>", b"<p>", b"<a:p>", b"<ns0:p>", b"<x.y-z:p/>", b"<w:p\tw:rsid='1'>"]
    yes_tc = [b"<w:tc>", b"<tc/>", b"<ns0:tc >"]
    for tag in no:
        counts = count_elements(io.BytesIO(tag))
        assert (counts.paragraphs, counts.table_cells) == (0, 0), tag
    for tag in yes_p:
        assert count_elements(io.BytesIO(tag)).paragraphs == 1, tag
    for tag in yes_tc:
        assert count_elements(io.BytesIO(tag)).table_cells == 1, tag


class _StubRunner:
    """A parse worker that records its requests and replies ok."""

    def __init__(self) -> None:
        self.requests: list[Request] = []

    @property
    def pid(self) -> int | None:
        return 4242

    def run(self, request: Request, deadline_s: float) -> Reply:
        self.requests.append(request)
        return "ok", None, None, "# parsed", {"route": "docling-kept"}


def _store_unit(store: LocalStore, source: Path) -> str:
    work_id = page_pipeline.work_id_for(source, "docx")
    page_pipeline._intake(store, work_id, source, "docx")
    return work_id


def test_guard_rejection_never_starts_docling(
    bombs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Through ``_process_page`` (D4 step 4): a bomb is one final ``failed`` record and
    never reaches the parser, on the worker path or the local executor; a normal DOCX
    is parsed; the guard itself never imports Docling."""
    # Group 5b's handler and deadline are not in this image yet: stand-ins.
    monkeypatch.setitem(settings.DEADLINE_ENV, "docx", ("TEST_DEADLINE_DOCX_S", 60))
    handled: list[Path] = []

    def stub_handler(path: Path, page_number: int, escalate: bool) -> Reply:
        handled.append(path)
        return "ok", None, None, "# parsed", {"route": "docling-kept"}

    monkeypatch.setitem(unit_handlers.HANDLERS, "docx", stub_handler)
    store = LocalStore(tmp_path / "store")
    runner = _StubRunner()

    # Worker path: the index bomb is refused in this process, after the attempt.
    caplog.set_level(logging.INFO, logger="parser_service.events")
    work_id = _store_unit(store, bombs["part"])
    started = time.perf_counter()
    record = page_pipeline._process_page(store, work_id, 1, False, 3, runner=runner, kind="docx")
    seconds = time.perf_counter() - started
    assert runner.requests == [] and seconds < _REJECT_BUDGET_S
    assert (record["state"], record["reason_code"], record["attempt"]) == ("failed", "zip_bomb", 1)
    assert record["reason"].startswith("zip_bomb: expanded 4")
    assert record["limits"] == LIMITS.zip_limits()
    assert (record["parse_worker_pid"], record["markdown"], record["kind"]) == (None, "", "docx")
    assert page_pipeline._read_attempts(store, work_id, 1) == 1
    events = [
        json.loads(r.getMessage()) for r in caplog.records if r.name == "parser_service.events"
    ]
    assert any(e["event"] == "guard_reject" and e["reason_code"] == "zip_bomb" for e in events)

    # Final under unchanged limits (D5); a raised limit releases it.
    limits_now = page_pipeline.current_limits("docx")
    assert {
        **LIMITS.zip_limits(),
        **LIMITS.count_limits(),
        "deadline_s": 60,
    }.items() <= limits_now.items()
    assert page_pipeline._is_final(record, limits_now)
    monkeypatch.setenv("PARSER_DOCX_MAX_EXPANDED_MB", "500")
    assert not page_pipeline._is_final(record, page_pipeline.current_limits("docx"))
    monkeypatch.delenv("PARSER_DOCX_MAX_EXPANDED_MB")

    # Local executor: too_complex, and the stub handler is never called.
    work_id = _store_unit(store, bombs["elements"])
    record = page_pipeline._process_page(store, work_id, 1, False, 3, kind="docx")
    assert (record["reason_code"], record["limits"], handled) == (
        "too_complex",
        LIMITS.count_limits(),
        [],
    )
    assert page_pipeline._is_final(record, page_pipeline.current_limits("docx"))
    monkeypatch.setenv("PARSER_DOCX_MAX_PARAGRAPHS", "60000")
    assert not page_pipeline._is_final(record, page_pipeline.current_limits("docx"))
    monkeypatch.delenv("PARSER_DOCX_MAX_PARAGRAPHS")

    # A normal DOCX passes the guard and is parsed, on both paths.
    assert check_docx(bombs["normal"], LIMITS) is None
    work_id = _store_unit(store, bombs["normal"])
    record = page_pipeline._process_page(store, work_id, 1, False, 3, runner=runner, kind="docx")
    assert (record["state"], record["markdown"], record["parse_worker_pid"]) == (
        "ok",
        "# parsed",
        4242,
    )
    assert [request[0] for request in runner.requests] == ["docx"]
    local_store = LocalStore(tmp_path / "local")
    work_id = _store_unit(local_store, bombs["normal"])
    record = page_pipeline._process_page(local_store, work_id, 1, False, 3, kind="docx")
    assert record["state"] == "ok" and len(handled) == 1

    # The guard alone, in a fresh interpreter: no Docling (or torch) is imported.
    script = (
        "import sys\n"
        "from parser_service.guards import check_docx, DocxLimits\n"
        "limits = DocxLimits(300, 200, 100, 10000, 50000, 100000)\n"
        "assert check_docx(sys.argv[1], limits)[0] == 'zip_bomb'\n"
        "assert check_docx(sys.argv[2], limits) is None\n"
        "loaded = [m for m in ('docling', 'torch') if m in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script, str(bombs["entries"]), str(bombs["normal"])],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr[-2000:]


def test_duplicate_or_encrypted_entry_is_a_zip_bomb(tmp_path: Path) -> None:
    """A duplicate entry name or an encrypted entry is refused from the index alone."""
    duplicate = _docx_with_body(tmp_path / "dup.docx", _PARA % b"x")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # zipfile warns on the duplicate
        with zipfile.ZipFile(duplicate, "a") as archive:
            archive.writestr("word/document.xml", b"<w:p/>")
    assert check_docx(duplicate, LIMITS) == (
        "zip_bomb",
        "1 duplicate entry name(s)",
        LIMITS.zip_limits(),
    )

    encrypted = _docx_with_body(tmp_path / "enc.docx", _PARA % b"x")
    _patch_index(encrypted, "word/styles.xml", flags=0x1)
    assert zipfile.ZipFile(encrypted).getinfo("word/styles.xml").flag_bits & 0x1
    assert check_docx(encrypted, LIMITS) == (
        "zip_bomb",
        "1 encrypted entry(ies)",
        LIMITS.zip_limits(),
    )

    # Neither is a ZIP problem in a normal file; a file that is not a ZIP at all
    # passes the guard (nothing to inflate) and is left to the handler.
    assert check_docx(_docx_with_body(tmp_path / "ok.docx", _PARA % b"x"), LIMITS) is None
    not_zip = tmp_path / "not.docx"
    not_zip.write_bytes(b"PK\x03\x04 truncated")
    assert check_docx(not_zip, LIMITS) is None
