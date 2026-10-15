"""
parse_batch.py

Batch document parsing script using asyncio + ThreadPoolExecutor.

Runs the markdown-first pipeline (``parsing.document.parse_to_markdown``): it
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
        [--budget-usd 5.00]

``--retry-on-throttle`` is DEPRECATED (no-op): transient-throttle retry now
lives inside the escalation clients (bounded exponential backoff), and an
exhausted throttle is recorded as the ``throttled`` route reason.
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
from typing import Any, NamedTuple

# Configure logging before importing parser_service.
logging.basicConfig(
    level=os.environ.get("PARSER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from parser_service.parsing.document import (  # noqa: E402
    parse_to_markdown,
    wrap_md_as_prediction,
)
from parser_service.parsing.route_stats import (  # noqa: E402
    FIELDNAMES,
    error_record,
    route_record,
    summarize,
)
from parser_service.storage.store import Store, open_store  # noqa: E402

logger = logging.getLogger(__name__)

# The input files parse_to_markdown routes (by extension), read from the top level
# of the input folder or S3 prefix.
SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".docx",
    ".xlsx",
    ".xlsm",
    ".html",
    ".htm",
}


class InputRef(NamedTuple):
    """One input file: its key in the input store and its file name."""

    key: str
    filename: str


def list_input_files(store: Store) -> list[InputRef]:
    """The supported files at the top level of ``store``, sorted by key."""
    return [
        InputRef(key=key, filename=key)
        for key in store.list_keys()
        if "/" not in key and Path(key).suffix.lower() in SUPPORTED_EXTENSIONS
    ]


def _write_text(store: Store, key: str, text: str) -> None:
    store.write(key, text.encode("utf-8"))


def _write_json(store: Store, key: str, data: Any) -> None:
    _write_text(store, key, json.dumps(data, ensure_ascii=False, indent=2))

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

# Textract AnalyzeDocument (LAYOUT + TABLES) price, per promoted page (= one
# analyze_document call per page). PROVISIONAL — this is a starting figure, NOT
# a verified ap-southeast-2 number.
#
#   - docs/escalation-engine-comparison.md cites ≈ $0.019/page for LAYOUT+TABLES
#     (used here as the starting figure).
#   - The public AWS Textract pricing page only surfaced US West (Oregon) list
#     rates when checked (Tables $0.015/page; Layout free when used with Tables,
#     so LAYOUT+TABLES ≈ $0.015/page in us-west-2). It did NOT display an
#     ap-southeast-2 (Sydney) breakdown, and Textract pricing is region-specific.
#
# ACTION REQUIRED (human/ops): verify the live ap-southeast-2 AnalyzeDocument
# LAYOUT+TABLES per-page price via the AWS Pricing Calculator / a billing export
# and replace this constant with the confirmed figure. Until then this is
# labelled PROVISIONAL and the emitted cost is an ESTIMATE (see below).
TEXTRACT_PRICE_PER_PAGE = 0.019  # PROVISIONAL — pending ap-southeast-2 verification


def _compute_cost_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Both-engine estimated-cost breakdown summed over per-file ``call_counts``.

    Bedrock leg: ``total_vlm_calls × _AVG_COST_PER_CALL`` — a per-call token model
    using AVERAGE tokens, NOT actual usage, so the number is an ESTIMATE, not a
    billed cost. Textract leg: ``total_textract_calls × TEXTRACT_PRICE_PER_PAGE``
    (one call per promoted page). ``estimated_cost_usd`` is the combined total.
    """
    total_vlm_calls = sum(r.get("vlm_calls", 0) for r in results)
    total_textract_calls = sum(r.get("textract_calls", 0) for r in results)
    bedrock_cost = total_vlm_calls * _AVG_COST_PER_CALL
    textract_cost = total_textract_calls * TEXTRACT_PRICE_PER_PAGE
    return {
        "total_vlm_calls": total_vlm_calls,
        "total_textract_calls": total_textract_calls,
        "bedrock_cost_usd": round(bedrock_cost, 6),
        "textract_cost_usd": round(textract_cost, 6),
        # Combined total (round the summed float, not the two rounded legs).
        "estimated_cost_usd": round(bedrock_cost + textract_cost, 6),
        # Explicit label: this is an ESTIMATE (Bedrock leg uses average tokens),
        # not billed cost; the Textract per-page price is PROVISIONAL.
        "cost_is_estimate": True,
    }


def _preflight_estimate(page_counts: list[int], engine: str) -> float:
    """Worst/expected-case cost bound over page counts: ``Σ page_count × per-page cost``.

    Modeled on pdfmux's ``estimate_document_cost``. Because the engine that will
    fire per page is NOT known until the gate runs (most pages are kept on
    Docling and never escalate), this is an upper bound assuming EVERY page
    escalates on ``engine`` — a worst-case bound over page counts, not a per-page
    prediction.
    """
    total_pages = sum(page_counts)
    per_page = TEXTRACT_PRICE_PER_PAGE if engine == "textract" else _AVG_COST_PER_CALL
    return round(total_pages * per_page, 6)


def _file_cost(result: dict[str, Any]) -> float:
    """Estimated spend for one completed file, from its returned ``call_counts``."""
    return (
        int(result.get("vlm_calls", 0)) * _AVG_COST_PER_CALL
        + int(result.get("textract_calls", 0)) * TEXTRACT_PRICE_PER_PAGE
    )


class BudgetTracker:
    """Document-level ``--budget-usd`` cap enforced at the document boundary.

    Running spend is accumulated from each COMPLETED file's per-engine call
    counts (the Part B cost table). ``allows_escalation()`` is checked before a
    file is dispatched; once running spend has exceeded the ceiling, escalation
    is halted for every subsequently-dispatched file (they still parse via
    Docling — no engine calls).

    COARSENESS: the cap is checked at the document boundary and files run
    concurrently, so actual spend can overshoot the ceiling by roughly one
    document's worth of in-flight escalation (files already dispatched before the
    trip finish escalating). Per-page hard capping is out of scope — a mid-page
    shared spend counter under concurrency would re-introduce the cross-thread
    race the thread-local counters removed.
    """

    def __init__(self, budget_usd: float | None) -> None:
        self.budget_usd = budget_usd
        self.running_spend = 0.0
        self.budget_exceeded = False
        self.exceeded_at_file_index: int | None = None

    def allows_escalation(self) -> bool:
        """True if a newly-dispatched file may still escalate (budget not spent)."""
        if self.budget_usd is None:
            return True
        return self.running_spend < self.budget_usd

    def record(self, result: dict[str, Any], file_index: int) -> None:
        """Add a completed file's estimated spend; trip the cap if it now exceeds."""
        self.running_spend += _file_cost(result)
        if (
            self.budget_usd is not None
            and not self.budget_exceeded
            and self.running_spend >= self.budget_usd
        ):
            self.budget_exceeded = True
            self.exceeded_at_file_index = file_index

    def summary_fields(self) -> dict[str, Any]:
        return {
            "budget_usd": self.budget_usd,
            "budget_exceeded": self.budget_exceeded,
            "budget_exceeded_at_file_index": self.exceeded_at_file_index,
            "running_spend_usd": round(self.running_spend, 6),
        }


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------


def _parse_file_sync(
    store: Store, ref: InputRef, escalate: bool = True, emit_test_json: bool = False
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Parse one file to markdown synchronously (runs in thread pool).

    The file is read through the input store's ``local_file`` (an S3 input is
    downloaded to a temporary file for the parse). Returns the ``parse_to_markdown``
    result (``{"markdown", "page_routes", "warnings"}``) and, with
    ``emit_test_json``, the wrapped prediction (built while the file is at hand).

    NOTE: transient-throttle retry now lives INSIDE the escalation clients
    (``engines.vlm.call_vlm`` / ``engines.textract.analyze_page`` wrap their inner
    AWS call in bounded exponential backoff). The old batch-layer
    ``ThrottlingException``-on-``parse_to_markdown`` retry loop is gone — the
    clients swallow the exception and return ``{"error": ...}`` (never re-raising
    the throttle), so it never reached this layer anyway.
    """
    # Only pass the additive ``escalate`` kwarg when suppressing escalation (the
    # budget-cap path). On the default path we call ``parse_to_markdown(path)``
    # positionally so the call site is byte-identical to before this feature.
    ptm_kwargs: dict[str, Any] = {} if escalate else {"escalate": False}
    with store.local_file(ref.key) as path:
        result = parse_to_markdown(path, **ptm_kwargs)
        prediction = (
            wrap_md_as_prediction(result.get("markdown", ""), path) if emit_test_json else None
        )
    return result, prediction


async def _process_file(
    ref: InputRef,
    in_store: Store,
    out_store: Store,
    emit_test_json: bool,
    semaphore: asyncio.Semaphore,
    executor: concurrent.futures.Executor,
    timeout_per_file: float | None,
    results: list[dict[str, Any]],
    failures: dict[str, list[dict[str, Any]]],
    route_records: list[dict[str, Any]],
    file_index: int = 0,
    budget: BudgetTracker | None = None,
) -> None:
    """Process one file with semaphore-controlled concurrency."""
    doc_id = Path(ref.filename).stem
    async with semaphore:
        start = time.monotonic()
        # Document-boundary budget check: decide (once, before dispatch) whether
        # this file may still escalate. Once running spend has tripped the cap,
        # remaining files parse Docling-only (no engine calls). Checked here at
        # the document boundary, NEVER inside the per-page escalation seam.
        escalate = budget.allows_escalation() if budget is not None else True
        if not escalate:
            logger.info(
                json.dumps(
                    {
                        "event": "budget_exceeded_skip_escalation",
                        "filename": ref.filename,
                        "file_index": file_index,
                    }
                )
            )
        try:
            loop = asyncio.get_event_loop()
            coro = loop.run_in_executor(
                executor, _parse_file_sync, in_store, ref, escalate, emit_test_json
            )
            if timeout_per_file is not None:
                result, prediction = await asyncio.wait_for(coro, timeout=timeout_per_file)
            else:
                result, prediction = await coro

            duration = time.monotonic() - start
            # Per-invocation, race-free counts returned by parse_to_markdown
            # (thread-local backed) — NOT a module global read.
            call_counts = result.get("call_counts", {"vlm": 0, "textract": 0})
            vlm_calls = call_counts.get("vlm", 0)
            textract_calls = call_counts.get("textract", 0)

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
                "textract_call_count": textract_calls,
            }
            logger.info(json.dumps(log_line))

            # Always write the RAG-ready Markdown.
            _write_text(out_store, f"{doc_id}.md", markdown)

            # Only on the eval path: also write the wrapped, schema-valid JSON the
            # doc-bench wheel grades. wrap_md_as_prediction builds the full source.
            if prediction is not None:
                _write_json(out_store, f"{doc_id}.json", prediction)

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

            file_result = {
                "filename": ref.filename,
                "success": True,
                "vlm_calls": vlm_calls,
                "textract_calls": textract_calls,
            }
            results.append(file_result)
            if budget is not None:
                # Update running spend at the document boundary (after completion).
                budget.record(file_result, file_index)

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
            results.append(
                {"filename": ref.filename, "success": False, "vlm_calls": 0, "textract_calls": 0}
            )
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
            results.append(
                {"filename": ref.filename, "success": False, "vlm_calls": 0, "textract_calls": 0}
            )
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
    emit_test_json: bool,
    budget_usd: float | None = None,
) -> None:
    """Run the batch parsing pipeline."""
    region = os.environ.get("AWS_REGION")
    in_store = open_store(input_uri, region=region)
    out_store = open_store(output_uri, region=region)
    refs = list_input_files(in_store)
    if max_files is not None:
        refs = refs[:max_files]

    if not refs:
        logger.info("No supported files found in %s", input_uri)
        return

    logger.info("Processing %d files from %s → %s", len(refs), input_uri, output_uri)

    semaphore = asyncio.Semaphore(concurrency)
    results: list[dict[str, Any]] = []
    failures: dict[str, list[dict[str, Any]]] = {}
    route_records: list[dict[str, Any]] = []
    # Document-level spend cap. None => disabled (unchanged behavior). The
    # allows_escalation()/record() calls run in the coroutine bodies on the
    # single asyncio loop thread, so the shared running-spend state is not racy.
    budget = BudgetTracker(budget_usd)

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        tasks = [
            _process_file(
                ref,
                in_store,
                out_store,
                emit_test_json,
                semaphore,
                executor,
                timeout_per_file,
                results,
                failures,
                route_records,
                file_index=idx,
                budget=budget,
            )
            for idx, ref in enumerate(refs)
        ]
        await asyncio.gather(*tasks)

    total_files = len(refs)
    succeeded = sum(1 for r in results if r["success"])
    failed = total_files - succeeded
    cost = _compute_cost_summary(results)

    route_summary = summarize(route_records)
    summary = {
        "event": "batch_complete",
        "total_files": total_files,
        "succeeded": succeeded,
        "failed": failed,
        "total_vlm_calls": cost["total_vlm_calls"],
        "total_textract_calls": cost["total_textract_calls"],
        # Combined both-engine ESTIMATE (not billed cost — Bedrock leg uses
        # average tokens; Textract per-page price is PROVISIONAL).
        "estimated_cost_usd": cost["estimated_cost_usd"],
        "bedrock_cost_usd": cost["bedrock_cost_usd"],
        "textract_cost_usd": cost["textract_cost_usd"],
        "cost_is_estimate": cost["cost_is_estimate"],
        "total_pages": route_summary["total_pages"],
        "vlm_routed_pages": route_summary["vlm_pages"],
        "docs_using_vlm": route_summary["used_vlm"],
        "docs_docling_kept": route_summary["docling_kept"],
    }
    # Pre-flight worst-case bound: Σ page_count × per-page cost for the configured
    # escalation engine, assuming EVERY page escalates. Because the firing engine
    # per page is unknown until the gate runs (most pages stay on Docling), this
    # is an upper bound over page counts, not a per-page prediction. Emitted for
    # visibility alongside the realized estimate.
    _engine = os.environ.get("PARSER_ESCALATION_ENGINE", "vlm")
    summary["preflight_worst_case_usd"] = _preflight_estimate(
        [route_summary["total_pages"]], engine=_engine
    )
    # Document-level budget cap outcome (budget_exceeded + the trip file index).
    summary.update(budget.summary_fields())
    logger.info(json.dumps(summary))

    # Write route_stats.csv (per-document routing breakdown) and failures.json.
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDNAMES)
    writer.writeheader()
    writer.writerows(route_records)
    _write_text(out_store, "route_stats.csv", buf.getvalue())
    logger.info("route_stats.csv written to %s", out_store.location("route_stats.csv"))

    failures_data = {
        "document_failures": failures.get("document_failures", []),
        "page_failures": failures.get("page_failures", []),
    }
    _write_json(out_store, "failures.json", failures_data)
    logger.info("failures.json written to %s", out_store.location("failures.json"))


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
        help=(
            "DEPRECATED no-op (accepted for one release). Transient-throttle "
            "retry now lives inside the escalation clients (bounded exponential "
            "backoff in the VLM and Textract engine clients); an exhausted throttle is "
            "recorded as a distinct `throttled` route reason. This flag no longer "
            "has any effect."
        ),
    )
    parser.add_argument(
        "--budget-usd",
        type=float,
        default=None,
        help=(
            "Optional document-level spend ceiling (estimated USD). Once running "
            "spend would exceed it, escalation is halted at the document boundary "
            "and remaining files parse via Docling only (no engine calls). "
            "Coarse: actual spend can overshoot by ~one document's in-flight "
            "escalation. Default: disabled. The cost is an ESTIMATE."
        ),
    )
    args = parser.parse_args()

    if args.retry_on_throttle:
        logger.warning(
            "--retry-on-throttle is DEPRECATED and now a no-op: transient-throttle "
            "retry lives inside the escalation clients (bounded exponential "
            "backoff), and exhausted throttles are recorded as the `throttled` "
            "route reason. The flag will be removed in a future release."
        )

    asyncio.run(
        run_batch(
            input_uri=args.input,
            output_uri=args.output,
            concurrency=args.concurrency,
            max_files=args.max_files,
            timeout_per_file=args.timeout_per_file,
            emit_test_json=args.emit_test_json,
            budget_usd=args.budget_usd,
        )
    )


if __name__ == "__main__":
    main()
