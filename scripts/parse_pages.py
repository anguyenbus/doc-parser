"""
parse_pages.py

Parse one PDF with the split-first page pipeline: split into one-page files,
parse the pages in parallel (Docling first, escalation only when the gate
promotes), and stitch one markdown file when every page is ok.

Usage:
    uv run python scripts/parse_pages.py --input <file.pdf | s3://bucket/key.pdf> \
        [--work-dir <dir | s3://bucket/prefix>] [--output <out.md>] [--workers 4] \
        [--no-escalate] [--partial] [--restart] \
        [--executor kserve --endpoint http://page-parser-predictor.doc-parser] [--concurrency 8]

    # One PDF of a bundle, picked by index (a Kubernetes Indexed Job sets the index):
    uv run python scripts/parse_pages.py --input-list <list.txt | s3://bucket/list.txt> \
        [--index N]   # default: $JOB_COMPLETION_INDEX

Everything is kept under one store location: PARSER_STORE_LOCATION (a local
directory or s3://bucket[/prefix]), or --work-dir, which overrides it. Under it:

    work/<file id>/   source.pdf, the one-page files, attempts and page results
    out/<file id>/    status.json, and output.md + output.json when complete

Pages are parsed by worker processes here (--executor local, the default) or by
the page service (--executor kserve): each page is POSTed to the KServe
InferenceService at --endpoint / PARSER_KSERVE_URL, which writes its record to the
same store. The split, the retry bookkeeping, the status and the stitch run here
either way, and the kserve executor needs an s3:// store location.

An --input-list file holds one PDF (path or s3:// URI) per line; blank lines and
lines starting with # are skipped, and --index picks one (0-based).

An s3:// input is downloaded to a temporary directory for the run. The settings
are checked at start-up (see parser_service/settings.py): an s3:// location or
input, and escalation, need AWS_REGION; a local run with --no-escalate needs no
AWS setting at all.

Re-running the same command resumes: pages already ok are skipped and the rest
are retried until the attempt limit (PARSER_PAGE_MAX_ATTEMPTS, default 3).

Exit codes: 0 every page ok and the document was stitched; 2 the file is not
complete (see out/<file id>/status.json; its location is printed); 1 the input or
the settings could not be used.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("PARSER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from parser_service import io_layer  # noqa: E402
from parser_service.page_pipeline import (  # noqa: E402
    FILE_COMPLETE,
    PipelineConfigMismatch,
    RemotePageError,
    parse_pdf_pages_in_store,
)
from parser_service.settings import SettingsError, load_settings  # noqa: E402
from parser_service.store import open_store  # noqa: E402


def _fetch_s3_input(uri: str, region: str | None, directory: Path) -> Path:
    """Download ``s3://bucket/key.pdf`` into ``directory`` under the key's base name.

    The name is kept because the stem, with the content hash, is the file id. One
    get, streamed to disk. A URI that is not a PDF key raises ``ValueError`` before
    any call; a missing object raises ``FileNotFoundError``; any other S3 error
    propagates.
    """
    from botocore.exceptions import ClientError

    bucket, key = io_layer.parse_s3_uri(uri)
    name = key.rpartition("/")[2]
    if not bucket or not name:
        raise ValueError(f"the input is not an s3://bucket/key.pdf URI: {uri}")
    if Path(name).suffix.lower() != ".pdf":
        raise ValueError(f"the page pipeline handles PDFs only, got {uri}")
    try:
        response = io_layer.s3_client(region).get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchKey":
            raise FileNotFoundError(f"{uri} does not exist") from exc
        raise
    target = directory / name
    with target.open("wb") as handle:
        shutil.copyfileobj(response["Body"], handle)
    return target


def _pick_from_list(list_uri: str, index: int | None, region: str | None) -> str:
    """The ``index``-th PDF named in the list at ``list_uri`` (a path or s3:// URI)."""
    if index is None:
        raw = os.environ.get("JOB_COMPLETION_INDEX")
        if raw is None:
            raise ValueError("--input-list needs --index or JOB_COMPLETION_INDEX")
        index = int(raw)
    if list_uri.startswith("s3://"):
        bucket, key = io_layer.parse_s3_uri(list_uri)
        body = io_layer.s3_client(region).get_object(Bucket=bucket, Key=key)["Body"].read()
        text = body.decode("utf-8")
    else:
        text = Path(list_uri).read_text(encoding="utf-8")
    inputs = [line.strip() for line in text.splitlines()]
    inputs = [line for line in inputs if line and not line.startswith("#")]
    if not 0 <= index < len(inputs):
        raise ValueError(f"index {index} is outside {list_uri}, which names {len(inputs)} PDFs")
    return inputs[index]


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse a PDF page by page and stitch it.")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--input", help="The PDF: a path or s3://bucket/key.pdf.")
    which.add_argument(
        "--input-list", help="A list of PDFs, one per line (a path or s3:// URI); see --index."
    )
    parser.add_argument(
        "--index",
        type=int,
        default=None,
        help="Which PDF of --input-list (0-based; default $JOB_COMPLETION_INDEX).",
    )
    parser.add_argument(
        "--work-dir",
        default=None,
        help="Store location for page files, records, status and the document: a directory "
        "or s3://bucket[/prefix]. Overrides PARSER_STORE_LOCATION.",
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
        "--restart",
        action="store_true",
        help="Discard everything stored for this file (work/ and out/) and start again.",
    )
    parser.add_argument(
        "--executor",
        choices=("local", "kserve"),
        default=None,
        help="Where pages are parsed: worker processes here, or the KServe page service "
        "(default PARSER_EXECUTOR or local).",
    )
    parser.add_argument(
        "--endpoint",
        default=None,
        help="The page service base URL for --executor kserve. Overrides PARSER_KSERVE_URL.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Page requests in flight for --executor kserve "
        "(default PARSER_KSERVE_CONCURRENCY, else --workers).",
    )
    args = parser.parse_args()
    list_is_s3 = bool(args.input_list and args.input_list.startswith("s3://"))

    try:
        settings = load_settings(
            store_location=args.work_dir,
            input_is_s3=list_is_s3 or bool(args.input and args.input.startswith("s3://")),
            escalate=not args.no_escalate,
            executor=args.executor,
            kserve_url=args.endpoint,
        )
        if args.input_list:
            args.input = _pick_from_list(args.input_list, args.index, settings.aws_region)
            print(f"input: {args.input}", file=sys.stderr)
            if args.input.startswith("s3://") and settings.aws_region is None:
                raise SettingsError(["AWS_REGION is required (the input is s3://)"])
    except (SettingsError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    input_is_s3 = args.input.startswith("s3://")

    location = settings.store_location
    if settings.store_kind == "local":
        location = str(Path(location).resolve())
    store = open_store(location, region=settings.aws_region)

    try:
        with contextlib.ExitStack() as stack:
            if input_is_s3:
                # Removed when the block ends, whatever the outcome of the run.
                directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="parser-input-"))
                source = _fetch_s3_input(args.input, settings.aws_region, Path(directory))
            else:
                source = Path(args.input)
            result = parse_pdf_pages_in_store(
                source,
                location,
                settings.aws_region,
                store=store,
                workers=settings.page_workers if args.workers is None else args.workers,
                escalate=not args.no_escalate,
                max_attempts=settings.page_max_attempts,
                partial=args.partial,
                restart=args.restart,
                executor=settings.executor,
                endpoint=settings.kserve_url,
                concurrency=args.concurrency or settings.kserve_concurrency,
            )
    except (FileNotFoundError, ValueError, PipelineConfigMismatch, RemotePageError) as exc:
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
    print(f"status: {result['status_path']}", file=sys.stderr)

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
        output.write_bytes(store.read(result["output_key"]))
        print(f"copied to: {output}", file=sys.stderr)


if __name__ == "__main__":
    main()
