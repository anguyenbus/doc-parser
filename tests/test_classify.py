"""
test_classify.py

Unit tests for _classify() — format classification from extension and MIME.

These are pure-function tests with no I/O; fixtures are fake paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from parser_service.parser_service import _classify

# ---------------------------------------------------------------------------
# Test 1: All supported extensions map to the correct classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extension, expected",
    [
        (".pdf", "pdf"),
        (".png", "image"),
        (".jpg", "image"),
        (".jpeg", "image"),
        (".tif", "image"),
        (".tiff", "image"),
        (".docx", "docx"),
        (".xlsx", "xlsx"),
        (".xlsm", "xlsx"),
        (".html", "html"),
        (".htm", "html"),
    ],
)
def test_classify_by_extension(extension: str, expected: str) -> None:
    """All supported extensions return the correct classification string."""
    fake_path = Path(f"document{extension}")
    result = _classify(fake_path, "")
    assert result == expected, f"Expected {expected!r} for {extension!r}, got {result!r}"


# ---------------------------------------------------------------------------
# Test 2: .webp extension → "image"
# ---------------------------------------------------------------------------


def test_classify_webp() -> None:
    """.webp extension classifies as 'image'."""
    assert _classify(Path("photo.webp"), "") == "image"


# ---------------------------------------------------------------------------
# Test 3: No extension but application/pdf MIME → "pdf"
# ---------------------------------------------------------------------------


def test_classify_by_mime_pdf_no_extension() -> None:
    """File with no extension but application/pdf MIME → 'pdf'."""
    result = _classify(Path("document_without_extension"), "application/pdf")
    assert result == "pdf"


# ---------------------------------------------------------------------------
# Test 4: Completely unknown extension + unknown MIME → "unknown"
# ---------------------------------------------------------------------------


def test_classify_unknown_extension_and_mime() -> None:
    """Unknown extension and unknown MIME returns 'unknown'."""
    result = _classify(Path("file.xyz123"), "application/octet-stream")
    assert result == "unknown"


# ---------------------------------------------------------------------------
# Test 5: "unknown" classification must not raise
# ---------------------------------------------------------------------------


def test_classify_unknown_does_not_raise() -> None:
    """_classify never raises — always returns a string."""
    result = _classify(Path("mystery.bin"), "application/binary")
    assert isinstance(result, str)
    assert result == "unknown"


# ---------------------------------------------------------------------------
# Additional: MIME-based classification for known types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mime, expected",
    [
        ("image/jpeg", "image"),
        ("image/png", "image"),
        ("image/tiff", "image"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
        ("text/html", "html"),
        ("application/xhtml+xml", "html"),
    ],
)
def test_classify_by_mime_fallback(mime: str, expected: str) -> None:
    """MIME-based fallback classifies correctly for known MIME types."""
    # Use an extension that doesn't match anything, so MIME fallback is used
    result = _classify(Path("file.unknown_ext"), mime)
    assert result == expected
