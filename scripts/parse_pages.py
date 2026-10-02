"""
parse_pages.py

Parse one PDF with the split-first page pipeline: split into one-page files,
parse the pages in parallel (Docling first, escalation only when the gate
promotes), and stitch one markdown file when every page is ok.

Usage:
    uv run python scripts/parse_pages.py --input <file.pdf> --work-dir <dir> \
        [--output <out.md>] [--workers 4] [--no-escalate] [--partial] [--restart]

Re-running the same command resumes: pages already ok are skipped and the rest
are retried until the attempt limit (PARSER_PAGE_MAX_ATTEMPTS, default 3).

Exit codes: 0 every page ok and the document was stitched; 2 the file is not
complete (see status.json in the work directory); 1 the input could not be used.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("PARSER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from parser_service.page_pipeline import (  # noqa: E402
    FILE_COMPLETE,
    PipelineConfigMismatch,
    parse_pdf_pages,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse a PDF page by page and stitch it.")
    parser.add_argument("--input", required=True, help="Path to the PDF.")
    parser.add_argument(
        "--work-dir", required=True, help="Directory for page files, records and status."
    )
    parser.add_argument(
        "--output", default=None, help="Also copy the stitched markdown here when complete."
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Worker processes (default PARSER_PAGE_WORKERS or min(4, cpus // 2)); "
        "0 runs in this process, one page at a time.",
    )
    parser.add_argument(
        "--no-escalate",
        action="store_true",
        help="Make no engine calls; a page the gate promotes is recorded as degraded.",
    )
    parser.add_argument(
        "--partial",
        action="store_true",
        help="When the file is not complete, also write output.partial.md with gap markers.",
    )
    parser.add_argument(
        "--restart", action="store_true", help="Discard the work directory and start again."
    )
    args = parser.parse_args()

    try:
        result = parse_pdf_pages(
            Path(args.input),
            Path(args.work_dir),
            workers=args.workers,
            escalate=not args.no_escalate,
            partial=args.partial,
            restart=args.restart,
        )
    except (FileNotFoundError, ValueError, PipelineConfigMismatch) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    counts = result["counts"]
    print(
        f"{result['state'].upper()}: {result['page_count']} pages — "
        f"{counts['ok']} ok, {counts['degraded']} degraded, {counts['failed']} failed, "
        f"{counts['pending']} pending — {result['elapsed_seconds']}s",
        file=sys.stderr,
    )
    print(f"work dir: {result['work_dir']}", file=sys.stderr)

    if result["state"] != FILE_COMPLETE:
        for page in result["not_ok_pages"]:
            retry = "will retry on re-run" if page["retryable"] else "attempt limit reached"
            print(
                f"  page {page['page']}: {page['state']} — {page['reason']} ({retry})",
                file=sys.stderr,
            )
        if result["partial_path"]:
            print(f"partial document: {result['partial_path']}", file=sys.stderr)
        sys.exit(2)

    print(f"document: {result['output_path']}", file=sys.stderr)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(result["output_path"], output)
        print(f"copied to: {output}", file=sys.stderr)


if __name__ == "__main__":
    main()
