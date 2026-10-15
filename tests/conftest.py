"""
conftest.py

Shared pytest fixtures for the parser_service test suite.
"""

from __future__ import annotations

from pathlib import Path

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
