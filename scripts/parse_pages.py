"""
parse_pages.py

Parse one file with the split-first page pipeline: intake sniffs the content to
route it to a kind (a file no enabled kind handles fails alone with
unsupported_type); a PDF is split into one-page files, any other kind is one unit;
the units are parsed in parallel (Docling first, escalation only when the gate
promotes), and one markdown file is stitched when every unit is ok.

Usage:
    uv run python scripts/parse_pages.py --input <file | s3://bucket/key> \
        [--work-dir <dir | s3://bucket/prefix>] [--output <out.md>] [--workers 4] \
        [--no-escalate] [--partial] [--restart] \
        [--executor kserve --endpoint http://page-parser-predictor.doc-parser] [--concurrency 8]

    # One file of a bundle, picked by index (a Kubernetes Indexed Job sets the index):
    uv run python scripts/parse_pages.py --input-list <list.txt | s3://bucket/list.txt> \
        [--index N]   # default: $JOB_COMPLETION_INDEX

    # One document of an inbox job (job.json; see parser_service/delivery/jobs.py), published:
    uv run python scripts/parse_pages.py --job s3://bucket/parser/inbox/<system>/<job>/job.json \
        [--index N] [--publish-partial]

    # Publish a single file under a doc id (default: the file name, slugified):
    uv run python scripts/parse_pages.py --input report.pdf --publish [--doc-id annual-report-2025]

Everything is kept under one store location: PARSER_STORE_LOCATION (a local
directory or s3://bucket[/prefix]), or --work-dir, which overrides it. Under it:

    work/<sha256>/<config hash>/   source.pdf, the one-page files, attempts and page
                                   results (keyed by content and config: the same
                                   bytes under any name share them)
    out/<file id>/                 output.md + output.json when complete; status.json
    published/v1/                  with --job or --publish: the versioned hand-off
                                   (see parser_service/delivery/publish.py)
    ops/jobs/<job id>/             with --job: per-document outcomes and the job status

Pages are parsed by worker processes here (--executor local, the default) or by
the page service (--executor kserve): each page is POSTed to the KServe
InferenceService at --endpoint / PARSER_KSERVE_URL, which writes its record to the
same store. The split, the retry bookkeeping, the status and the stitch run here
either way, and the kserve executor needs an s3:// store location.

An --input-list file holds one file (path or s3:// URI) per line; blank lines and
lines starting with # are skipped, and --index picks one (0-based).

An s3:// input is downloaded to a temporary directory for the run. The settings
are checked at start-up (see parser_service/settings.py): an s3:// location or
input, and escalation, need AWS_REGION; a local run with --no-escalate needs no
AWS setting at all.

Re-running the same command resumes: pages already ok are skipped and the rest
are retried until the attempt limit (PARSER_PAGE_MAX_ATTEMPTS, default 3).

With --job, a document whose action is "delete" publishes a deleted event and
parses nothing. An incomplete document publishes a failed event, or a partial
version with --publish-partial.

Exit codes: 0 every page ok and the document was stitched (or deleted); 2 the
file is not complete (see out/<file id>/status.json; its location is printed); 1
the input or the settings could not be used.
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

from parser_service.delivery.jobs import (  # noqa: E402
    ACTION_DELETE,
    DOC_DELETED,
    Job,
    JobDoc,
    parse_job,
    rebuild_job_status,
    record_doc,
    resolve_file,
)
from parser_service.delivery.publish import PublishRequest, publish_deletion, slugify  # noqa: E402
from parser_service.observability import truncate  # noqa: E402
from parser_service.pipeline.api import parse_pdf_pages_in_store  # noqa: E402
from parser_service.pipeline.config import FILE_COMPLETE, PipelineConfigMismatch  # noqa: E402
from parser_service.pipeline.remote_runner import RemotePageError  # noqa: E402
from parser_service.settings import SettingsError, load_settings  # noqa: E402
from parser_service.storage import aws  # noqa: E402
from parser_service.storage.store import open_store  # noqa: E402


def _fetch_s3_input(uri: str, region: str | None, directory: Path) -> tuple[Path, str | None]:
    """Download ``s3://bucket/key`` into ``directory`` under the key's base name.

    Returns the path and the object's ``Content-Type`` (None when it has none),
    which intake's sniff uses as the declared type. Any key is accepted: the
    content decides the kind. The name is kept because the stem, with the content
    hash, is the file id. One get, streamed to disk. A URI with no bucket or key
    raises ``ValueError`` before any call; a missing object raises
    ``FileNotFoundError``; any other S3 error propagates.
    """
    from botocore.exceptions import ClientError

    bucket, key = aws.parse_s3_uri(uri)
    name = key.rpartition("/")[2]
    if not bucket or not name:
        raise ValueError(f"the input is not an s3://bucket/key URI: {uri}")
    try:
        response = aws.s3_client(region).get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchKey":
            raise FileNotFoundError(f"{uri} does not exist") from exc
        raise
    target = directory / name
    with target.open("wb") as handle:
        shutil.copyfileobj(response["Body"], handle)
    return target, response.get("ContentType") or None


def _index(index: int | None, flag: str) -> int:
    if index is not None:
        return index
    raw = os.environ.get("JOB_COMPLETION_INDEX")
    if raw is None:
        raise ValueError(f"{flag} needs --index or JOB_COMPLETION_INDEX")
    return int(raw)


def _read_text(uri: str, region: str | None) -> str:
    """A small text file from a path or an s3:// URI."""
    if uri.startswith("s3://"):
        bucket, key = aws.parse_s3_uri(uri)
        body = aws.s3_client(region).get_object(Bucket=bucket, Key=key)["Body"].read()
        return body.decode("utf-8")
    return Path(uri).read_text(encoding="utf-8")


def _pick_from_job(job_uri: str, index: int | None, region: str | None) -> tuple[Job, JobDoc]:
    """The job at ``job_uri`` and its ``index``-th document."""
    index = _index(index, "--job")
    job = parse_job(_read_text(job_uri, region), job_uri)
    if not 0 <= index < len(job.docs):
        raise ValueError(f"index {index} is outside {job_uri}, which lists {len(job.docs)} documents")
    return job, job.docs[index]


def _pick_from_list(list_uri: str, index: int | None, region: str | None) -> str:
    """The ``index``-th file named in the list at ``list_uri`` (a path or s3:// URI)."""
    index = _index(index, "--input-list")
    text = _read_text(list_uri, region)
    inputs = [line.strip() for line in text.splitlines()]
    inputs = [line for line in inputs if line and not line.startswith("#")]
    if not 0 <= index < len(inputs):
        raise ValueError(f"index {index} is outside {list_uri}, which names {len(inputs)} files")
    return inputs[index]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse one file: a PDF page by page, any other kind as one unit; then stitch."
    )
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--input", help="The file: a path or s3://bucket/key.")
    which.add_argument(
        "--input-list", help="A list of files, one per line (a path or s3:// URI); see --index."
    )
    which.add_argument(
        "--job", help="An inbox job.json (a path or s3:// URI); see --index. Always publishes."
    )
    parser.add_argument(
        "--index",
        type=int,
        default=None,
        help="Which file of --input-list or document of --job (0-based; default "
        "$JOB_COMPLETION_INDEX).",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish the result to published/v1/ under --doc-id (implied by --job).",
    )
    parser.add_argument(
        "--doc-id",
        default=None,
        help="The document id to publish under (default: the file name, slugified).",
    )
    parser.add_argument(
        "--publish-partial",
        action="store_true",
        help="Publish an incomplete document as a partial version instead of a failed event.",
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
    list_is_s3 = any(u and u.startswith("s3://") for u in (args.input_list, args.job))
    job: Job | None = None
    doc: JobDoc | None = None

    try:
        settings = load_settings(
            store_location=args.work_dir,
            input_is_s3=list_is_s3 or bool(args.input and args.input.startswith("s3://")),
            escalate=not args.no_escalate,
            executor=args.executor,
            kserve_url=args.endpoint,
        )
        if args.job:
            job, doc = _pick_from_job(args.job, args.index, settings.aws_region)
            print(f"job: {job.job_id}, document: {doc.doc_id} ({doc.action})", file=sys.stderr)
            if doc.action != ACTION_DELETE:
                args.input = resolve_file(job, doc)
        elif args.input_list:
            args.input = _pick_from_list(args.input_list, args.index, settings.aws_region)
        if args.input and (args.job or args.input_list):
            print(f"input: {args.input}", file=sys.stderr)
            if args.input.startswith("s3://") and settings.aws_region is None:
                raise SettingsError(["AWS_REGION is required (the input is s3://)"])
        publish = None
        if job is not None and doc is not None:
            publish = PublishRequest(doc.doc_id, job.job_id, doc.metadata, args.publish_partial)
        elif args.publish or args.doc_id:
            doc_id = args.doc_id or slugify(Path(args.input).stem)
            publish = PublishRequest(doc_id, None, {}, args.publish_partial)
    except (SettingsError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    location = settings.store_location
    if settings.store_kind == "local":
        location = str(Path(location).resolve())
    store = open_store(location, region=settings.aws_region)

    if job is not None and doc is not None and doc.action == ACTION_DELETE:
        key = publish_deletion(store, doc.doc_id, job.job_id)
        record_doc(store, job, doc, {"state": DOC_DELETED, "event": store.location(key)})
        rebuild_job_status(store, job)
        print(f"DELETED: {doc.doc_id} — event {store.location(key)}", file=sys.stderr)
        return
    input_is_s3 = args.input.startswith("s3://")

    try:
        with contextlib.ExitStack() as stack:
            declared_type: str | None = None
            if input_is_s3:
                # Removed when the block ends, whatever the outcome of the run.
                directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="parser-input-"))
                source, declared_type = _fetch_s3_input(
                    args.input, settings.aws_region, Path(directory)
                )
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
                publish=publish,
                request_timeout=settings.remote_request_timeout_s,
                declared_type=declared_type,
            )
    except (FileNotFoundError, ValueError, PipelineConfigMismatch, RemotePageError) as exc:
        if job is not None and doc is not None:
            record_doc(store, job, doc, {"state": "error", "error": truncate(str(exc))})
            rebuild_job_status(store, job)
        print(f"Error: {truncate(str(exc))}", file=sys.stderr)
        sys.exit(1)

    published = result.get("published")
    if job is not None and doc is not None:
        counts = result["counts"]
        record_doc(
            store,
            job,
            doc,
            {
                "state": result["state"],
                "file_id": result["file_id"],
                "work_id": result["work_id"],
                "kind": result["kind"],
                "unit": result["unit"],
                "detected_type": result["detected_type"],
                "flags": result["flags"],
                "page_count": result["page_count"],
                "ok_pages": counts["ok"],
                "not_ok_pages": [
                    {
                        "page": p["page"],
                        "state": p["state"],
                        "reason": p["reason"],
                        "reason_code": p.get("reason_code"),
                    }
                    for p in result["not_ok_pages"]
                ],
                "published": published["type"] if published else None,
                "version": published["version"] if published else None,
                "status": result["status_path"],
            },
        )
        job_status = rebuild_job_status(store, job)
        print(f"job status: {job_status['state']} ({job_status['docs_by_state']})", file=sys.stderr)

    counts = result["counts"]
    print(
        f"{result['state'].upper()}: {result['page_count']} pages — "
        f"{counts['ok']} ok, {counts['degraded']} degraded, {counts['failed']} failed, "
        f"{counts['pending']} pending — {result['elapsed_seconds']}s",
        file=sys.stderr,
    )
    if result["work_dir"] is not None:  # None: refused at intake, nothing under work/
        print(f"work dir: {result['work_dir']}", file=sys.stderr)
    print(f"status: {result['status_path']}", file=sys.stderr)
    if published and published["type"]:
        where = published["manifest_key"] or published["event_key"]
        print(f"published: {published['type']} {published['version']} — {store.location(where)}", file=sys.stderr)

    if result["state"] != FILE_COMPLETE:
        for page in result["not_ok_pages"]:
            retry = "will retry on re-run" if page["retryable"] else "attempt limit reached"
            print(
                f"  page {page['page']}: {page['state']} — {truncate(page['reason'])} ({retry})",
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
