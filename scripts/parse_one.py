"""
parse_one.py

Parse a single document file and print or save the output.

Usage:
    uv run python scripts/parse_one.py --input <path> [--output <path>] [--format json|md|both]

If --output is omitted, output is printed to stdout.

--format json (default) emits the LEGACY element JSON via ``parse()`` (kept for
A/B comparison and rollback during the markdown-first transition — roadmap
item 26). --format md emits the markdown-first output via
``markdown_pipeline.parse_to_markdown``. --format both writes legacy JSON + md.
The wrapped 1-paragraph prediction is NOT a user-facing format; it is test
plumbing emitted only by the eval path (``parse_batch.py --emit-test-json``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("PARSER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from parser_service import parse  # noqa: E402
from parser_service.markdown_pipeline import parse_to_markdown  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse a single document file.")
    parser.add_argument("--input", required=True, help="Path to the document file.")
    parser.add_argument("--output", default=None, help="Output path (default: stdout).")
    parser.add_argument(
        "--format",
        choices=["json", "md", "both"],
        default="json",
        help=(
            "Output format: json (default, LEGACY element JSON via parse()), "
            "md (markdown-first via parse_to_markdown), or both."
        ),
    )
    args = parser.parse_args()

    file_path = Path(args.input)
    if not file_path.exists():
        print(f"Error: file not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    log = logging.getLogger(__name__)

    if args.format == "both":
        if not args.output:
            print("Error: --output <directory> is required with --format both", file=sys.stderr)
            sys.exit(1)
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        # Legacy element JSON via parse(); markdown via the markdown-first path.
        legacy = parse(file_path)
        md = parse_to_markdown(file_path)["markdown"]
        json_path = out_dir / (file_path.stem + ".json")
        md_path = out_dir / (file_path.stem + ".md")
        json_path.write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(md, encoding="utf-8")
        log.info("JSON written to %s", json_path)
        log.info("Markdown written to %s", md_path)
    elif args.format == "md":
        content = parse_to_markdown(file_path)["markdown"]
        if args.output:
            out_path = Path(args.output)
            if out_path.is_dir():
                out_path = out_path / (file_path.stem + ".md")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")
            log.info("Markdown written to %s", out_path)
        else:
            print(content)
    else:
        # Legacy element JSON via parse() — unchanged (transition + rollback).
        result = parse(file_path)
        content = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            out_path = Path(args.output)
            if out_path.is_dir():
                out_path = out_path / (file_path.stem + ".json")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")
            log.info("JSON written to %s", out_path)
        else:
            print(content)


if __name__ == "__main__":
    main()
