"""
test_bench_pages.py — scripts/bench_pages.py: doc-bench predictions from the page
pipeline, offline (``--no-escalate``, no AWS).

Run ONLY these tests:
    uv run pytest tests/test_bench_pages.py
"""

from __future__ import annotations

import csv
import importlib.util
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


def _load_bench() -> Any:
    sys.path.insert(0, str(ROOT / "src"))
    spec = importlib.util.spec_from_file_location(
        "bench_pages", ROOT / "scripts" / "bench_pages.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bench_pages_writes_graded_predictions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("PARSER_ENABLED_KINDS", raising=False)
    monkeypatch.delenv("PARSER_STORE_LOCATION", raising=False)
    bench = _load_bench()
    folder = tmp_path / "dataset"
    folder.mkdir()
    shutil.copy(FIXTURES / "three_page_digital.pdf", folder / "doc_a.pdf")
    shutil.copy(FIXTURES / "page.html", folder / "doc_b.html")
    shutil.copy(FIXTURES / "sheet.xlsx", folder / "doc_c.xlsx")  # refused: unsupported_type
    (folder / "doc_a.json").write_text("{}")  # gold next to the source: skipped
    (folder / "README.md").write_text("notes")  # skipped
    output = tmp_path / "predictions"

    caplog.set_level(logging.INFO, logger="bench_pages")
    with pytest.raises(SystemExit) as done:
        bench.main(["--input", str(folder), "--output", str(output), "--no-escalate"])
    assert done.value.code == 0

    assert sorted(p.name for p in output.iterdir()) == [
        "doc_a.json",
        "doc_a.md",
        "doc_b.json",
        "doc_b.md",
        "doc_c.json",
        "doc_c.md",
        "failures.json",
        "route_stats.csv",
    ]
    assert (tmp_path / "predictions_store" / "work").is_dir()  # the default store

    # The stitched document without page markers, wrapped as one paragraph.
    markdown = (output / "doc_a.md").read_text()
    assert "PAGE1_SENTINEL" in markdown and "PAGE3_SENTINEL" in markdown
    assert "==== page" not in markdown
    prediction = json.loads((output / "doc_a.json").read_text())
    assert [e["text"] for e in prediction["elements"]] == [markdown]
    assert prediction["source"]["filename"] == "doc_a.pdf"

    # A refused file: an empty prediction, a failure, and route "error".
    assert (output / "doc_c.md").read_text() == ""
    failures = json.loads((output / "failures.json").read_text())
    assert [(f["filename"], f["state"]) for f in failures] == [("doc_c.xlsx", "failed")]
    with (output / "route_stats.csv").open() as f:
        rows = {row["doc_id"]: row for row in csv.DictReader(f)}
    assert (rows["doc_a"]["route"], rows["doc_a"]["pages"], rows["doc_a"]["state"]) == (
        "docling-kept",
        "3",
        "complete",
    )
    assert (rows["doc_b"]["route"], rows["doc_c"]["route"]) == ("docling-kept", "error")

    # One file_parsed line per file, as aggregate_benchmark.py reads them.
    events = [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "bench_pages" and r.getMessage().startswith("{")
    ]
    assert [e["filename"] for e in events] == ["doc_a.pdf", "doc_b.html", "doc_c.xlsx"]
    assert all(e["event"] == "file_parsed" and e["parse_duration_s"] >= 0 for e in events)
    assert events[0]["page_count"] == 3 and events[0]["vlm_call_count"] == 0
