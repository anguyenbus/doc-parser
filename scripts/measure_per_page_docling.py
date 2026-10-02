"""
measure_per_page_docling.py

Measure one whole-file Docling run against Docling run on physically split
one-page PDF files (roadmap item 1 — a measurement that gates item 2, NOT a
product feature). For each input PDF it reports wall time, peak memory, the cost
of the split step, and whether the per-page markdown and the quality-gate
outcome match between the two configurations.

Usage:
    uv run python scripts/measure_per_page_docling.py <pdf> [<pdf> ...] \
        --output-dir <dir> [--runs 3] [--keep-split]

Configurations (Docling only — no ``parse_to_markdown``, no VLM, no Textract, no
AWS call; ``PARSER_SCAN_FASTPATH`` is forced off in every measuring process):
  - whole : one ``convert()`` on the original PDF.
  - split : the PDF is split with pypdf into one-page files and ``convert()`` runs
            once per file, in original page order, on ONE reused converter.

Every run of every configuration executes in a fresh child process started by
this script (a hidden ``--child`` sub-command run through ``sys.executable``), so
peak RSS is never inherited from another configuration. Each configuration is run
``--runs`` times (default 3); the median is reported and the raw values are kept.

Output: a readable summary on stdout and ``<output-dir>/<pdf-stem>.json`` with the
raw figures. One-page files go to a temporary directory OUTSIDE the repo and are
removed at the end unless ``--keep-split`` is given.

Exit codes: 0 = measured (review flags alone do not change this); 1 = error
(missing/unreadable input, a child run failed, a one-page assertion failed);
2 = measured, but a gate action/reason differs between the configurations
(blocking pending the user's call — see the summary).

The split uses pypdf for the MEASUREMENT only; it does not bind roadmap item 2.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.metadata
import json
import logging
import math
import os
import platform
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

logging.basicConfig(
    level=os.environ.get("PARSER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

# None of these imports loads Docling (``_document_converter`` imports it lazily),
# so input validation in ``main`` still happens before any Docling import or work.
from parser_service.markdown_pipeline import (  # noqa: E402
    _document_converter,
    _elements_by_page,
    _render_page_markdown,
    _scan_fastpath_enabled,
)
from parser_service.quality_gate import evaluate_page  # noqa: E402
from parser_service.render import classify_for_fastpath, text_layer_tokens  # noqa: E402

logger = logging.getLogger(__name__)

# Review-flag lines (flags, NOT failures — the user makes the go/no-go call).
_WALL_RATIO_FLAG = 1.25  # split conversion total / whole-file time
_RSS_RATIO_FLAG = 1.10  # split peak RSS / whole-file peak RSS ("about 10%")

# Mirrors ``_pdf_to_markdown``: a page with no rendered Docling markdown is routed
# to escalation with this reason and the gate is never called.
_NO_DOCLING_CONTENT = "no_docling_content"
_PROMOTE = "promote_to_vlm"

_CONFIGS = ("whole", "split")
_EXIT_ERROR = 1
_EXIT_BLOCKING = 2

# Fallback warm-up sources, used only when the input itself has no page of a class
# (e.g. an all-digital input still needs a scanned page to exercise the OCR model).
_WARMUP_FALLBACK = {
    "digital": REPO_ROOT / "tests" / "fixtures" / "digital_simple.pdf",
    "scanned": REPO_ROOT / "tests" / "fixtures" / "scanned.pdf",
}


# ---------------------------------------------------------------------------
# Split step (pypdf) — shared by the measurement and tests/test_per_page_split_parity.py
# ---------------------------------------------------------------------------


# NamedTuples, not dataclasses: tests load this file with ``_load_script``, which does not
# register the module in ``sys.modules`` — a dataclass with postponed annotations needs it.
class SplitResult(NamedTuple):
    """Outcome of splitting one PDF into one-page files.

    ``pages`` is the explicit, ordered ORIGINAL-page-index → file mapping. It is the
    ONLY source of the original page index: a one-page file's Docling result always
    numbers its page 1 (index 0), so the index is never read back from the result.
    """

    pages: dict[int, Path]
    seconds: float
    original_bytes: int
    split_bytes: int
    library: str
    library_version: str


def split_pdf(pdf_path: Path, out_dir: Path) -> SplitResult:
    """Split ``pdf_path`` into one-page PDFs under ``out_dir`` (one writer per page).

    Each file holds a single page of the source reader, copied by pypdf without
    re-encoding its streams, and is named by the zero-padded original page index.
    ``out_dir`` must be outside the repo (one-page files never land in the tree).
    """
    import pypdf  # local import; only needed for PDFs (as in render.py)
    from pypdf import PdfReader, PdfWriter

    if out_dir.resolve().is_relative_to(REPO_ROOT):
        raise ValueError(f"split output directory must be outside the repo: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    reader = PdfReader(str(pdf_path))
    page_count = len(reader.pages)
    width = max(4, len(str(max(page_count - 1, 0))))
    pages: dict[int, Path] = {}
    for idx in range(page_count):
        writer = PdfWriter()
        writer.add_page(reader.pages[idx])
        target = out_dir / f"page_{idx:0{width}d}.pdf"
        with target.open("wb") as fh:
            writer.write(fh)
        pages[idx] = target
    seconds = time.perf_counter() - t0

    return SplitResult(
        pages=pages,
        seconds=seconds,
        original_bytes=pdf_path.stat().st_size,
        split_bytes=sum(p.stat().st_size for p in pages.values()),
        library="pypdf",
        library_version=pypdf.__version__,
    )


# ---------------------------------------------------------------------------
# Per-page evaluation — shared by the measurement and the pytest guard
# ---------------------------------------------------------------------------


class PageOutcome(NamedTuple):
    """One page's Docling-only markdown and gate outcome (before any escalation)."""

    markdown: str
    action: str
    reason: str | None
    layer: int | None
    gate_called: bool
    element_count: int


def _own_confidence_view(result: Any, local_index: int) -> Any:
    """A stand-in conversion result exposing ONLY this page's own confidence entry.

    Docling keys ``confidence.pages`` by 1-based page number, so the page's own key
    is ``local_index + 1``. With only that key present, the gate's unchanged lookup
    (``get(page_no) or get(page_no + 1)``) can resolve nothing but the page's own
    entry — this is how a lookup-caused gate difference is told apart from one
    caused by splitting. Production code is called, not edited.
    """
    pages_conf = getattr(getattr(result, "confidence", None), "pages", None) or {}
    own = pages_conf.get(local_index + 1)
    own_only = {} if own is None else {local_index + 1: own}
    return SimpleNamespace(confidence=SimpleNamespace(pages=own_only))


def evaluate_result_page(
    result: Any,
    local_index: int,
    page_text_layer_tokens: int | None,
    own_confidence_only: bool = False,
) -> PageOutcome:
    """Render one page of a conversion result and take its gate outcome.

    Mirrors the per-page loop of ``_pdf_to_markdown``: empty rendered markdown is
    recorded as ``no_docling_content`` WITHOUT calling the gate; otherwise the
    outcome is ``evaluate_page``'s action, reason and layer. ``local_index`` is the
    0-based index WITHIN ``result`` (always 0 for a one-page file) and
    ``page_text_layer_tokens`` must come from the same file as ``result``.

    ``own_confidence_only`` runs the same unchanged gate against a view of the
    result holding only this page's own confidence entry (see ``_own_confidence_view``).
    """
    container: dict[str, Any] = {"warnings": [], "elements": []}
    page_elems = _elements_by_page(result.document, container).get(local_index, [])
    markdown = _render_page_markdown(page_elems)
    if not markdown:
        return PageOutcome(markdown, _PROMOTE, _NO_DOCLING_CONTENT, None, False, len(page_elems))

    gate_input = _own_confidence_view(result, local_index) if own_confidence_only else result
    decision = evaluate_page(
        local_index,
        gate_input,
        page_elems,
        page_text_layer_tokens=page_text_layer_tokens,
    )
    return PageOutcome(
        markdown, decision.action, decision.reason, decision.layer, True, len(page_elems)
    )


def assert_one_page_result(result: Any) -> None:
    """Check a split file's result: exactly one page, numbered 1, all elements at index 0.

    Passing the ORIGINAL index with a one-page result would miss both gate lookups
    without an error, so this is asserted for every split file, not assumed.
    """
    page_numbers = sorted(result.document.pages.keys())
    if page_numbers != [1]:
        raise AssertionError(f"one-page result has page numbers {page_numbers}, expected [1]")
    container: dict[str, Any] = {"warnings": [], "elements": []}
    stray = sorted(k for k in _elements_by_page(result.document, container) if k != 0)
    if stray:
        raise AssertionError(f"one-page result has elements at page_index {stray}, expected 0")


def _grade(entry: Any, name: str) -> str | None:
    """Docling grade as a plain lowercase string (``QualityGrade`` is a str-Enum)."""
    grade = getattr(entry, name, None)
    if grade is None:
        return None
    value = getattr(grade, "value", None)
    return value if isinstance(value, str) else str(grade)


def _score(entry: Any, name: str) -> float | None:
    """A Docling confidence score, with NaN mapped to None (strict-JSON safe)."""
    value = getattr(entry, name, None)
    if not isinstance(value, float) or math.isnan(value):
        return None
    return round(value, 6)


def _confidence_entry(entry: Any) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {
        "low_grade": _grade(entry, "low_grade"),
        "mean_grade": _grade(entry, "mean_grade"),
        "parse_score": _score(entry, "parse_score"),
        "layout_score": _score(entry, "layout_score"),
        "table_score": _score(entry, "table_score"),
        "ocr_score": _score(entry, "ocr_score"),
    }


def confidence_lookup(result: Any, page_no: int) -> dict[str, Any]:
    """Record which key the gate's confidence lookup resolves for ``page_no``.

    Replays ``quality_gate._layer1_decision``'s line
    ``pages_conf.get(page_no) or pages_conf.get(page_no + 1)`` (0-based ``page_no``)
    without changing it. Docling 2.96.0 keys ``confidence.pages`` by 1-based page
    number (measured — see results.md), so the page's OWN key is ``page_no + 1``;
    a lookup that resolves ``page_no`` reads the PREVIOUS page's entry.
    """
    pages_conf = getattr(getattr(result, "confidence", None), "pages", None) or {}
    first = pages_conf.get(page_no)
    second = pages_conf.get(page_no + 1)
    resolved_key: int | None
    if first:
        resolved_key = page_no
    elif second is not None:
        resolved_key = page_no + 1
    else:
        resolved_key = None

    own_key = page_no + 1
    if resolved_key is None:
        verdict = "none"
    elif resolved_key == own_key:
        verdict = "own"
    else:
        verdict = "other_page"
    return {
        "page_no": page_no,
        "keys": sorted(pages_conf.keys()),
        "resolved_key": resolved_key,
        "own_key": own_key,
        "verdict": verdict,
        # A present-but-falsy entry at ``page_no`` would make the ``or`` fall through.
        "falsy_fallthrough": first is not None and not first,
        "resolved_entry": _confidence_entry(pages_conf.get(resolved_key))
        if resolved_key is not None
        else None,
        "own_entry": _confidence_entry(pages_conf.get(own_key)),
    }


# ---------------------------------------------------------------------------
# Child process — one run of one configuration
# ---------------------------------------------------------------------------


def _peak_rss_kb() -> int:
    """Peak RSS of this process in KILOBYTES (Linux ``ru_maxrss`` unit)."""
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _vm_hwm_kb() -> int | None:
    """``VmHWM`` from /proc/self/status in kilobytes (cross-check for ``ru_maxrss``)."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _page_record(result: Any, local_index: int, tokens: int | None) -> dict[str, Any]:
    """Everything recorded for one page of one conversion result."""
    outcome = evaluate_result_page(result, local_index, tokens)
    own = evaluate_result_page(result, local_index, tokens, own_confidence_only=True)
    return {
        "markdown": outcome.markdown,
        "markdown_sha256": hashlib.sha256(outcome.markdown.encode("utf-8")).hexdigest(),
        "action": outcome.action,
        "reason": outcome.reason,
        "layer": outcome.layer,
        "gate_called": outcome.gate_called,
        "element_count": outcome.element_count,
        "text_layer_tokens": tokens,
        "lookup": confidence_lookup(result, local_index),
        # The unchanged gate, shown only this page's own confidence entry.
        "own_confidence_gate": {"action": own.action, "reason": own.reason, "layer": own.layer},
    }


def _run_child(job_path: Path) -> None:
    """Run one configuration once and write its figures as JSON (hidden sub-command)."""
    job = json.loads(job_path.read_text(encoding="utf-8"))
    config: str = job["config"]

    # Forced off whatever the caller's environment holds.
    os.environ["PARSER_SCAN_FASTPATH"] = "0"
    if _scan_fastpath_enabled():
        raise RuntimeError("PARSER_SCAN_FASTPATH is still on in the measuring process")

    rss_start_kb = _peak_rss_kb()

    # ONE converter per process, kept; the split configuration reuses it for every page.
    t0 = time.perf_counter()
    converter = _document_converter()
    construct_seconds = time.perf_counter() - t0

    # Warm-up: Docling loads its models at FIRST conversion, not at construction, so
    # one digital and one scanned one-page file are converted before anything is timed.
    warmup: list[dict[str, Any]] = []
    for item in job["warmup"]:
        t0 = time.perf_counter()
        converter.convert(item["path"])
        warmup.append({**item, "seconds": time.perf_counter() - t0})
    rss_after_warmup_kb = _peak_rss_kb()

    pages: list[dict[str, Any]] = []
    out: dict[str, Any] = {"config": config}
    if config == "whole":
        input_path = Path(job["input"])
        t0 = time.perf_counter()
        result = converter.convert(str(input_path))
        out["convert_seconds"] = time.perf_counter() - t0
        out["peak_rss_after_convert_kb"] = _peak_rss_kb()

        tokens = text_layer_tokens(input_path)
        # 0-based indices in page order (Docling keys ``doc.pages`` 1-based).
        for page_idx in sorted(k - 1 for k in result.document.pages.keys()):
            record = _page_record(result, page_idx, tokens.get(page_idx))
            record["page_index"] = page_idx
            pages.append(record)
        out["confidence_keys"] = sorted(result.confidence.pages.keys())
        out["document_page_numbers"] = sorted(result.document.pages.keys())
        out["status"] = str(result.status)
    else:
        total = 0.0
        key_sets: set[tuple[int, ...]] = set()
        for original_index, file_name in job["split_files"]:
            one_page = Path(file_name)
            t0 = time.perf_counter()
            result = converter.convert(str(one_page))
            seconds = time.perf_counter() - t0
            total += seconds

            assert_one_page_result(result)
            # Local index 0 and this file's OWN result and token count; the record
            # is then stored under the original index from the split mapping.
            record = _page_record(result, 0, text_layer_tokens(one_page).get(0))
            record["page_index"] = original_index
            record["convert_seconds"] = seconds
            record["status"] = str(result.status)
            pages.append(record)
            key_sets.add(tuple(sorted(result.confidence.pages.keys())))
            del result
        out["convert_seconds"] = total
        out["peak_rss_after_convert_kb"] = _peak_rss_kb()
        out["confidence_keys"] = sorted(list(k) for k in key_sets)
        out["one_page_assertions_held"] = len(pages)

    out.update(
        {
            "pages": pages,
            "construct_seconds": construct_seconds,
            "warmup": warmup,
            "model_load_seconds": construct_seconds + sum(w["seconds"] for w in warmup),
            "rss_unit": "kB (ru_maxrss on Linux)",
            "rss_start_kb": rss_start_kb,
            "rss_after_warmup_kb": rss_after_warmup_kb,
            "peak_rss_kb": _peak_rss_kb(),
            "vm_hwm_kb": _vm_hwm_kb(),
            "scan_fastpath_env": os.environ["PARSER_SCAN_FASTPATH"],
            "versions": _versions(),
        }
    )
    Path(job["result_path"]).write_text(json.dumps(out), encoding="utf-8")


# ---------------------------------------------------------------------------
# Parent — orchestration, parity, flags, report
# ---------------------------------------------------------------------------


def _versions() -> dict[str, str]:
    """Versions actually installed in the running environment."""
    names = ("docling", "docling-core", "pypdf", "pypdfium2", "torch", "rapidocr")
    found: dict[str, str] = {}
    for name in names:
        try:
            found[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            found[name] = "not installed"
    found["python"] = platform.python_version()
    return found


def _machine() -> dict[str, Any]:
    mem_total_kb: int | None = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_total_kb = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        mem_total_kb = None
    return {
        "cpu_count": os.cpu_count(),
        "mem_total_mb": round(mem_total_kb / 1024) if mem_total_kb else None,
        "platform": platform.platform(),
        "load_average_at_start": list(os.getloadavg()),
    }


def _input_error(pdf_path: Path) -> str | None:
    """Why ``pdf_path`` cannot be measured, or None. No Docling import or work here."""
    if not pdf_path.is_file():
        return f"file not found: {pdf_path}"
    try:
        from pypdf import PdfReader  # local import; only needed for PDFs

        if len(PdfReader(str(pdf_path)).pages) == 0:
            return f"PDF has no pages: {pdf_path}"
    except Exception as exc:  # noqa: BLE001 — any read failure is "unreadable"
        return f"unreadable PDF: {pdf_path} ({exc})"
    return None


def _warmup_files(
    split: SplitResult, classes: dict[int, str], work_dir: Path
) -> list[dict[str, Any]]:
    """One digital and one scanned one-page file to warm the layout and OCR models.

    Taken from the input's own split files where the class exists; otherwise from
    the first page of a repo fixture, so an all-digital input still loads OCR.
    """
    chosen: list[dict[str, Any]] = []
    for cls in ("digital", "scanned"):
        own = next((idx for idx in split.pages if classes[idx] == cls), None)
        if own is not None:
            chosen.append(
                {
                    "class": cls,
                    "path": str(split.pages[own]),
                    "source": f"input page index {own}",
                    "page_index": own,
                }
            )
            continue
        fallback = _WARMUP_FALLBACK[cls]
        if not fallback.is_file():
            chosen.append(
                {
                    "class": cls,
                    "path": "",
                    "source": "none available (not warmed)",
                    "page_index": None,
                }
            )
            continue
        first_page = split_pdf(fallback, work_dir / f"warmup_{cls}").pages[0]
        chosen.append(
            {
                "class": cls,
                "path": str(first_page),
                "source": f"{fallback.relative_to(REPO_ROOT)} page index 0",
                "page_index": None,
            }
        )
    return [c for c in chosen if c["path"]] + [c for c in chosen if not c["path"]]


def _spawn(job: dict[str, Any], work_dir: Path, tag: str) -> dict[str, Any]:
    """Run one child process and return its JSON figures."""
    job_path = work_dir / f"job_{tag}.json"
    result_path = work_dir / f"result_{tag}.json"
    job_path.write_text(json.dumps({**job, "result_path": str(result_path)}), encoding="utf-8")

    env = dict(os.environ)
    env["PARSER_SCAN_FASTPATH"] = "0"
    proc = subprocess.run(  # noqa: S603 — re-invokes this script only
        [sys.executable, str(Path(__file__).resolve()), "--child", str(job_path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not result_path.is_file():
        tail = "\n".join(proc.stderr.splitlines()[-25:])
        raise RuntimeError(f"child run {tag} failed (exit {proc.returncode}):\n{tail}")
    loaded: dict[str, Any] = json.loads(result_path.read_text(encoding="utf-8"))
    return loaded


def _gate_tuple(record: dict[str, Any]) -> tuple[str, str | None, int | None]:
    return (record["action"], record["reason"], record["layer"])


def _own_gate_tuple(record: dict[str, Any]) -> tuple[str, str | None, int | None]:
    own = record["own_confidence_gate"]
    return (own["action"], own["reason"], own["layer"])


def _gate_dict(outcome: tuple[str, str | None, int | None]) -> dict[str, Any]:
    return {"action": outcome[0], "reason": outcome[1], "layer": outcome[2]}


def _run_disagreements(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pages where a later run of a configuration differs from its first run."""
    found: list[dict[str, Any]] = []
    first = {p["page_index"]: p for p in runs[0]["pages"]}
    for run_no, run in enumerate(runs[1:], start=2):
        for page in run["pages"]:
            base = first.get(page["page_index"])
            if base is None:
                found.append({"run": run_no, "page_index": page["page_index"], "what": "new page"})
                continue
            if page["markdown_sha256"] != base["markdown_sha256"]:
                found.append({"run": run_no, "page_index": page["page_index"], "what": "markdown"})
            if _gate_tuple(page) != _gate_tuple(base):
                found.append({"run": run_no, "page_index": page["page_index"], "what": "gate"})
    return found


def _parity(
    whole: dict[str, Any], split: dict[str, Any], classes: dict[int, str]
) -> dict[str, Any]:
    """Per-page markdown and gate parity, from ONE run of each configuration."""
    whole_pages = {p["page_index"]: p for p in whole["pages"]}
    split_pages = {p["page_index"]: p for p in split["pages"]}

    identical: list[int] = []
    differing: list[dict[str, Any]] = []
    action_diffs: list[dict[str, Any]] = []
    reason_diffs: list[dict[str, Any]] = []
    own_entry_diffs: list[dict[str, Any]] = []
    token_mismatches: list[dict[str, Any]] = []
    missing = sorted(set(whole_pages) ^ set(split_pages))

    for idx in sorted(set(whole_pages) & set(split_pages)):
        w, s = whole_pages[idx], split_pages[idx]
        if w["markdown"] == s["markdown"]:
            identical.append(idx)
        else:
            diff = difflib.unified_diff(
                w["markdown"].splitlines(),
                s["markdown"].splitlines(),
                fromfile=f"whole/page_index_{idx}",
                tofile=f"split/page_index_{idx}",
                lineterm="",
            )
            differing.append(
                {
                    "page_index": idx,
                    "class": classes[idx],
                    "similarity": difflib.SequenceMatcher(
                        None, w["markdown"], s["markdown"]
                    ).ratio(),
                    "whole_chars": len(w["markdown"]),
                    "split_chars": len(s["markdown"]),
                    "unified_diff": "\n".join(diff),
                }
            )

        if w["text_layer_tokens"] != s["text_layer_tokens"]:
            token_mismatches.append(
                {
                    "page_index": idx,
                    "whole": w["text_layer_tokens"],
                    "split": s["text_layer_tokens"],
                }
            )

        as_is, own, split_gate = _gate_tuple(w), _own_gate_tuple(w), _gate_tuple(s)
        # What remains when the whole-file gate sees the page's OWN confidence entry:
        # a difference here is caused by splitting, not by the line-93 lookup.
        if own != split_gate:
            own_entry_diffs.append(
                {
                    "page_index": idx,
                    "class": classes[idx],
                    "whole_own_confidence": _gate_dict(own),
                    "split": _gate_dict(split_gate),
                }
            )
        if as_is == split_gate:
            continue
        if own == split_gate:
            cause = "confidence_lookup"
        elif as_is == own:
            cause = "split"
        else:
            cause = "confidence_lookup_and_split"
        entry = {
            "page_index": idx,
            "class": classes[idx],
            "whole": _gate_dict(as_is),
            "split": _gate_dict(split_gate),
            "whole_own_confidence": _gate_dict(own),
            "whole_lookup_resolved_key": w["lookup"]["resolved_key"],
            "whole_lookup_own_key": w["lookup"]["own_key"],
            "cause": cause,
        }
        (action_diffs if as_is[0] != split_gate[0] else reason_diffs).append(entry)

    return {
        "identical_pages": identical,
        "differing_pages": differing,
        "pages_missing_from_one_configuration": missing,
        "gate_action_differences": action_diffs,
        "gate_reason_only_differences": reason_diffs,
        "gate_differences_with_own_confidence": own_entry_diffs,
        "text_layer_mismatches": token_mismatches,
    }


def _lookup_flags(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Pages whose confidence lookup resolves no entry or another page's entry."""
    return [
        {
            "page_index": p["page_index"],
            "verdict": p["lookup"]["verdict"],
            "resolved_key": p["lookup"]["resolved_key"],
            "own_key": p["lookup"]["own_key"],
            "resolved_entry": p["lookup"]["resolved_entry"],
            "own_entry": p["lookup"]["own_entry"],
        }
        for p in run["pages"]
        if p["lookup"]["verdict"] != "own"
    ]


def _by_class(
    classes: dict[int, str],
    whole: dict[str, Any],
    split_runs: list[dict[str, Any]],
    parity: dict[str, Any],
) -> dict[str, Any]:
    """Digital and scanned pages reported separately (split per-page times, parity, gate)."""
    differing = {d["page_index"] for d in parity["differing_pages"]}
    whole_pages = {p["page_index"]: p for p in whole["pages"]}
    split_pages = {p["page_index"]: p for p in split_runs[0]["pages"]}
    per_page_median = {
        idx: statistics.median(
            next(p["convert_seconds"] for p in run["pages"] if p["page_index"] == idx)
            for run in split_runs
        )
        for idx in classes
    }

    report: dict[str, Any] = {}
    for cls in ("digital", "scanned"):
        indices = [idx for idx in sorted(classes) if classes[idx] == cls]
        times = [per_page_median[idx] for idx in indices]

        def _counts(pages: dict[int, dict[str, Any]], members: list[int]) -> dict[str, int]:
            tally = Counter(
                f"{pages[i]['action']} | {pages[i]['reason']}" for i in members if i in pages
            )
            return dict(sorted(tally.items()))

        report[cls] = {
            "page_indices": indices,
            "page_count": len(indices),
            "split_per_page_seconds_median": {str(i): per_page_median[i] for i in indices},
            "split_seconds_sum": sum(times),
            "split_seconds_mean_per_page": statistics.mean(times) if times else None,
            "identical_pages": len([i for i in indices if i not in differing]),
            "differing_pages": len([i for i in indices if i in differing]),
            "gate_outcomes_whole": _counts(whole_pages, indices),
            "gate_outcomes_split": _counts(split_pages, indices),
        }
    return report


def _net_model_load(run: dict[str, Any]) -> float:
    """Model load of one SPLIT run with the warm-up pages' own conversion work removed.

    A warm-up page taken from the input is converted again in the timed section of
    the same process, so ``warm-up time - timed time`` is what the first conversion
    cost on top of an ordinary one. A fallback fixture page has no timed counterpart
    and is counted in full.
    """
    timed = {p["page_index"]: p["convert_seconds"] for p in run["pages"]}
    net: float = run["construct_seconds"]
    for item in run["warmup"]:
        net += item["seconds"] - timed.get(item["page_index"], 0.0)
    return net


def _measure_input(pdf_path: Path, work_dir: Path, runs: int) -> dict[str, Any]:
    """Split, run every configuration ``runs`` times in fresh children, and compare."""
    machine = _machine()  # load average read BEFORE any child starts
    split = split_pdf(pdf_path, work_dir / "split")
    page_count = len(split.pages)

    qualifies: dict[int, bool] = classify_for_fastpath(pdf_path)["page_qualifies"]
    if sorted(qualifies) != sorted(split.pages):
        raise RuntimeError(
            f"page classification covers pages {sorted(qualifies)} but the split has "
            f"{sorted(split.pages)}"
        )
    classes = {idx: "scanned" if qualifies[idx] else "digital" for idx in sorted(qualifies)}

    warmup = _warmup_files(split, classes, work_dir)
    usable_warmup = [w for w in warmup if w["path"]]
    jobs: dict[str, dict[str, Any]] = {
        "whole": {"config": "whole", "input": str(pdf_path), "warmup": usable_warmup},
        "split": {
            "config": "split",
            "input": str(pdf_path),
            "warmup": usable_warmup,
            # Explicit ordered [original index, file] pairs — the only index source.
            "split_files": [[idx, str(path)] for idx, path in split.pages.items()],
        },
    }

    # Interleaved (whole, split, whole, split, ...) so slow drift hits both alike.
    by_config: dict[str, list[dict[str, Any]]] = {name: [] for name in _CONFIGS}
    for run_no in range(1, runs + 1):
        for name in _CONFIGS:
            logger.info("%s: %s run %d/%d", pdf_path.name, name, run_no, runs)
            by_config[name].append(_spawn(jobs[name], work_dir, f"{name}_{run_no}"))

    whole_runs, split_runs = by_config["whole"], by_config["split"]
    whole_seconds = [r["convert_seconds"] for r in whole_runs]
    split_seconds = [r["convert_seconds"] for r in split_runs]
    whole_rss = [r["peak_rss_kb"] for r in whole_runs]
    split_rss = [r["peak_rss_kb"] for r in split_runs]
    load_seconds = [r["model_load_seconds"] for r in whole_runs + split_runs]

    whole_median = statistics.median(whole_seconds)
    split_median = statistics.median(split_seconds)
    whole_rss_median = statistics.median(whole_rss)
    split_rss_median = statistics.median(split_rss)
    wall_ratio = split_median / whole_median
    rss_ratio = split_rss_median / whole_rss_median

    # Parity is taken from the FIRST run of each configuration.
    parity = _parity(whole_runs[0], split_runs[0], classes)
    disagreements = {
        "whole": _run_disagreements(whole_runs),
        "split": _run_disagreements(split_runs),
    }

    review_flags: list[str] = []
    if wall_ratio > _WALL_RATIO_FLAG:
        review_flags.append(
            f"split conversion total is {wall_ratio:.2f}x whole-file time "
            f"(review line {_WALL_RATIO_FLAG}x)"
        )
    if rss_ratio > _RSS_RATIO_FLAG:
        review_flags.append(
            f"split peak RSS is {rss_ratio:.3f}x whole-file peak "
            f"(review line about {_RSS_RATIO_FLAG}x)"
        )

    return {
        "input": str(pdf_path),
        "page_count": page_count,
        "page_count_docling_whole": len(whole_runs[0]["pages"]),
        "page_classes": {str(idx): cls for idx, cls in classes.items()},
        "runs_per_configuration": runs,
        "run_order": "interleaved: whole, split, whole, split, ...",
        "machine": machine,
        "versions": whole_runs[0]["versions"],
        "scan_fastpath_env_in_children": sorted(
            {r["scan_fastpath_env"] for r in whole_runs + split_runs}
        ),
        "split": {
            **{k: v for k, v in split._asdict().items() if k != "pages"},
            "files": {str(idx): path.name for idx, path in split.pages.items()},
            "size_ratio": split.split_bytes / split.original_bytes,
            "timing_note": "single timed split in the parent process (not repeated)",
        },
        "model_load": {
            "seconds_median": statistics.median(load_seconds),
            "seconds_raw": load_seconds,
            "construct_seconds_raw": [r["construct_seconds"] for r in whole_runs + split_runs],
            "net_of_warmup_page_work_seconds_median": statistics.median(
                _net_model_load(r) for r in split_runs
            ),
            "net_of_warmup_page_work_seconds_raw": [_net_model_load(r) for r in split_runs],
            "warmup_files": warmup,
            "how_obtained": (
                "per child: time of _document_converter() (includes importing Docling) plus "
                "the time of the untimed warm-up conversions of one digital and one scanned "
                "one-page file; median over all child runs of both configurations. It "
                "therefore includes the conversion work of the warm-up pages themselves. "
                "net_of_warmup_page_work removes that work (split runs only): for a warm-up "
                "page taken from the input, the time of the same page in the timed section "
                "is subtracted."
            ),
        },
        "timing": {
            "whole_seconds_raw": whole_seconds,
            "whole_seconds_median": whole_median,
            "split_total_seconds_raw": split_seconds,
            "split_total_seconds_median": split_median,
            "ratio_split_over_whole": wall_ratio,
            "ratio_split_plus_split_step_over_whole": (split_median + split.seconds) / whole_median,
        },
        "peak_rss": {
            "unit": "kB as reported by resource.getrusage(RUSAGE_SELF).ru_maxrss on Linux",
            "whole_kb_raw": whole_rss,
            "split_kb_raw": split_rss,
            "whole_mb_median": whole_rss_median / 1024,
            "split_mb_median": split_rss_median / 1024,
            "ratio_split_over_whole": rss_ratio,
            "whole_after_warmup_kb_raw": [r["rss_after_warmup_kb"] for r in whole_runs],
            "split_after_warmup_kb_raw": [r["rss_after_warmup_kb"] for r in split_runs],
        },
        "by_class": _by_class(classes, whole_runs[0], split_runs, parity),
        "confidence_lookup": {
            "whole_keys": whole_runs[0]["confidence_keys"],
            "split_key_sets": split_runs[0]["confidence_keys"],
            "whole_flagged_pages": _lookup_flags(whole_runs[0]),
            "split_flagged_pages": _lookup_flags(split_runs[0]),
        },
        "one_page_assertions_held": [r["one_page_assertions_held"] for r in split_runs],
        "parity": parity,
        "run_disagreements": disagreements,
        "review_flags": review_flags,
        "blocking": bool(
            parity["gate_action_differences"] or parity["gate_reason_only_differences"]
        ),
        "runs": by_config,
    }


def _print_summary(report: dict[str, Any]) -> None:
    timing, rss, split, parity = (
        report["timing"],
        report["peak_rss"],
        report["split"],
        report["parity"],
    )
    lookup = report["confidence_lookup"]

    def _fmt(values: list[float]) -> str:
        return ", ".join(f"{v:.3f}" for v in values)

    print(f"\n=== {report['input']} ===")
    print(
        f"pages found: {report['page_count']} (Docling whole-file: "
        f"{report['page_count_docling_whole']})"
    )
    for cls, info in report["by_class"].items():
        print(f"  {cls}: {info['page_count']} pages, indices {info['page_indices']}")
    versions = report["versions"]
    print(f"versions: docling {versions['docling']}, pypdf {versions['pypdf']}")
    load = report["model_load"]
    print(
        f"model load (construct + warm-up): {load['seconds_median']:.2f} s "
        f"(net of the warm-up pages' own work: "
        f"{load['net_of_warmup_page_work_seconds_median']:.2f} s)"
    )
    print(
        f"whole-file convert: median {timing['whole_seconds_median']:.3f} s "
        f"(raw {_fmt(timing['whole_seconds_raw'])})"
    )
    print(
        f"split convert total: median {timing['split_total_seconds_median']:.3f} s "
        f"(raw {_fmt(timing['split_total_seconds_raw'])})"
    )
    print(
        f"ratio split/whole: {timing['ratio_split_over_whole']:.3f}  "
        f"(with split step: {timing['ratio_split_plus_split_step_over_whole']:.3f})"
    )
    print(
        f"peak RSS: whole {rss['whole_mb_median']:.0f} MB, split {rss['split_mb_median']:.0f} MB "
        f"(ratio {rss['ratio_split_over_whole']:.3f}; ru_maxrss, kB on Linux)"
    )
    print(
        f"split step ({split['library']} {split['library_version']}): {split['seconds']:.4f} s, "
        f"{split['original_bytes']} B -> {split['split_bytes']} B "
        f"(ratio {split['size_ratio']:.3f})"
    )

    for cls, info in report["by_class"].items():
        if not info["page_count"]:
            continue
        print(
            f"  [{cls}] split per-page median s: "
            f"{_fmt(list(info['split_per_page_seconds_median'].values()))} "
            f"(mean {info['split_seconds_mean_per_page']:.3f})"
        )
        print(
            f"  [{cls}] markdown identical {info['identical_pages']}, "
            f"differing {info['differing_pages']}"
        )
        print(f"  [{cls}] gate whole: {info['gate_outcomes_whole']}")
        print(f"  [{cls}] gate split: {info['gate_outcomes_split']}")

    print(f"confidence.pages keys: whole {lookup['whole_keys']}, split {lookup['split_key_sets']}")
    for name in _CONFIGS:
        for flagged in lookup[f"{name}_flagged_pages"]:
            print(
                f"  LOOKUP [{name}] page index {flagged['page_index']}: resolves key "
                f"{flagged['resolved_key']} ({flagged['verdict']}), own key is "
                f"{flagged['own_key']}"
            )
    for mismatch in parity["text_layer_mismatches"]:
        print(
            f"  TEXT-LAYER MISMATCH page index {mismatch['page_index']}: "
            f"whole {mismatch['whole']} vs split {mismatch['split']}"
        )
    if not parity["text_layer_mismatches"]:
        print("text-layer token counts: whole[k] == one-page[0] for every page")

    for page in parity["differing_pages"]:
        print(
            f"  MARKDOWN DIFFERS page index {page['page_index']} ({page['class']}): "
            f"similarity {page['similarity']:.4f}"
        )
        print("    " + page["unified_diff"].replace("\n", "\n    "))
    for label, key in (
        ("ACTION", "gate_action_differences"),
        ("REASON-ONLY", "gate_reason_only_differences"),
    ):
        for entry in parity[key]:
            print(
                f"  BLOCKING gate {label} difference, page index {entry['page_index']} "
                f"({entry['class']}), cause={entry['cause']}: whole {entry['whole']} | "
                f"split {entry['split']} | whole with own confidence "
                f"{entry['whole_own_confidence']}"
            )
    print(
        f"gate differences left when the whole-file gate reads each page's OWN confidence "
        f"entry: {len(parity['gate_differences_with_own_confidence'])}"
    )
    for name, found in report["run_disagreements"].items():
        if found:
            print(f"  RUNS DISAGREE [{name}]: {found}")
    if not any(report["run_disagreements"].values()):
        print("the runs of each configuration agree with each other (markdown and gate)")
    for flag in report["review_flags"]:
        print(f"  REVIEW FLAG: {flag}")
    if not report["review_flags"]:
        print("no review flag raised")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure whole-file Docling against Docling on split one-page PDFs."
    )
    parser.add_argument("pdfs", nargs="*", help="One or more PDF paths to measure.")
    parser.add_argument("--output-dir", default=None, help="Directory for the raw JSON figures.")
    parser.add_argument("--runs", type=int, default=3, help="Runs per configuration (default 3).")
    parser.add_argument(
        "--keep-split",
        action="store_true",
        help="Keep the temporary one-page files instead of removing them at the end.",
    )
    parser.add_argument("--child", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.child:
        _run_child(Path(args.child))
        return

    if not args.pdfs:
        print("Error: at least one PDF path is required", file=sys.stderr)
        sys.exit(_EXIT_ERROR)
    if not args.output_dir:
        print("Error: --output-dir is required", file=sys.stderr)
        sys.exit(_EXIT_ERROR)
    if args.runs < 1:
        print("Error: --runs must be at least 1", file=sys.stderr)
        sys.exit(_EXIT_ERROR)

    # Validate every input BEFORE any Docling import or work.
    pdf_paths = [Path(p).resolve() for p in args.pdfs]
    for pdf_path in pdf_paths:
        error = _input_error(pdf_path)
        if error is not None:
            print(f"Error: {error}", file=sys.stderr)
            sys.exit(_EXIT_ERROR)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    temp_root = Path(tempfile.mkdtemp(prefix="measure_per_page_docling_"))
    if temp_root.resolve().is_relative_to(REPO_ROOT):
        shutil.rmtree(temp_root, ignore_errors=True)
        print(f"Error: temporary directory is inside the repo: {temp_root}", file=sys.stderr)
        sys.exit(_EXIT_ERROR)

    blocking = False
    try:
        for number, pdf_path in enumerate(pdf_paths):
            report = _measure_input(
                pdf_path, temp_root / f"{number:02d}_{pdf_path.stem}", args.runs
            )
            json_path = out_dir / f"{pdf_path.stem}.json"
            json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            _print_summary(report)
            print(f"raw figures: {json_path}")
            blocking = blocking or report["blocking"]
    except (RuntimeError, AssertionError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(_EXIT_ERROR)
    finally:
        if args.keep_split:
            print(f"one-page files kept in {temp_root}")
        else:
            shutil.rmtree(temp_root, ignore_errors=True)

    if blocking:
        print(
            "\nBLOCKING: a gate action or reason differs between the configurations "
            "(pending the user's call).",
            file=sys.stderr,
        )
        sys.exit(_EXIT_BLOCKING)


if __name__ == "__main__":
    main()
