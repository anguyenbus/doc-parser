"""
conftest.py

Shared pytest fixtures for the parser_service test suite.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Path fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def digital_pdf_path() -> Path:
    return FIXTURES_DIR / "digital_simple.pdf"


@pytest.fixture
def scanned_pdf_path() -> Path:
    return FIXTURES_DIR / "scanned.pdf"


# ---------------------------------------------------------------------------
# VLM mock fixtures (monkeypatch call_vlm in parser_service.parser_service)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_vlm_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch call_vlm to return a valid 2x3 table dict."""
    table_response: dict[str, Any] = {
        "rows": 2,
        "cols": 3,
        "header_rows": 1,
        "cells": [
            {"row": 0, "col": 0, "text": "Header A", "row_span": 1, "col_span": 1},
            {"row": 0, "col": 1, "text": "Header B", "row_span": 1, "col_span": 1},
            {"row": 0, "col": 2, "text": "Header C", "row_span": 1, "col_span": 1},
            {"row": 1, "col": 0, "text": "Value 1", "row_span": 1, "col_span": 1},
            {"row": 1, "col": 1, "text": "Value 2", "row_span": 1, "col_span": 1},
            {"row": 1, "col": 2, "text": "Value 3", "row_span": 1, "col_span": 1},
        ],
    }

    def _mock_call_vlm(image_bytes: bytes, mode: str) -> dict[str, Any]:
        return table_response

    monkeypatch.setattr(
        "parser_service.parser_service.call_vlm", _mock_call_vlm
    )


@pytest.fixture
def mock_vlm_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch call_vlm to return a valid page elements dict."""
    page_response: dict[str, Any] = {
        "elements": [
            {"type": "heading", "text": "Sample Heading", "level": 1},
            {"type": "paragraph", "text": "Hello world"},
        ]
    }

    def _mock_call_vlm(image_bytes: bytes, mode: str) -> dict[str, Any]:
        return page_response

    monkeypatch.setattr(
        "parser_service.parser_service.call_vlm", _mock_call_vlm
    )


@pytest.fixture
def mock_vlm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch call_vlm to return an error dict."""
    def _mock_call_vlm(image_bytes: bytes, mode: str) -> dict[str, Any]:
        return {"error": "mocked_failure"}

    monkeypatch.setattr(
        "parser_service.parser_service.call_vlm", _mock_call_vlm
    )
