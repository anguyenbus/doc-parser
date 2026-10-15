"""
observability.py — the page pipeline's metrics and structured log events (spec D9).

Two outputs, one set of facts:

- **Prometheus metrics** (page service). On ``prometheus_client``'s default
  registry, which KServe serves on its ``/metrics`` route. ``service_metrics()``
  builds them once, on first use; it returns None where ``prometheus_client``
  (the ``serve`` extra) is not installed, and every caller then skips the metric.
- **Structured log events** (driver and page service). One JSON object per log
  message, through ``log_event``, the one helper: it keeps only the D9 fields
  (``EVENT_FIELDS``) and cuts every string to 500 characters, so a caller cannot
  put document text, email headers, attachment names or long exception text in
  a log line by passing an extra field.

The driver is a short-lived Job and serves no ``/metrics``. Its counters
(``DRIVER_METRICS``) are defined as counts of its log events, and
``driver_counters`` derives them; no log shipping or log-to-metrics path exists
on the cluster yet, so nothing computes them there (see spec Risks).

Metrics (label sets as D9):

    parser_files_total{kind}                        driver: file_done
    parser_sniff_mismatch_total{kind, extension}    driver: file_intake with mismatch
    parser_unsupported_total{detected_type}         driver: file_intake with unsupported_type
    parser_guard_rejections_total{kind, reason}     driver guard_reject (too_large) and service
    parser_timeouts_total{kind}                     service
    parser_units_total{kind, state, reason_code}    service (reason_code "none" when ok)
    parser_flags_total{kind, flag}                  service
    parser_parse_seconds{kind}                      service histogram (worker wall time)
    parser_input_bytes{kind}, parser_output_bytes{kind}   service histograms
    parser_worker_restarts_total{cause}             service

Events and their fields:

    file_intake   driver, once per file after the sniff: job_id, doc_id, file_id,
                  kind, detected_type, extension, mismatch, reason_code (the
                  intake refusal, or null), input_bytes
    guard_reject  driver (too_large at intake) and service (zip_bomb,
                  too_complex, output_too_large records): kind, reason_code,
                  limits, input_bytes, and work_id/unit on the service
    unit_done     wherever a unit is parsed (the service; the local executor's
                  workers): work_id, kind, unit, state, reason_code, attempt,
                  limits, flags, parse_seconds, input_bytes, output_chars,
                  parse_worker_pid
    file_done     driver, once per file: job_id, doc_id, file_id, work_id, kind,
                  detected_type, extension, mismatch, unit, state, reason_code,
                  flags, input_bytes, output_chars, elapsed_seconds
    unit_timeout, worker_restart   service (``kserve_predictor``)

``kind`` in events and labels is the routed kind, or ``unsupported`` for a file
no kind handles. ``unit`` is the unit number (1-based) in unit-level events
(``unit_done``, ``unit_timeout``, the service's ``guard_reject``) and the unit
type (``page`` or ``document``) in ``file_done``. ``extension`` is one of
``KNOWN_EXTENSIONS``, ``other`` or ``none``. Every event also carries
``worker_pid`` and ``worker_host`` (the process and pod that logged it).
"""

from __future__ import annotations

import collections
import functools
import json
import logging
import os
import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from parser_service.sniff import extension_of

logger = logging.getLogger("parser_service.events")

# The longest string any event field may hold (spec D9: no exception text over 500).
MAX_TEXT_CHARS = 500

# The fields an event may carry (spec D9); ``cause`` is worker_restart's.
EVENT_FIELDS = frozenset(
    {
        "event",
        "job_id",
        "doc_id",
        "file_id",
        "work_id",
        "kind",
        "detected_type",
        "extension",
        "mismatch",
        "unit",
        "state",
        "reason_code",
        "attempt",
        "deadline_s",
        "limits",
        "parse_seconds",
        "elapsed_seconds",
        "input_bytes",
        "output_chars",
        "flags",
        "worker_pid",
        "parse_worker_pid",
        "worker_host",
        "cause",
    }
)

# The ``extension`` label's known values: every kind's own extensions and the
# types the sniff detects or the text rule names. Anything else is ``other``.
KNOWN_EXTENSIONS = frozenset(
    {
        "pdf", "docx", "txt", "html", "htm", "eml", "msg",
        "doc", "xls", "xlsx", "ppt", "pptx", "zip", "rtf",
        "png", "jpg", "jpeg", "gif", "tif", "tiff", "webp",
        "csv", "json", "xml", "md", "log",
    }
)  # fmt: skip

KIND_UNSUPPORTED = "unsupported"
# Unit reason codes that are guard rejections (D5, D7); too_large is the driver's.
GUARD_REASONS = frozenset({"too_large", "zip_bomb", "too_complex", "output_too_large"})

_SECONDS_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 900, 1800)
_BYTES_BUCKETS = (1e3, 1e4, 1e5, 1e6, 5e6, 1e7, 2e7, 5e7, 1e8, 2e8, 5e8, 1e9)

# The driver's counters: metric -> (the event counted, the event fields that give
# the label values, in label order; guard_reject's ``reason_code`` is ``reason``).
DRIVER_METRICS: dict[str, tuple[str, tuple[str, ...]]] = {
    "parser_files_total": ("file_done", ("kind",)),
    "parser_sniff_mismatch_total": ("file_intake", ("kind", "extension")),
    "parser_unsupported_total": ("file_intake", ("detected_type",)),
    "parser_guard_rejections_total": ("guard_reject", ("kind", "reason_code")),
}


def kind_label(kind: str | None) -> str:
    """The ``kind`` label of a file: its routed kind, or ``unsupported``."""
    return kind or KIND_UNSUPPORTED


def extension_label(name: str) -> str:
    """The ``extension`` label of a file name: a known extension, ``other`` or ``none``."""
    ext = extension_of(name)
    if not ext:
        return "none"
    return ext if ext in KNOWN_EXTENSIONS else "other"


def truncate(value: Any, limit: int = MAX_TEXT_CHARS) -> Any:
    """``value`` with every string in it cut to ``limit`` characters."""
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, Mapping):
        return {str(k)[:limit]: truncate(v, limit) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [truncate(v, limit) for v in value]
    return value


def log_event(event: str, **fields: Any) -> dict[str, Any]:
    """Log one structured event (spec D9) as a JSON object; return what was logged.

    Fields outside ``EVENT_FIELDS`` are dropped, and every string is cut to
    ``MAX_TEXT_CHARS``: this is the only path an event takes to the log.
    """
    record = {k: truncate(v) for k, v in fields.items() if k in EVENT_FIELDS}
    record["event"] = event
    record.setdefault("worker_pid", os.getpid())
    record.setdefault("worker_host", socket.gethostname())
    logger.info("%s", json.dumps(record, sort_keys=True, default=str))
    return record


# ---------------------------------------------------------------------------
# Page service metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceMetrics:
    """The page service's Prometheus metrics (spec D9), on the default registry."""

    guard_rejections: Any
    timeouts: Any
    units: Any
    flags: Any
    parse_seconds: Any
    input_bytes: Any
    output_bytes: Any
    worker_restarts: Any


@functools.cache
def service_metrics() -> ServiceMetrics | None:
    """The metrics, built once; None when ``prometheus_client`` is not installed."""
    try:
        from prometheus_client import Counter, Histogram
    except ImportError:
        return None
    return ServiceMetrics(
        guard_rejections=Counter(
            "parser_guard_rejections",
            "Units refused by a cost guard, by kind and reason.",
            ["kind", "reason"],
        ),
        timeouts=Counter(
            "parser_timeouts",
            "Units whose parse ran past the deadline (the worker was killed).",
            ["kind"],
        ),
        units=Counter(
            "parser_units",
            "Units parsed, by kind, state and reason code.",
            ["kind", "state", "reason_code"],
        ),
        flags=Counter("parser_flags", "Flags set on parsed units.", ["kind", "flag"]),
        parse_seconds=Histogram(
            "parser_parse_seconds",
            "Wall time of one unit's parse in the worker.",
            ["kind"],
            buckets=_SECONDS_BUCKETS,
        ),
        input_bytes=Histogram(
            "parser_input_bytes", "Size of a parsed unit's file.", ["kind"], buckets=_BYTES_BUCKETS
        ),
        output_bytes=Histogram(
            "parser_output_bytes",
            "Size of a parsed unit's markdown (UTF-8).",
            ["kind"],
            buckets=_BYTES_BUCKETS,
        ),
        worker_restarts=Counter(
            "parser_worker_restarts",
            "Parse worker losses and failed starts, by cause.",
            ["cause"],
        ),
    )


def count_worker_restart(cause: str) -> None:
    """``parser_worker_restarts_total{cause}`` += 1."""
    metrics = service_metrics()
    if metrics is not None:
        metrics.worker_restarts.labels(cause=cause).inc()


def count_timeout(kind: str) -> None:
    """``parser_timeouts_total{kind}`` += 1."""
    metrics = service_metrics()
    if metrics is not None:
        metrics.timeouts.labels(kind=kind).inc()


def observe_unit(
    record: Mapping[str, Any],
    *,
    work_id: str,
    input_bytes: int | None,
) -> None:
    """Count one parsed unit and log ``unit_done`` (and ``guard_reject`` for a guard code).

    ``record`` is the unit's record as written (``page_pipeline._write_record``).
    """
    kind = str(record.get("kind") or "pdf")
    state = str(record.get("state"))
    reason_code = record.get("reason_code")
    flags = list(record.get("flags") or [])
    markdown = str(record.get("markdown") or "")
    parse_seconds = record.get("elapsed_seconds")
    metrics = service_metrics()
    if metrics is not None:
        metrics.units.labels(kind=kind, state=state, reason_code=reason_code or "none").inc()
        for flag in flags:
            metrics.flags.labels(kind=kind, flag=flag).inc()
        if isinstance(parse_seconds, int | float):
            metrics.parse_seconds.labels(kind=kind).observe(parse_seconds)
        if input_bytes is not None:
            metrics.input_bytes.labels(kind=kind).observe(input_bytes)
        metrics.output_bytes.labels(kind=kind).observe(len(markdown.encode("utf-8")))
        if reason_code in GUARD_REASONS:
            metrics.guard_rejections.labels(kind=kind, reason=reason_code).inc()
    common = {
        "work_id": work_id,
        "kind": kind,
        "unit": record.get("page"),
        "reason_code": reason_code,
        "limits": record.get("limits"),
        "input_bytes": input_bytes,
    }
    if reason_code in GUARD_REASONS:
        log_event("guard_reject", **common)
    log_event(
        "unit_done",
        **common,
        state=state,
        attempt=record.get("attempt"),
        flags=flags,
        parse_seconds=parse_seconds,
        output_chars=len(markdown),
        parse_worker_pid=record.get("parse_worker_pid"),
    )


# ---------------------------------------------------------------------------
# Driver counters, derived from its events
# ---------------------------------------------------------------------------


def driver_counters(
    events: Iterable[Mapping[str, Any]],
) -> dict[str, collections.Counter[tuple[str, ...]]]:
    """The driver's ``DRIVER_METRICS`` as counts of its ``events``, by label values.

    What a log-to-metrics path would compute from the driver's log events:
    ``file_intake`` counts towards the mismatch counter only with ``mismatch``,
    and towards the unsupported counter only with ``reason_code``
    ``unsupported_type``.
    """
    counts: dict[str, collections.Counter[tuple[str, ...]]] = {
        name: collections.Counter() for name in DRIVER_METRICS
    }
    for event in events:
        for name, (source, labels) in DRIVER_METRICS.items():
            if event.get("event") != source:
                continue
            if name == "parser_sniff_mismatch_total" and not event.get("mismatch"):
                continue
            if (
                name == "parser_unsupported_total"
                and event.get("reason_code") != "unsupported_type"
            ):
                continue
            counts[name][tuple(str(event.get(label)) for label in labels)] += 1
    return counts
