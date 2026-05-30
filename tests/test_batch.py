"""
test_batch.py

Unit tests for the I/O abstraction layer (io_layer.py).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from parser_service.io_layer import InputRef, LocalIO


# ---------------------------------------------------------------------------
# Test 1: LocalIO.list_input_files — yields only supported files
# ---------------------------------------------------------------------------

def test_local_io_list_input_files_filters_by_extension(tmp_path: Path) -> None:
    """list_input_files yields only supported extensions, not .txt files."""
    # Create test files
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "notes.txt").write_text("plain text")
    (tmp_path / "photo.png").write_bytes(b"\x89PNG")

    io = LocalIO()
    refs = list(io.list_input_files(str(tmp_path)))

    filenames = {ref.filename for ref in refs}
    assert "report.pdf" in filenames
    assert "photo.png" in filenames
    assert "notes.txt" not in filenames  # .txt is not a supported extension

    # All refs must have kind="local"
    for ref in refs:
        assert ref.kind == "local"


# ---------------------------------------------------------------------------
# Test 2: LocalIO.read_bytes — returns exact file bytes
# ---------------------------------------------------------------------------

def test_local_io_read_bytes_returns_file_bytes(tmp_path: Path) -> None:
    """read_bytes returns the exact bytes of the file."""
    content = b"PDF content bytes \x00\x01\x02"
    test_file = tmp_path / "test.pdf"
    test_file.write_bytes(content)

    io = LocalIO()
    ref = InputRef(uri=str(test_file), filename="test.pdf", kind="local")
    result = io.read_bytes(ref)

    assert result == content


# ---------------------------------------------------------------------------
# Test 3: LocalIO.write_json — writes correct JSON file
# ---------------------------------------------------------------------------

def test_local_io_write_json_creates_json_file(tmp_path: Path) -> None:
    """write_json writes a .json file at output_dir/<stem>.json."""
    data: dict[str, Any] = {
        "schema_version": "1.0.0",
        "elements": [],
        "warnings": [],
    }
    ref = InputRef(uri="/some/path/report.pdf", filename="report.pdf", kind="local")
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    io = LocalIO()
    io.write_json(ref, str(out_dir), data)

    expected_path = out_dir / "report.json"
    assert expected_path.exists()
    written = json.loads(expected_path.read_text(encoding="utf-8"))
    assert written["schema_version"] == "1.0.0"
    assert written["elements"] == []


# ---------------------------------------------------------------------------
# Additional: InputRef dataclass has correct fields
# ---------------------------------------------------------------------------

def test_input_ref_fields() -> None:
    """InputRef dataclass stores uri, filename, and kind correctly."""
    ref = InputRef(uri="/path/to/file.pdf", filename="file.pdf", kind="local")
    assert ref.uri == "/path/to/file.pdf"
    assert ref.filename == "file.pdf"
    assert ref.kind == "local"


# ---------------------------------------------------------------------------
# Additional: module-level list_input_files works with local URI
# ---------------------------------------------------------------------------

def test_module_level_list_input_files(tmp_path: Path) -> None:
    """Module-level list_input_files function works for local URIs."""
    from parser_service.io_layer import list_input_files

    (tmp_path / "doc.docx").write_bytes(b"PK\x03\x04fake")
    (tmp_path / "ignored.csv").write_text("a,b,c")

    refs = list(list_input_files(str(tmp_path)))
    filenames = [r.filename for r in refs]
    assert "doc.docx" in filenames
    assert "ignored.csv" not in filenames
