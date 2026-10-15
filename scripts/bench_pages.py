"""
bench_pages.py — benchmark predictions from the production page pipeline.

Runs every file of a folder through ``parse_pdf_pages``, the split-first page
pipeline production runs (intake, the per-kind handlers and guards, one record per
unit, the fail-closed stitch), and writes what the doc-bench grader reads, in the
same layout as ``parse_batch.py --emit-test-json``:

    <output>/<stem>.md        the stitched document, page markers removed
    <output>/<stem>.json      the same markdown wrapped as a schema-valid prediction
    <output>/route_stats.csv  one row per file: route, pages, escalated pages, state
    <output>/failures.json    files that did not complete, with their status

and one ``{"event": "file_parsed", ...}`` JSON log line per file, which
``aggregate_benchmark.py`` reads for latency. A file that does not complete gets an
empty prediction, as production would deliver nothing for it.

Usage:
    uv run python scripts/bench_pages.py --input <folder> --output <predictions dir> \\
        [--store <dir>] [--no-escalate] [--reuse]

Pages run in this process, one at a time, with Docling's models loaded once and
kept (``workers=0``): the same parse code as the page service, without its
per-unit deadline or pod parallelism. A warm-up parse runs before the timed files,
so ``parse_duration_s`` is a file's parse compute with warm models, not
production wall time. Each file is re-parsed from scratch unless ``--reuse``.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=os.environ.get("PARSER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from parser_service.parsing.document import wrap_md_as_prediction  # noqa: E402
from parser_service.parsing.route_stats import route_record  # noqa: E402
from parser_service.pipeline.api import parse_pdf_pages  # noqa: E402
from parser_service.pipeline.config import FILE_COMPLETE, strip_page_markers  # noqa: E402
from parser_service.settings import SettingsError, load_settings  # noqa: E402

logger = logging.getLogger("bench_pages")

# Gold and notes that sit next to the source files in a doc-bench fixture folder.
_SKIPPED_SUFFIXES = {".json", ".md"}
_WARMUP_FILE = Path(__file__).parent.parent / "tests" / "fixtures" / "three_page_digital.pdf"
_ROUTE_FIELDS = ["doc_id", "route", "pages", "vlm_pages", "state", "routes", "reason"]


def _inputs(folder: Path) -> list[Path]:
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() not in _SKIPPED_SUFFIXES and not p.name.startswith(".")
    )


def _records(result: dict[str, Any]) -> list[dict[str, Any]]:
    """The unit records of a parsed file (none for a file refused at intake)."""
    if not result.get("work_dir"):
        return []
    results = Path(result["work_dir"]) / "results"
    return [json.loads(p.read_text()) for p in sorted(results.glob("[0-9][0-9][0-9][0-9].json"))]


def _route_row(
    doc_id: str, result: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    """``route_stats`` over the escalation routes of every unit; ``blank`` and the
    single-unit routes (``txt_passthrough``, ``empty_document``) are outside that
    vocabulary and keep their own name."""
    page_routes = [route for record in records for route in record.get("page_routes") or []]
    row: dict[str, Any] = {"doc_id": doc_id, "pages": result.get("page_count"), "vlm_pages": 0}
    try:
        stats = route_record(page_routes, doc_id) if page_routes else None
    except AssertionError:
        stats = None
    if stats is not None:
        row.update(route=stats["route"], vlm_pages=stats["vlm_pages"], routes=stats["routes"])
        row["reason"] = stats["reason"]
    else:
        routes = [str(record.get("route")) for record in records] or ["-"]
        row.update(route=routes[0] if len(set(routes)) == 1 else "mixed", routes=",".join(routes))
        row["reason"] = ""
    if result.get("state") != FILE_COMPLETE:
        row["route"] = "error"
    row["state"] = result.get("state")
    return row


def _markdown(result: dict[str, Any]) -> str:
    """The stitched document without page markers ("" for a file that did not complete)."""
    if result.get("state") != FILE_COMPLETE:
        return ""
    return strip_page_markers((Path(result["out_dir"]) / "output.md").read_text(encoding="utf-8"))


def run(folder: Path, output: Path, store: Path, escalate: bool, reuse: bool) -> int:
    output.mkdir(parents=True, exist_ok=True)
    files = _inputs(folder)
    if not files:
        print(f"Error: no input files in {folder}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="bench-warmup-") as warm_store:
        started = time.perf_counter()
        parse_pdf_pages(_WARMUP_FILE, Path(warm_store), workers=0, escalate=False)
        logger.info("warm-up done in %.1f s", time.perf_counter() - started)

    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for source in files:
        started = time.perf_counter()
        result = parse_pdf_pages(source, store, workers=0, escalate=escalate, restart=not reuse)
        seconds = time.perf_counter() - started
        records = _records(result)
        markdown = _markdown(result)
        (output / f"{source.stem}.md").write_text(markdown, encoding="utf-8")
        prediction = wrap_md_as_prediction(markdown, source)
        (output / f"{source.stem}.json").write_text(json.dumps(prediction, indent=2), "utf-8")

        row = _route_row(source.stem, result, records)
        rows.append(row)
        calls = {"vlm": 0, "textract": 0}
        for record in records:
            for engine, count in (record.get("call_counts") or {}).items():
                calls[engine] = calls.get(engine, 0) + int(count or 0)
        if result.get("state") != FILE_COMPLETE:
            failures.append(
                {
                    "filename": source.name,
                    "state": result.get("state"),
                    "status": result.get("status_path"),
                    "not_ok_pages": result.get("not_ok_pages"),
                }
            )
        logger.info(
            json.dumps(
                {
                    "event": "file_parsed",
                    "filename": source.name,
                    "kind": result.get("kind"),
                    "state": result.get("state"),
                    "page_count": result.get("page_count"),
                    "route": row["route"],
                    "vlm_routed_pages": row["vlm_pages"],
                    "markdown_chars": len(markdown),
                    "parse_duration_s": round(seconds, 3),
                    "unit_seconds_sum": round(
                        sum(float(r.get("elapsed_seconds") or 0) for r in records), 3
                    ),
                    "vlm_call_count": calls.get("vlm", 0),
                    "textract_call_count": calls.get("textract", 0),
                }
            )
        )

    with (output / "route_stats.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_ROUTE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    (output / "failures.json").write_text(json.dumps(failures, indent=2), encoding="utf-8")
    done = sum(1 for row in rows if row["state"] == FILE_COMPLETE)
    print(f"{done}/{len(rows)} files complete -> {output}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--input", required=True, type=Path, help="Folder of source files")
    parser.add_argument("--output", required=True, type=Path, help="Predictions folder")
    parser.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Page-pipeline store (default: <output>_store, next to the predictions)",
    )
    parser.add_argument(
        "--no-escalate", action="store_true", help="Never call the escalation engine"
    )
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="Keep records from an earlier run (no re-parse; latency is then meaningless)",
    )
    args = parser.parse_args(argv)
    store = args.store or args.output.with_name(args.output.name + "_store")
    try:
        load_settings(store_location=str(store), escalate=not args.no_escalate)
    except SettingsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    if not args.reuse and args.store is None and store.exists():
        shutil.rmtree(store)
    sys.exit(run(args.input, args.output, store.resolve(), not args.no_escalate, args.reuse))


if __name__ == "__main__":
    main()
