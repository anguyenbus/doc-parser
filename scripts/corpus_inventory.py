"""
corpus_inventory.py

Measure a document corpus before setting the page pipeline's caps, guard thresholds
and deadlines (spec sub-step 1). Read-only: it lists and reads objects, and writes
only its own reports.

Usage:
    AWS_REGION=ap-southeast-2 uv run python scripts/corpus_inventory.py \
        --prefix s3://bucket/some/prefix --out-dir <report dir> \
        [--max-files N] [--time-sample K] [--csv <per-file.csv>] [--seed 0]

    # The same on a local folder (no AWS call):
    uv run python scripts/corpus_inventory.py --prefix <folder> --out-dir <report dir>

Every file is sniffed from its first 64 KiB (an S3 range GET) with
``parser_service.formats.sniff``. Only files detected as DOCX are read in full, streamed to a
temporary file, for ``parser_service.formats.guards.docx_stats``; files detected
as XLSX likewise, for ``guards.xlsx_stats``. A PNG or JPEG
gets its declared megapixels from the same head (``guards.image_header_of``); a JPEG
whose frame header lies past the head is read in full for it.

Outputs:
    <out-dir>/inventory.json   aggregate: counts by detected type, by extension and by
                               routed kind; size p50/p99/max per kind and per detected
                               type; DOCX index and element-count percentiles; name and
                               content mismatches; the .msg share; XLSX cell and
                               used-range percentiles; image megapixel percentiles;
                               timing (if asked)
    <out-dir>/inventory.md     the same as a markdown report
    --csv                      one row per file: key and numbers only, no document text.
                               It must be outside the repository (default: the system
                               temp directory), so it is never committed.

``--time-sample K`` parses K files per routed kind (the largest plus random ones,
seeded by ``--seed``) in a child process, and records its wall time and peak RSS.
The child runs in its own session and is killed (its whole process group) after
``--time-limit`` seconds (default 900, the driver's request timeout) or above
``--rss-ceiling-gb``, as ``planning/evidence/bomb_test.py`` does. TXT is read and
decoded without Docling; every other kind goes through Docling's ``DocumentConverter``.

S3 work runs in ap-southeast-2 only: any other AWS_REGION (or none) is refused.

Exit codes: 0 the reports were written; 1 the arguments or the region could not be used.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import logging
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from parser_service.formats.guards import (  # noqa: E402
    docx_stats,
    image_header,
    image_header_of,
    xlsx_stats,
)
from parser_service.formats.sniff import (  # noqa: E402
    KIND_EXTENSIONS,
    SNIFF_BYTES,
    extension_of,
    sniff,
)
from parser_service.storage import aws  # noqa: E402

log = logging.getLogger("corpus_inventory")

REGION = "ap-southeast-2"
REPO_ROOT = Path(__file__).resolve().parent.parent
TIME_LIMIT_S = 900.0
RSS_CEILING_GB = 14.0

CSV_COLUMNS = (
    "key",
    "size_bytes",
    "extension",
    "declared_type",
    "detected_type",
    "kind",
    "mismatch",
    "docx_entries",
    "docx_expanded_bytes",
    "docx_compressed_bytes",
    "docx_largest_part_bytes",
    "docx_ratio",
    "docx_max_large_part_ratio",
    "docx_duplicate_names",
    "docx_encrypted_entries",
    "docx_document_xml_bytes",
    "docx_paragraphs",
    "docx_table_cells",
    "xlsx_sheets",
    "xlsx_cells",
    "xlsx_grid_cells",
    "xlsx_formulas_without_value",
    "xlsx_expanded_bytes",
    "xlsx_ratio",
    "image_megapixels",
    "error",
    "parse_verdict",
    "parse_seconds",
    "parse_peak_rss_bytes",
)
_DOCX_FIELDS = (
    "docx_entries",
    "docx_expanded_bytes",
    "docx_largest_part_bytes",
    "docx_ratio",
    "docx_max_large_part_ratio",
    "docx_document_xml_bytes",
    "docx_paragraphs",
    "docx_table_cells",
)

_XLSX_FIELDS = (
    "xlsx_sheets",
    "xlsx_cells",
    "xlsx_grid_cells",
    "xlsx_formulas_without_value",
    "xlsx_expanded_bytes",
    "xlsx_ratio",
)


# ---------------------------------------------------------------------------
# Sources: a local folder or an S3 prefix
# ---------------------------------------------------------------------------


@dataclass
class FileRow:
    """One file's numbers. ``key`` is the S3 key or the path under the folder."""

    key: str
    size_bytes: int
    extension: str
    declared_type: str = ""
    detected_type: str = ""
    kind: str = ""
    mismatch: bool = False
    docx: dict[str, Any] = field(default_factory=dict)
    xlsx: dict[str, Any] = field(default_factory=dict)
    image_megapixels: float | None = None
    error: str = ""
    parse_verdict: str = ""
    parse_seconds: float | None = None
    parse_peak_rss_bytes: int | None = None

    def csv_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "key": self.key,
            "size_bytes": self.size_bytes,
            "extension": self.extension,
            "declared_type": self.declared_type,
            "detected_type": self.detected_type,
            "kind": self.kind,
            "mismatch": int(self.mismatch),
            "image_megapixels": "" if self.image_megapixels is None else self.image_megapixels,
            "error": self.error,
            "parse_verdict": self.parse_verdict,
            "parse_seconds": "" if self.parse_seconds is None else round(self.parse_seconds, 3),
            "parse_peak_rss_bytes": (
                "" if self.parse_peak_rss_bytes is None else self.parse_peak_rss_bytes
            ),
        }
        for column in CSV_COLUMNS:
            if column.startswith(("docx_", "xlsx_")):
                value = (self.docx if column.startswith("docx_") else self.xlsx).get(column)
                row[column] = "" if value is None else value
        return row


class LocalSource:
    """Files under a local folder, in sorted order."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def list(self) -> Iterator[tuple[str, int]]:
        for path in sorted(p for p in self.root.rglob("*") if p.is_file()):
            yield path.relative_to(self.root).as_posix(), path.stat().st_size

    def head(self, key: str) -> tuple[bytes, str]:
        with (self.root / key).open("rb") as handle:
            return handle.read(SNIFF_BYTES), ""

    def download(self, key: str, target: Path) -> None:
        shutil.copyfile(self.root / key, target)


class S3Source:
    """Objects under ``s3://bucket/prefix``: list, range GET, streamed GET. Read-only."""

    def __init__(self, uri: str, region: str) -> None:
        self.bucket, self.prefix = aws.parse_s3_uri(uri)
        if not self.bucket:
            raise ValueError(f"not an s3://bucket/prefix URI: {uri}")
        self.client = aws.s3_client(region)

    def list(self) -> Iterator[tuple[str, int]]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for item in page.get("Contents", []):
                if not item["Key"].endswith("/"):
                    yield item["Key"], int(item["Size"])

    def head(self, key: str) -> tuple[bytes, str]:
        response = self.client.get_object(
            Bucket=self.bucket, Key=key, Range=f"bytes=0-{SNIFF_BYTES - 1}"
        )
        return response["Body"].read(), response.get("ContentType") or ""

    def download(self, key: str, target: Path) -> None:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        with target.open("wb") as handle:
            shutil.copyfileobj(response["Body"], handle)


def open_source(prefix: str, region: str | None) -> LocalSource | S3Source:
    """A source for ``prefix``. S3 needs ``region`` to be ap-southeast-2."""
    if prefix.startswith("s3://"):
        if region != REGION:
            raise ValueError(
                f"S3 inventory runs in {REGION} only; set AWS_REGION={REGION} (got {region!r})"
            )
        return S3Source(prefix, region)
    root = Path(prefix)
    if not root.is_dir():
        raise ValueError(f"not a folder: {prefix}")
    return LocalSource(root)


# ---------------------------------------------------------------------------
# Per-file measurement
# ---------------------------------------------------------------------------


def measure_file(source: LocalSource | S3Source, key: str, size: int, scratch: Path) -> FileRow:
    """Sniff one file; stream a DOCX or XLSX in full for its stats; for an image read
    its declared megapixels."""
    row = FileRow(key=key, size_bytes=size, extension=extension_of(key))
    try:
        head, declared = source.head(key)
        row.declared_type = declared
        result = sniff(head, key.rpartition("/")[2], declared or None)
        row.detected_type = result.detected_type
        row.kind = result.kind or ""
        row.mismatch = result.mismatch
        if result.detected_type == "docx":
            target = scratch / "stats.docx"
            try:
                source.download(key, target)
                row.docx = _docx_numbers(target)
            finally:
                target.unlink(missing_ok=True)
        elif result.detected_type == "xlsx":
            target = scratch / "stats.xlsx"
            try:
                source.download(key, target)
                row.xlsx = _xlsx_numbers(target)
            finally:
                target.unlink(missing_ok=True)
        elif result.detected_type in ("png", "jpeg"):
            header = image_header_of(head)
            if header is None and result.detected_type == "jpeg":
                target = scratch / "header.jpg"
                try:
                    source.download(key, target)
                    header = image_header(target)
                finally:
                    target.unlink(missing_ok=True)
            if header is not None:
                row.image_megapixels = round(header.megapixels, 2)
    except Exception as exc:  # noqa: BLE001 - one bad object must not stop the inventory
        row.error = type(exc).__name__
    return row


def _xlsx_numbers(path: Path) -> dict[str, Any]:
    stats = xlsx_stats(path)
    return {
        "xlsx_sheets": stats.sheets,
        "xlsx_cells": stats.cells,
        "xlsx_grid_cells": stats.grid_cells,
        "xlsx_formulas_without_value": stats.formulas_without_value,
        "xlsx_expanded_bytes": stats.index.expanded_bytes,
        "xlsx_ratio": round(stats.index.ratio, 2),
    }


def _docx_numbers(path: Path) -> dict[str, Any]:
    stats = docx_stats(path)
    return {
        "docx_entries": stats.index.entries,
        "docx_expanded_bytes": stats.index.expanded_bytes,
        "docx_compressed_bytes": stats.index.compressed_bytes,
        "docx_largest_part_bytes": stats.index.largest_part_bytes,
        "docx_ratio": round(stats.index.ratio, 2),
        "docx_max_large_part_ratio": round(stats.index.max_large_part_ratio, 2),
        "docx_duplicate_names": stats.index.duplicate_names,
        "docx_encrypted_entries": stats.index.encrypted_entries,
        "docx_document_xml_bytes": stats.document_xml_bytes,
        "docx_paragraphs": stats.paragraphs,
        "docx_table_cells": stats.table_cells,
    }


# ---------------------------------------------------------------------------
# Timing child: wall time and peak RSS, killed at a limit
# ---------------------------------------------------------------------------


def measure_child(
    argv: list[str],
    time_limit_s: float = TIME_LIMIT_S,
    rss_ceiling_bytes: int | None = None,
    poll_s: float = 0.2,
) -> dict[str, Any]:
    """Run ``argv`` in its own session; return its verdict, wall time and peak RSS.

    The verdict is ``finished`` (exit 0), ``failed`` (non-zero exit),
    ``killed_time`` (over ``time_limit_s``) or ``killed_memory`` (RSS over the
    ceiling). A kill sends SIGKILL to the child's whole process group. Peak RSS is
    the kernel's ``ru_maxrss`` for the child, from ``os.wait4``.
    """
    start = time.monotonic()
    child = subprocess.Popen(
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )
    verdict = ""
    while True:
        pid, status, usage = os.wait4(child.pid, os.WNOHANG)
        if pid:
            break
        if time.monotonic() - start > time_limit_s:
            verdict = "killed_time"
        elif rss_ceiling_bytes is not None and _rss_bytes(child.pid) > rss_ceiling_bytes:
            verdict = "killed_memory"
        if verdict:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            _, status, usage = os.wait4(child.pid, 0)
            break
        time.sleep(poll_s)
    child.returncode = os.waitstatus_to_exitcode(status)
    if not verdict:
        verdict = "finished" if child.returncode == 0 else "failed"
    return {
        "verdict": verdict,
        "seconds": time.monotonic() - start,
        "peak_rss_bytes": usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
        "returncode": child.returncode,
    }


def _rss_bytes(pid: int) -> int:
    """Current RSS of ``pid`` from /proc (0 where /proc is not available)."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return 0


def child_parse(kind: str, path: str) -> int:
    """The timing child's work: parse one file the way its kind would be parsed."""
    if kind == "txt":
        data = Path(path).read_bytes()
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        return len(text)
    from docling.document_converter import DocumentConverter

    document = DocumentConverter().convert(path).document
    return len(document.export_to_markdown())


def pick_time_sample(rows: list[FileRow], k: int, seed: int) -> list[FileRow]:
    """Per routed kind: the largest file plus up to ``k - 1`` random others."""
    rng = random.Random(seed)
    picked: list[FileRow] = []
    for kind in sorted({row.kind for row in rows if row.kind and not row.error}):
        candidates = sorted(
            (row for row in rows if row.kind == kind and not row.error),
            key=lambda row: (-row.size_bytes, row.key),
        )
        if not candidates or k <= 0:
            continue
        rest = candidates[1:]
        picked.append(candidates[0])
        picked.extend(rng.sample(rest, min(k - 1, len(rest))))
    return picked


def time_sample(
    source: LocalSource | S3Source,
    rows: list[FileRow],
    k: int,
    seed: int,
    scratch: Path,
    time_limit_s: float,
    rss_ceiling_bytes: int | None,
) -> None:
    """Parse the sampled files in a child process each; fill in their timing fields."""
    for row in pick_time_sample(rows, k, seed):
        target = scratch / f"sample.{KIND_EXTENSIONS[row.kind]}"
        try:
            source.download(row.key, target)
            argv = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--child-parse",
                row.kind,
                str(target),
            ]
            result = measure_child(argv, time_limit_s, rss_ceiling_bytes)
        finally:
            target.unlink(missing_ok=True)
        row.parse_verdict = result["verdict"]
        row.parse_seconds = result["seconds"]
        row.parse_peak_rss_bytes = result["peak_rss_bytes"]
        log.info(
            "timed kind=%s size=%d verdict=%s seconds=%.1f",
            row.kind,
            row.size_bytes,
            row.parse_verdict,
            row.parse_seconds,
        )


# ---------------------------------------------------------------------------
# Aggregation and reports
# ---------------------------------------------------------------------------


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (``q`` in 0-100); None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * q // 100))
    return ordered[int(rank) - 1]


def _spread(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50": percentile(values, 50),
        "p99": percentile(values, 99),
        "max": max(values) if values else None,
    }


def aggregate(rows: list[FileRow], prefix: str) -> dict[str, Any]:
    """The aggregate report: counts and numbers only, never a key or any text."""
    total = len(rows)
    by_kind: dict[str, list[float]] = {}
    by_type: dict[str, list[float]] = {}
    for row in rows:
        by_kind.setdefault(row.kind or "none", []).append(row.size_bytes)
        by_type.setdefault(row.detected_type or "error", []).append(row.size_bytes)
    docx_rows = [row for row in rows if row.docx]
    xlsx_rows = [row for row in rows if row.xlsx]
    image_rows = [row for row in rows if row.detected_type in ("png", "jpeg")]
    msg_files = sum(1 for row in rows if row.detected_type == "msg")
    msg_bytes = sum(row.size_bytes for row in rows if row.detected_type == "msg")
    total_bytes = sum(row.size_bytes for row in rows)
    mismatch_pairs = Counter(
        f"{row.extension or 'none'}->{row.detected_type}" for row in rows if row.mismatch
    )
    timing: dict[str, Any] = {}
    for row in rows:
        if row.parse_verdict:
            entry = timing.setdefault(
                row.kind, {"n": 0, "verdicts": Counter(), "seconds": [], "peak_rss_bytes": []}
            )
            entry["n"] += 1
            entry["verdicts"][row.parse_verdict] += 1
            entry["seconds"].append(row.parse_seconds)
            entry["peak_rss_bytes"].append(row.parse_peak_rss_bytes)
    return {
        "prefix": prefix,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": total,
        "bytes": total_bytes,
        "errors": dict(Counter(row.error for row in rows if row.error)),
        "by_detected_type": dict(Counter(row.detected_type or "error" for row in rows)),
        "by_extension": dict(Counter(row.extension or "none" for row in rows)),
        "by_kind": dict(Counter(row.kind or "none" for row in rows)),
        "size_bytes_by_kind": {kind: _spread(v) for kind, v in sorted(by_kind.items())},
        "size_bytes_by_detected_type": {t: _spread(v) for t, v in sorted(by_type.items())},
        "docx": {
            "files": len(docx_rows),
            **{
                name.removeprefix("docx_"): _spread(
                    [row.docx[name] for row in docx_rows if row.docx.get(name) is not None]
                )
                for name in _DOCX_FIELDS
            },
            "duplicate_name_files": sum(
                1 for row in docx_rows if row.docx.get("docx_duplicate_names")
            ),
            "encrypted_entry_files": sum(
                1 for row in docx_rows if row.docx.get("docx_encrypted_entries")
            ),
        },
        "xlsx": {
            "files": len(xlsx_rows),
            **{
                name.removeprefix("xlsx_"): _spread(
                    [row.xlsx[name] for row in xlsx_rows if row.xlsx.get(name) is not None]
                )
                for name in _XLSX_FIELDS
            },
            "files_with_uncalculated_formulas": sum(
                1 for row in xlsx_rows if row.xlsx.get("xlsx_formulas_without_value")
            ),
        },
        "images": {
            "files": len(image_rows),
            "unreadable_header_files": sum(1 for row in image_rows if row.image_megapixels is None),
            "megapixels": _spread(
                [row.image_megapixels for row in image_rows if row.image_megapixels is not None]
            ),
        },
        "mismatches": {"files": sum(mismatch_pairs.values()), "by_pair": dict(mismatch_pairs)},
        "extension_vs_detected": dict(
            Counter(f"{row.extension or 'none'}->{row.detected_type or 'error'}" for row in rows)
        ),
        "msg_share": {
            "files": msg_files,
            "file_share": msg_files / total if total else 0.0,
            "byte_share": msg_bytes / total_bytes if total_bytes else 0.0,
        },
        "timing": {
            kind: {
                "n": entry["n"],
                "verdicts": dict(entry["verdicts"]),
                "seconds": _spread(entry["seconds"]),
                "peak_rss_bytes": _spread(entry["peak_rss_bytes"]),
            }
            for kind, entry in sorted(timing.items())
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    """The aggregate report as markdown."""
    lines = [
        "# Corpus inventory",
        "",
        f"- Prefix: `{report['prefix']}`",
        f"- Generated: {report['generated_at']}",
        f"- Files: {report['files']} ({report['bytes']} bytes)",
        f"- Errors: {report['errors'] or 'none'}",
        f"- `.msg` share: {report['msg_share']['files']} files, "
        f"{report['msg_share']['file_share']:.2%} of files, "
        f"{report['msg_share']['byte_share']:.2%} of bytes",
        "",
    ]
    for title, key in (
        ("Files by detected type", "by_detected_type"),
        ("Files by extension", "by_extension"),
        ("Files by routed kind", "by_kind"),
        ("Name and content mismatches (routed files)", None),
        ("Extension vs detected type (all files)", "extension_vs_detected"),
    ):
        counts = report["mismatches"]["by_pair"] if key is None else report[key]
        lines += [f"## {title}", "", "| Value | Files |", "|---|---|"]
        lines += [f"| {name} | {count} |" for name, count in sorted(counts.items())]
        lines.append("")
    for title, key in (
        ("Size (bytes) by routed kind", "size_bytes_by_kind"),
        ("Size (bytes) by detected type", "size_bytes_by_detected_type"),
    ):
        lines += [_spread_table(title, "Kind", report[key]), ""]
    docx = report["docx"]
    lines += [
        _spread_table(
            f"DOCX ({docx['files']} files; {docx['duplicate_name_files']} with duplicate "
            f"names, {docx['encrypted_entry_files']} with encrypted entries)",
            "Measure",
            {name.removeprefix("docx_"): docx[name.removeprefix("docx_")] for name in _DOCX_FIELDS},
        ),
        "",
    ]
    xlsx = report["xlsx"]
    lines += [
        _spread_table(
            f"XLSX ({xlsx['files']} files; {xlsx['files_with_uncalculated_formulas']} with "
            "formulas stored without a value)",
            "Measure",
            {name.removeprefix("xlsx_"): xlsx[name.removeprefix("xlsx_")] for name in _XLSX_FIELDS},
        ),
        "",
    ]
    images = report["images"]
    lines += [
        _spread_table(
            f"Images ({images['files']} files; {images['unreadable_header_files']} with "
            "no readable size)",
            "Measure",
            {"megapixels": images["megapixels"]},
        ),
        "",
    ]
    if report["timing"]:
        lines += [
            "## Parse timing (sample)",
            "",
            "| Kind | n | Verdicts | s p50 | s p99 | s max | RSS p50 | RSS max |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for kind, entry in report["timing"].items():
            s, r = entry["seconds"], entry["peak_rss_bytes"]
            lines.append(
                f"| {kind} | {entry['n']} | {entry['verdicts']} | {_num(s['p50'])} | "
                f"{_num(s['p99'])} | {_num(s['max'])} | {_num(r['p50'])} | {_num(r['max'])} |"
            )
        lines.append("")
    return "\n".join(lines)


def _spread_table(title: str, label: str, spreads: dict[str, dict[str, Any]]) -> str:
    lines = [f"## {title}", "", f"| {label} | n | p50 | p99 | max |", "|---|---|---|---|---|"]
    for name, s in spreads.items():
        lines.append(
            f"| {name} | {s['n']} | {_num(s['p50'])} | {_num(s['p99'])} | {_num(s['max'])} |"
        )
    return "\n".join(lines)


def _num(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}" if isinstance(value, float) and not value.is_integer() else f"{value:.0f}"


def _inside_repo(path: Path) -> bool:
    return path.resolve().is_relative_to(REPO_ROOT)


def write_reports(
    rows: list[FileRow], prefix: str, out_dir: Path, csv_path: Path
) -> dict[str, Any]:
    """Write inventory.json, inventory.md and the per-file CSV; return the aggregate."""
    report = aggregate(rows, prefix)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "inventory.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (out_dir / "inventory.md").write_text(render_markdown(report))
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.csv_row())
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    csv_path = (
        Path(args.csv)
        if args.csv
        else Path(tempfile.gettempdir())
        / (f"corpus_inventory_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.csv")
    )
    if _inside_repo(csv_path):
        print(
            f"error: the per-file CSV must be outside the repository: {csv_path}", file=sys.stderr
        )
        return 1
    try:
        source = open_source(args.prefix, os.environ.get("AWS_REGION"))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    rows: list[FileRow] = []
    with tempfile.TemporaryDirectory(prefix="corpus_inventory_") as scratch:
        for key, size in source.list():
            if args.max_files is not None and len(rows) >= args.max_files:
                break
            rows.append(measure_file(source, key, size, Path(scratch)))
            if len(rows) % 500 == 0:
                log.info("sniffed %d files", len(rows))
        if args.time_sample:
            ceiling = int(args.rss_ceiling_gb * 1e9) if args.rss_ceiling_gb else None
            time_sample(
                source, rows, args.time_sample, args.seed, Path(scratch), args.time_limit, ceiling
            )

    write_reports(rows, args.prefix, Path(args.out_dir), csv_path)
    print(f"{len(rows)} files; reports in {args.out_dir}; per-file CSV {csv_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1] if __doc__ else None)
    parser.add_argument("--prefix", help="s3://bucket/prefix or a local folder")
    parser.add_argument("--out-dir", help="folder for inventory.json and inventory.md")
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--time-sample", type=int, default=0, metavar="K")
    parser.add_argument("--time-limit", type=float, default=TIME_LIMIT_S, help="seconds")
    parser.add_argument("--rss-ceiling-gb", type=float, default=RSS_CEILING_GB)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--csv", default=None, help="per-file CSV path, outside the repo")
    parser.add_argument("--child-parse", nargs=2, metavar=("KIND", "PATH"), help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.child_parse:
        child_parse(*args.child_parse)
        return 0
    if not args.prefix or not args.out_dir:
        print("error: --prefix and --out-dir are required", file=sys.stderr)
        return 1
    logging.basicConfig(
        level=os.environ.get("PARSER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
