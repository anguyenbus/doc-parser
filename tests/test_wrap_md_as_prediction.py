"""
test_wrap_md_as_prediction.py — Task Group 3 tests for ``wrap_md_as_prediction``.

These are the 2-4 focused tests written first (task 3.1). ``wrap_md_as_prediction``
is the ONLY place JSON is produced on the markdown path: it wraps a markdown string
in a single ``paragraph`` element inside a schema-``1.0.0``-valid prediction so the
doc-bench wheel can grade it.

Schema source-of-truth note (important):
  doc-bench's ``validate()`` takes ``schema_path`` as an argument, so the schema
  actually enforced at grade time is the WHEEL'S BUNDLED copy
  (``doc_bench/fixtures/parser_output.schema.json``), NOT the repo ``references/``
  copy. These tests therefore self-validate the wrapped output against the wheel's
  bundled schema, resolved robustly (import ``doc_bench`` if available, else locate
  the uv-tool installation).

  The wheel schema was diffed against
  ``references/doc-bench/contracts/parser_output.schema.json`` (both ``1.0.0``):
  they are BYTE-IDENTICAL (same sha256). Recorded in
  ``planning/schema-diff.md``.

Run ONLY these tests (task 3.3):
    uv run pytest tests/test_wrap_md_as_prediction.py
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

Validator = Callable[[dict[str, Any]], None]

FIXTURES = Path(__file__).parent / "fixtures"
DIGITAL = FIXTURES / "digital_simple.pdf"


# ---------------------------------------------------------------------------
# Resolve the WHEEL'S bundled schema (the one doc-bench actually grades against).
# ---------------------------------------------------------------------------


def _wheel_schema_path() -> Path:
    """Return the path to the wheel's bundled parser_output.schema.json.

    Prefer importing ``doc_bench`` (in-process resolution). If doc-bench is not
    importable in this venv, fall back to the uv-tool installation. Skip the
    suite with a clear marker if neither is available.
    """
    try:
        import doc_bench

        return Path(doc_bench.__file__).parent / "fixtures" / "parser_output.schema.json"
    except Exception:  # noqa: BLE001 — doc_bench not installed in this venv
        pass

    # Fall back to the uv-tool installation of the doc-bench wheel.
    candidates = [
        Path(
            os.path.expanduser(
                "~/.local/share/uv/tools/doc-bench/lib/python3.13/site-packages/"
                "doc_bench/fixtures/parser_output.schema.json"
            )
        ),
    ]
    tool_root = Path(os.path.expanduser("~/.local/share/uv/tools/doc-bench"))
    if tool_root.exists():
        candidates.extend(tool_root.rglob("doc_bench/fixtures/parser_output.schema.json"))

    for c in candidates:
        if c.exists():
            return c

    pytest.skip("doc-bench wheel not installed; cannot locate bundled schema")


@pytest.fixture(scope="module")
def wheel_schema() -> dict[str, Any]:
    path = _wheel_schema_path()
    schema: dict[str, Any] = json.loads(path.read_text())
    return schema


@pytest.fixture
def validate_against_wheel(wheel_schema: dict[str, Any]) -> Validator:
    """Return a callable that validates a prediction against the WHEEL schema.

    Uses ``jsonschema`` (a doc-bench dependency, also present in this venv).
    """
    jsonschema = pytest.importorskip("jsonschema")

    def _validate(prediction: dict[str, Any]) -> None:
        jsonschema.validate(instance=prediction, schema=wheel_schema)

    return _validate


# ---------------------------------------------------------------------------
# Test 1: wrapped output validates against the WHEEL schema; one paragraph.
# ---------------------------------------------------------------------------


def test_wrap_validates_against_wheel_schema_one_paragraph(
    validate_against_wheel: Validator,
) -> None:
    """The wrap conforms to the wheel schema with exactly one paragraph element."""
    from parser_service.markdown_pipeline import wrap_md_as_prediction

    md = "# Title\n\nA paragraph of body text.\n\n- a\n- b"
    pred = wrap_md_as_prediction(md, DIGITAL)

    # Self-validate against the schema the grader actually enforces.
    validate_against_wheel(pred)

    assert pred["schema_version"] == "1.0.0"

    elements = pred["elements"]
    assert len(elements) == 1
    elem = elements[0]
    assert elem["type"] == "paragraph"
    assert elem["text"] == md
    assert elem["page_index"] == 0
    assert elem["char_span"] == [0, len(md)]
    assert elem["content"] == {"kind": "text"}


# ---------------------------------------------------------------------------
# Test 2: full four-field source, matching _empty_output for the same file.
# ---------------------------------------------------------------------------


def test_wrap_source_matches_empty_output(validate_against_wheel: Validator) -> None:
    """Source has all four fields and matches what _empty_output produces."""
    import mimetypes

    from parser_service.markdown_pipeline import wrap_md_as_prediction
    from parser_service.parser_service import _empty_output

    md = "hello world"
    pred = wrap_md_as_prediction(md, DIGITAL)
    validate_against_wheel(pred)

    mime = mimetypes.guess_type(str(DIGITAL))[0] or ""
    expected = _empty_output(DIGITAL.resolve(), mime)["source"]

    source = pred["source"]
    assert set(source.keys()) == {"doc_id", "filename", "mime_type", "sha256"}
    assert source == expected


# ---------------------------------------------------------------------------
# Test 3: 64-hex sha256 and additionalProperties: false satisfied.
# ---------------------------------------------------------------------------


def test_wrap_sha256_is_64_hex_and_no_extra_props(validate_against_wheel: Validator) -> None:
    """sha256 is 64 lowercase hex chars; no extra top-level/source keys leak."""
    from parser_service.markdown_pipeline import wrap_md_as_prediction

    pred = wrap_md_as_prediction("body", DIGITAL)
    validate_against_wheel(pred)  # would fail if additionalProperties: false is violated

    sha = pred["source"]["sha256"]
    assert len(sha) == 64
    assert all(c in "0123456789abcdef" for c in sha)


# ---------------------------------------------------------------------------
# Test 4: a prebuilt source dict is accepted and passed through unchanged.
# ---------------------------------------------------------------------------


def test_wrap_accepts_prebuilt_source_dict(validate_against_wheel: Validator) -> None:
    """A caller-supplied full source dict is accepted (path-or-dict contract)."""
    from parser_service.markdown_pipeline import wrap_md_as_prediction

    source = {
        "doc_id": "fixed",
        "filename": "fixed.pdf",
        "mime_type": "application/pdf",
        "sha256": "a" * 64,
    }
    md = "wrapped from a prebuilt source"
    pred = wrap_md_as_prediction(md, source)
    validate_against_wheel(pred)

    assert pred["source"] == source
    assert len(pred["elements"]) == 1
    assert pred["elements"][0]["text"] == md
