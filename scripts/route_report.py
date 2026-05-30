"""
route_report.py

Scan a directory of parser_service JSON outputs and emit a routing breakdown:
which documents were handled by the VLM vs kept on Docling, and why.

Usage:
    uv run python scripts/route_report.py --input <dir-of-json> [--output route_stats.csv]

If --output is omitted, writes <input>/route_stats.csv. The table and totals are
also printed to stdout.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from parser_service.route_stats import (  # noqa: E402
    format_table,
    route_record,
    write_route_csv,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Routing breakdown for parser outputs.")
    parser.add_argument("--input", required=True, help="Directory of parser output *.json files.")
    parser.add_argument(
        "--output",
        default=None,
        help="CSV path (default: <input>/route_stats.csv).",
    )
    args = parser.parse_args()

    in_dir = Path(args.input)
    if not in_dir.is_dir():
        print(f"Error: not a directory: {in_dir}", file=sys.stderr)
        sys.exit(1)

    out_csv = Path(args.output) if args.output else in_dir / "route_stats.csv"

    records = []
    for fp in sorted(in_dir.glob("*.json")):
        # Skip sidecar files produced by batch/dump runs.
        if fp.name in {"route_stats.csv", "failures.json", "manifest.json"}:
            continue
        try:
            output = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"WARNING: skipping {fp.name}: {exc}", file=sys.stderr)
            continue
        if not isinstance(output, dict) or "elements" not in output:
            continue
        records.append(route_record(output, doc_id=fp.stem))

    if not records:
        print(f"No parser output JSON files found in {in_dir}", file=sys.stderr)
        sys.exit(1)

    write_route_csv(records, out_csv)
    print(format_table(records))
    print(f"\nRoute stats CSV written to: {out_csv}")


if __name__ == "__main__":
    main()
