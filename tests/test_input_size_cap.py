"""
test_input_size_cap.py

Tests for issue #4 (PR1): raw-input size cap + streaming hash + the removal of
the discarded whole-file hash read on the markdown (production) path.

Layers:
  A. Pure helpers — _input_size_error, _max_input_bytes, _sha256_file.
  B. Guard integration — parse() and parse_to_markdown reject oversized input
     before Docling, and the markdown path never invokes the converter.
  C. Decoupling — parse_to_markdown no longer depends on _empty_output.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from parser_service import markdown_pipeline, parser_service
from parser_service.markdown_pipeline import parse_to_markdown
from parser_service.parser_service import (
    _input_size_error,
    _max_input_bytes,
    _sha256_file,
    parse,
)

FIXTURES = Path(__file__).parent / "fixtures"
DIGITAL_PDF = FIXTURES / "digital_simple.pdf"


# ---------------------------------------------------------------------------
# A. Pure helpers
# ---------------------------------------------------------------------------


def test_max_input_bytes_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PARSER_MAX_INPUT_MB", "50")
    assert _max_input_bytes() == 50_000_000
    monkeypatch.delenv("PARSER_MAX_INPUT_MB", raising=False)
    assert _max_input_bytes() == 200_000_000  # default


def test_input_size_error_under_cap(tmp_path: Path) -> None:
    f = tmp_path / "small.pdf"
    f.write_bytes(b"x" * 1000)
    assert _input_size_error(f) is None


def test_input_size_error_over_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PARSER_MAX_INPUT_MB", "0.001")  # 1000-byte cap
    f = tmp_path / "big.pdf"
    f.write_bytes(b"x" * 5000)
    msg = _input_size_error(f)
    assert msg is not None
    assert "exceeds" in msg


def test_input_size_error_disabled_when_cap_nonpositive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PARSER_MAX_INPUT_MB", "0")
    f = tmp_path / "big.pdf"
    f.write_bytes(b"x" * 5000)
    assert _input_size_error(f) is None


def test_input_size_error_missing_file_returns_none(tmp_path: Path) -> None:
    # Cannot stat → return None and let the normal parse path surface the failure.
    assert _input_size_error(tmp_path / "does_not_exist.pdf") is None


def test_sha256_file_matches_oneshot(tmp_path: Path) -> None:
    """Streaming hash equals a one-shot hash for a multi-chunk (>1 MiB) file."""
    data = (b"abcdefgh" * 200_003)  # ~1.6 MiB, not a chunk multiple
    f = tmp_path / "blob.bin"
    f.write_bytes(data)
    assert _sha256_file(f) == hashlib.sha256(data).hexdigest()


def test_sha256_file_matches_on_fixture() -> None:
    assert _sha256_file(DIGITAL_PDF) == hashlib.sha256(DIGITAL_PDF.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# B. Guard integration — reject before Docling
# ---------------------------------------------------------------------------


def test_parse_to_markdown_rejects_oversized_before_converter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oversized input → empty markdown + input_too_large warning, converter never built."""
    monkeypatch.setenv("PARSER_MAX_INPUT_MB", "0.0001")  # 100-byte cap; fixture exceeds

    def _boom() -> object:
        raise AssertionError("DocumentConverter must not be constructed for oversized input")

    monkeypatch.setattr(markdown_pipeline, "_document_converter", _boom)

    result = parse_to_markdown(DIGITAL_PDF)
    assert result["markdown"] == ""
    assert result["page_routes"] == []
    codes = [w["code"] for w in result["warnings"]]
    assert codes == ["input_too_large"]


def test_parse_rejects_oversized_before_docling(monkeypatch: pytest.MonkeyPatch) -> None:
    """parse() returns an empty, schema-valid skeleton with an input_too_large warning."""
    monkeypatch.setenv("PARSER_MAX_INPUT_MB", "0.0001")
    out = parse(DIGITAL_PDF)
    assert out["elements"] == []
    assert out["pages"] == []
    codes = [w["code"] for w in out["warnings"]]
    assert "input_too_large" in codes
    # Skeleton still carries a real content hash (64 hex chars).
    assert len(out["source"]["sha256"]) == 64


def test_default_cap_allows_normal_fixture() -> None:
    """At the 200 MB default the small fixture parses (no input_too_large)."""
    os.environ.pop("PARSER_MAX_INPUT_MB", None)
    result = parse_to_markdown(DIGITAL_PDF)
    codes = [w["code"] for w in result["warnings"]]
    assert "input_too_large" not in codes
    assert len(result["markdown"]) > 0


# ---------------------------------------------------------------------------
# C. Decoupling — markdown path no longer reads the file to hash-and-discard
# ---------------------------------------------------------------------------


def test_parse_to_markdown_independent_of_empty_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """parse_to_markdown must succeed even if _empty_output would raise.

    Proves the production path no longer calls _empty_output (which previously
    read the whole file just to compute a discarded sha256).
    """

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("_empty_output must not be called on the markdown path")

    monkeypatch.setattr(parser_service, "_empty_output", _boom)
    monkeypatch.setattr(markdown_pipeline, "_empty_output", _boom)

    result = parse_to_markdown(DIGITAL_PDF)
    assert len(result["markdown"]) > 0
