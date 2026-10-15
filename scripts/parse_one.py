"""
parse_one.py

Parse a single document file to Markdown and print or save it.

Usage:
    uv run python scripts/parse_one.py --input <path> [--output <path>]

If --output is omitted, the Markdown is printed to stdout; a directory as --output
gets ``<input stem>.md``. Uses ``parsing.document.parse_to_markdown`` on the whole
file (no page split); ``scripts/parse_pages.py`` runs the page pipeline production
runs. ``--format md`` is still accepted, for older commands.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("PARSER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from parser_service.parsing.document import parse_to_markdown  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse a single document file to Markdown.")
    parser.add_argument("--input", required=True, help="Path to the document file.")
    parser.add_argument("--output", default=None, help="Output path (default: stdout).")
    parser.add_argument("--format", choices=["md"], default="md", help=argparse.SUPPRESS)
    args = parser.parse_args()

    file_path = Path(args.input)
    if not file_path.exists():
        print(f"Error: file not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    content = parse_to_markdown(file_path)["markdown"]
    if args.output:
        out_path = Path(args.output)
        if out_path.is_dir():
            out_path = out_path / (file_path.stem + ".md")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        logging.getLogger(__name__).info("Markdown written to %s", out_path)
    else:
        print(content)


if __name__ == "__main__":
    main()
