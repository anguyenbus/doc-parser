"""
test_scripts_rewiring.py — Task Group 4 tests (task 4.1).

Covers the CLI/script rewiring onto the markdown-first path, with the VLM
mocked (no Bedrock needed):

  - parse_one.py               → parse_to_markdown output (markdown only)
  - parse_batch.py             → ALWAYS writes .md; wrapped .json ONLY with --emit-test-json
  - route_stats.route_record   → derives routes from page_routes, with the
                                  route-counts-sum-to-page-count invariant.

Run ONLY these tests (task 4.6):
    uv run pytest tests/test_scripts_rewiring.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = Path(__file__).parent / "fixtures"

DIGITAL = FIXTURES / "digital_simple.pdf"


# ---------------------------------------------------------------------------
# Helpers — load the scripts as importable modules.
# ---------------------------------------------------------------------------


def _load_script(name: str) -> Any:
    """Import scripts/<name>.py as a module (they self-insert src/ on sys.path)."""
    sys.path.insert(0, str(ROOT / "src"))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# parse_one.py --format routing
# ===========================================================================


def test_parse_one_prints_parse_to_markdown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """parse_one prints parse_to_markdown's markdown (``--format md`` still accepted)."""
    parse_one = _load_script("parse_one")
    sentinel = "PARSE_TO_MARKDOWN_OUTPUT_SENTINEL"
    calls: list[Any] = []

    def _fake_ptm(path: Any) -> dict[str, Any]:
        calls.append(path)
        return {"markdown": sentinel, "page_routes": [], "warnings": []}

    monkeypatch.setattr(parse_one, "parse_to_markdown", _fake_ptm)
    for argv in (["--input", str(DIGITAL)], ["--input", str(DIGITAL), "--format", "md"]):
        monkeypatch.setattr(sys, "argv", ["parse_one.py", *argv])
        parse_one.main()
        assert sentinel in capsys.readouterr().out
    assert calls == [DIGITAL, DIGITAL]


def test_parse_one_writes_md_into_an_output_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parse_one = _load_script("parse_one")
    monkeypatch.setattr(
        parse_one,
        "parse_to_markdown",
        lambda p: {"markdown": "MD_OUTPUT", "page_routes": [], "warnings": []},
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.setattr(
        sys, "argv", ["parse_one.py", "--input", str(DIGITAL), "--output", str(out_dir)]
    )
    parse_one.main()
    assert (out_dir / (DIGITAL.stem + ".md")).read_text() == "MD_OUTPUT"


# ===========================================================================
# parse_batch.py — always .md; wrapped .json only with --emit-test-json
# ===========================================================================


def _patch_batch_parse(monkeypatch: pytest.MonkeyPatch, batch: Any) -> None:
    """Make parse_to_markdown (inside parse_batch) deterministic and VLM-free."""

    def _fake_ptm(path: Any) -> dict[str, Any]:
        return {
            "markdown": "BATCH_MD_BODY",
            "page_routes": [
                {"page_index": 0, "route": "docling-kept", "reason": None},
            ],
            "warnings": [],
        }

    monkeypatch.setattr(batch, "parse_to_markdown", _fake_ptm)


def test_parse_batch_always_writes_md_no_json_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without --emit-test-json, parse_batch writes .md but NOT the wrapped .json."""
    batch = _load_script("parse_batch")
    _patch_batch_parse(monkeypatch, batch)

    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    out_dir = tmp_path / "out"

    monkeypatch.setattr(
        sys,
        "argv",
        ["parse_batch.py", "--input", str(in_dir), "--output", str(out_dir), "--concurrency", "1"],
    )
    batch.main()

    assert (out_dir / "doc.md").read_text() == "BATCH_MD_BODY"
    # No wrapped prediction JSON when the flag is absent.
    assert not (out_dir / "doc.json").exists()


def test_parse_batch_emits_wrapped_json_with_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With --emit-test-json, parse_batch ALSO writes the wrapped one-paragraph JSON."""
    batch = _load_script("parse_batch")
    _patch_batch_parse(monkeypatch, batch)

    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    out_dir = tmp_path / "out"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "parse_batch.py",
            "--input",
            str(in_dir),
            "--output",
            str(out_dir),
            "--emit-test-json",
            "--concurrency",
            "1",
        ],
    )
    batch.main()

    assert (out_dir / "doc.md").read_text() == "BATCH_MD_BODY"
    pred_path = out_dir / "doc.json"
    assert pred_path.exists()
    pred = json.loads(pred_path.read_text())
    # The wrapped prediction is the single-paragraph schema-valid shape.
    assert pred["schema_version"] == "1.0.0"
    assert len(pred["elements"]) == 1
    assert pred["elements"][0]["type"] == "paragraph"
    assert pred["elements"][0]["text"] == "BATCH_MD_BODY"


def test_parse_batch_route_stats_csv_from_page_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """route_stats.csv is derived from page_routes and keeps the doc_id/route columns
    that aggregate_benchmark.py consumes."""
    batch = _load_script("parse_batch")

    def _fake_ptm(path: Any) -> dict[str, Any]:
        return {
            "markdown": "x",
            "page_routes": [
                {"page_index": 0, "route": "docling-kept", "reason": None},
                {"page_index": 1, "route": "vlm", "reason": "forced"},
            ],
            "warnings": [],
        }

    monkeypatch.setattr(batch, "parse_to_markdown", _fake_ptm)

    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    out_dir = tmp_path / "out"

    monkeypatch.setattr(
        sys,
        "argv",
        ["parse_batch.py", "--input", str(in_dir), "--output", str(out_dir), "--concurrency", "1"],
    )
    batch.main()

    import csv as _csv

    with (out_dir / "route_stats.csv").open() as f:
        rows = list(_csv.DictReader(f))
    assert len(rows) == 1
    row = rows[0]
    # aggregate_benchmark.py reads exactly these two columns.
    assert row["doc_id"] == "doc"
    assert row["route"] == "vlm"  # any vlm page → doc roll-up "vlm"
    assert row["pages"] == "2"
    assert row["vlm_pages"] == "1"


# ===========================================================================
# route_stats.route_record — page_routes derived + sum invariant
# ===========================================================================


def test_route_record_derives_from_page_routes() -> None:
    """route_record reads routes from page_routes (the exact vocabulary), not vlm_ prefixes."""
    from parser_service.parsing import route_stats

    page_routes = [
        {"page_index": 0, "route": "docling-kept", "reason": None},
        {"page_index": 1, "route": "vlm", "reason": "forced_for_test"},
        {"page_index": 2, "route": "vlm-fallback-docling", "reason": "boom"},
    ]
    rec = route_stats.route_record(page_routes, doc_id="sample")

    assert rec["doc_id"] == "sample"
    assert rec["pages"] == 3
    assert rec["vlm_pages"] == 2  # vlm + vlm-fallback-docling
    assert rec["route"] == "vlm"  # any vlm page → doc roll-up "vlm"
    assert rec["reason"] == "forced_for_test"


def test_route_record_all_docling_kept() -> None:
    """A doc with only docling-kept pages rolls up to docling-kept with 0 vlm pages."""
    from parser_service.parsing import route_stats

    page_routes = [{"page_index": i, "route": "docling-kept", "reason": None} for i in range(4)]
    rec = route_stats.route_record(page_routes, doc_id="clean")
    assert rec["route"] == "docling-kept"
    assert rec["vlm_pages"] == 0
    assert rec["pages"] == 4


def test_route_record_all_fallback_is_vlm_failed() -> None:
    """When every promoted page falls back, the doc rolls up to vlm-failed."""
    from parser_service.parsing import route_stats

    page_routes = [
        {"page_index": 0, "route": "vlm-fallback-docling", "reason": "boom"},
        {"page_index": 1, "route": "vlm-fallback-docling", "reason": "boom"},
    ]
    rec = route_stats.route_record(page_routes, doc_id="failed")
    assert rec["route"] == "vlm-failed"
    assert rec["vlm_pages"] == 2


def test_route_counts_sum_to_page_count_invariant() -> None:
    """An unknown/drifted route value violates the sum==page_count invariant LOUDLY.

    This is the guard against a route-vocabulary mismatch silently reporting
    zeros/wrong splits without failing.
    """
    from parser_service.parsing import route_stats

    # A drifted route ("vlm_p0" — the OLD element-ID-prefix style) is not in the
    # known vocabulary, so it is counted nowhere and the sum must not match.
    bad_routes = [
        {"page_index": 0, "route": "docling-kept", "reason": None},
        {"page_index": 1, "route": "vlm_p0", "reason": None},  # drifted vocabulary
    ]
    with pytest.raises(AssertionError, match="route-count invariant"):
        route_stats.route_record(bad_routes, doc_id="drifted")


def test_route_record_empty_page_routes_invariant_holds() -> None:
    """Zero page_routes is consistent (0 == 0) and rolls up to docling-kept."""
    from parser_service.parsing import route_stats

    rec = route_stats.route_record([], doc_id="empty")
    assert rec["pages"] == 0
    assert rec["route"] == "docling-kept"
