"""
parse_batch.py

Batch document parsing script using asyncio + ThreadPoolExecutor.

Runs the markdown-first pipeline (``markdown_pipeline.parse_to_markdown``): it
ALWAYS writes one ``.md`` per document. When ``--emit-test-json`` is passed it
ALSO writes a schema-valid wrapped prediction ``.json`` (via
``wrap_md_as_prediction``) so the doc-bench wheel can grade the markdown — that
flag is set by ``run_benchmark.sh`` on the eval path and is otherwise off.

Telemetry/logging and ``route_stats.csv`` are driven from the pipeline's
``page_routes`` (page count, VLM-routed pages, route mix), NOT from element-ID
prefixes or warning codes.

Usage:
    uv run python scripts/parse_batch.py \
        --input <local-dir or s3://bucket/prefix> \
        --output <local-dir or s3://bucket/prefix> \
        [--emit-test-json] \
        [--concurrency 4] \
        [--max-files 100] \
        [--timeout-per-file 120.0] \
        [--retry-on-throttle]
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import csv
import io
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# Configure logging before importing parser_service.
logging.basicConfig(
    level=os.environ.get("PARSER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from parser_service.io_layer import (  # noqa: E402
    InputRef,
    list_input_files,
    write_json,
    write_text,
)
from parser_service.markdown_pipeline import (  # noqa: E402
    parse_to_markdown,
    wrap_md_as_prediction,
)
from parser_service.route_stats import (  # noqa: E402
    error_record,
    route_record,
    summarize,
    write_route_csv,
)
from parser_service.vlm_client import get_vlm_call_count  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cost estimation constants (approximate — see Bedrock pricing page)
# ---------------------------------------------------------------------------
AVG_INPUT_TOKENS_PER_CALL = 1700
AVG_OUTPUT_TOKENS_PER_CALL = 500
BEDROCK_INPUT_PRICE_PER_TOKEN = 3e-6  # USD per input token (Claude Sonnet approx)
BEDROCK_OUTPUT_PRICE_PER_TOKEN = 15e-6  # USD per output token (Claude Sonnet approx)

_AVG_COST_PER_CALL = (
    AVG_INPUT_TOKENS_PER_CALL * BEDROCK_INPUT_PRICE_PER_TOKEN
    + AVG_OUTPUT_TOKENS_PER_CALL * BEDROCK_OUTPUT_PRICE_PER_TOKEN
)


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------


def _parse_file_sync(ref: InputRef, retry_on_throttle: bool) -> dict[str, Any]:
    """Parse one file to markdown synchronously (runs in thread pool).

    Returns the ``parse_to_markdown`` result
    (``{"markdown", "page_routes", "warnings"}``).
    """
    from pathlib import Path as P

    path = P(ref.uri)
    if retry_on_throttle:
        # Retry loop at the batch layer for ThrottlingException.
        max_retries = 5
        delay = 1.0
        for attempt in range(max_retries + 1):
            try:
                return parse_to_markdown(path)
            except Exception as exc:
                exc_name = type(exc).__name__
                if "ThrottlingException" in exc_name and attempt < max_retries:
                    logger.warning(
                        "ThrottlingException on %s (attempt %d/%d); retrying in %.1fs",
                        ref.filename,
                        attempt + 1,
                        max_retries,
                        delay,
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, 30.0)
                else:
                    raise
    return parse_to_markdown(path)


async def _process_file(
    ref: InputRef,
    output_uri: str,
    emit_test_json: bool,
    semaphore: asyncio.Semaphore,
    executor: concurrent.futures.Executor,
    timeout_per_file: float | None,
    retry_on_throttle: bool,
    results: list[dict[str, Any]],
    failures: dict[str, list[dict[str, Any]]],
    route_records: list[dict[str, Any]],
) -> None:
    """Process one file with semaphore-controlled concurrency."""
    doc_id = Path(ref.filename).stem
    async with semaphore:
        start = time.monotonic()
        try:
            loop = asyncio.get_event_loop()
            coro = loop.run_in_executor(executor, _parse_file_sync, ref, retry_on_throttle)
            if timeout_per_file is not None:
                result = await asyncio.wait_for(coro, timeout=timeout_per_file)
            else:
                result = await coro

            duration = time.monotonic() - start
            vlm_calls = get_vlm_call_count()

            page_routes = result.get("page_routes", [])
            warnings = result.get("warnings", [])
            markdown = result.get("markdown", "")

            vlm_pages = sum(
                1 for r in page_routes if r.get("route") in ("vlm", "vlm-fallback-docling")
            )

            log_line = {
                "event": "file_parsed",
                "filename": ref.filename,
                "page_count": len(page_routes),
                "vlm_routed_pages": vlm_pages,
                "warning_count": len(warnings),
                "markdown_chars": len(markdown),
                "parse_duration_s": round(duration, 3),
                "vlm_call_count": vlm_calls,
            }
            logger.info(json.dumps(log_line))

            # Always write the RAG-ready Markdown.
            write_text(ref, output_uri, markdown, ext=".md")

            # Only on the eval path: also write the wrapped, schema-valid JSON the
            # doc-bench wheel grades. wrap_md_as_prediction builds the full source.
            if emit_test_json:
                prediction = wrap_md_as_prediction(markdown, Path(ref.uri))
                write_json(ref, output_uri, prediction)

            # Routing telemetry derived directly from page_routes.
            route_records.append(route_record(page_routes, doc_id=doc_id))

            # Track failures (warnings carry scope = document | page).
            doc_warns = [w for w in warnings if w.get("scope") == "document"]
            page_warns = [w for w in warnings if w.get("scope") == "page"]
            if doc_warns:
                failures.setdefault("document_failures", []).append(
                    {"filename": ref.filename, "warnings": doc_warns}
                )
            for w in page_warns:
                failures.setdefault("page_failures", []).append(
                    {"filename": ref.filename, "page_index": w.get("page_index"), "warnings": [w]}
                )

            results.append({"filename": ref.filename, "success": True, "vlm_calls": vlm_calls})

        except TimeoutError:
            duration = time.monotonic() - start
            logger.warning("Timeout parsing %s after %.1fs", ref.filename, duration)
            failures.setdefault("document_failures", []).append(
                {
                    "filename": ref.filename,
                    "warnings": [
                        {"scope": "page", "code": "page_unparseable", "message": "timeout"}
                    ],
                }
            )
            results.append({"filename": ref.filename, "success": False, "vlm_calls": 0})
            route_records.append(error_record(doc_id, "timeout"))

        except Exception as exc:
            duration = time.monotonic() - start
            logger.error("Error parsing %s: %s", ref.filename, exc)
            failures.setdefault("document_failures", []).append(
                {
                    "filename": ref.filename,
                    "warnings": [
                        {
                            "scope": "document",
                            "code": "unhandled_exception",
                            "message": str(exc),
                        }
                    ],
                }
            )
            results.append({"filename": ref.filename, "success": False, "vlm_calls": 0})
            route_records.append(error_record(doc_id, str(exc)))


# ---------------------------------------------------------------------------
# Main async runner
# ---------------------------------------------------------------------------


async def run_batch(
    input_uri: str,
    output_uri: str,
    concurrency: int,
    max_files: int | None,
    timeout_per_file: float | None,
    retry_on_throttle: bool,
    emit_test_json: bool,
) -> None:
    """Run the batch parsing pipeline."""
    refs = list(list_input_files(input_uri))
    if max_files is not None:
        refs = refs[:max_files]

    if not refs:
        logger.info("No supported files found in %s", input_uri)
        return

    logger.info("Processing %d files from %s → %s", len(refs), input_uri, output_uri)

    # Ensure output directory exists (local only).
    if not output_uri.startswith("s3://"):
        Path(output_uri).mkdir(parents=True, exist_ok=True)

    semaphore = asyncio.Semaphore(concurrency)
    results: list[dict[str, Any]] = []
    failures: dict[str, list[dict[str, Any]]] = {}
    route_records: list[dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        tasks = [
            _process_file(
                ref,
                output_uri,
                emit_test_json,
                semaphore,
                executor,
                timeout_per_file,
                retry_on_throttle,
                results,
                failures,
                route_records,
            )
            for ref in refs
        ]
        await asyncio.gather(*tasks)

    total_files = len(refs)
    succeeded = sum(1 for r in results if r["success"])
    failed = total_files - succeeded
    total_vlm_calls = sum(r.get("vlm_calls", 0) for r in results)
    estimated_cost = total_vlm_calls * _AVG_COST_PER_CALL

    route_summary = summarize(route_records)
    summary = {
        "event": "batch_complete",
        "total_files": total_files,
        "succeeded": succeeded,
        "failed": failed,
        "total_vlm_calls": total_vlm_calls,
        "estimated_cost_usd": round(estimated_cost, 6),
        "total_pages": route_summary["total_pages"],
        "vlm_routed_pages": route_summary["vlm_pages"],
        "docs_using_vlm": route_summary["used_vlm"],
        "docs_docling_kept": route_summary["docling_kept"],
    }
    logger.info(json.dumps(summary))

    # Write route_stats.csv (per-document routing breakdown).
    if not output_uri.startswith("s3://"):
        route_csv = Path(output_uri) / "route_stats.csv"
        write_route_csv(route_records, route_csv)
        logger.info("route_stats.csv written to %s", route_csv)
    else:
        import boto3  # noqa: PLC0415

        from parser_service.io_layer import S3IO  # noqa: PLC0415
        from parser_service.route_stats import FIELDNAMES  # noqa: PLC0415

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=FIELDNAMES)
        writer.writeheader()
        for rec in route_records:
            writer.writerow(rec)
        bucket, prefix = S3IO()._parse_uri(output_uri)
        key = f"{prefix.rstrip('/')}/route_stats.csv"
        boto3.client("s3").put_object(
            Bucket=bucket, Key=key, Body=buf.getvalue().encode("utf-8"), ContentType="text/csv"
        )
        logger.info("route_stats.csv written to s3://%s/%s", bucket, key)

    # Write failures.json.
    failures_data = {
        "document_failures": failures.get("document_failures", []),
        "page_failures": failures.get("page_failures", []),
    }
    if not output_uri.startswith("s3://"):
        failures_path = Path(output_uri) / "failures.json"
        failures_path.write_text(
            json.dumps(failures_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("failures.json written to %s", failures_path)
    else:
        # Write failures.json to S3.
        from parser_service.io_layer import S3IO  # noqa: PLC0415

        dummy_ref = InputRef(uri="", filename="failures.json", kind="s3")
        S3IO().write_json(dummy_ref, output_uri, failures_data)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch document parsing with Bedrock VLM.")
    parser.add_argument("--input", required=True, help="Local dir or s3://bucket/prefix")
    parser.add_argument("--output", required=True, help="Local dir or s3://bucket/prefix")
    parser.add_argument(
        "--emit-test-json",
        action="store_true",
        help=(
            "Also write the wrapped, schema-valid prediction .json (via "
            "wrap_md_as_prediction) alongside the .md, for doc-bench grading. "
            "Off by default; set by run_benchmark.sh on the eval path."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("PARSER_CONCURRENCY", "4")),
        help="Asyncio semaphore limit (default: PARSER_CONCURRENCY env or 4)",
    )
    parser.add_argument("--max-files", type=int, default=None, help="Cap on files to process")
    parser.add_argument(
        "--timeout-per-file",
        type=float,
        default=None,
        help="Seconds before a file parse is abandoned",
    )
    parser.add_argument(
        "--retry-on-throttle",
        action="store_true",
        help="Enable exponential backoff on Bedrock ThrottlingException",
    )
    args = parser.parse_args()

    asyncio.run(
        run_batch(
            input_uri=args.input,
            output_uri=args.output,
            concurrency=args.concurrency,
            max_files=args.max_files,
            timeout_per_file=args.timeout_per_file,
            retry_on_throttle=args.retry_on_throttle,
            emit_test_json=args.emit_test_json,
        )
    )


if __name__ == "__main__":
    main()
