"""
config.py — Pipeline configuration: unit and file states, the per-kind config and its hash,
enabled kinds, caps, deadlines, and the rule that makes a failure final.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from parser_service.formats.guards import DocxLimits, GuardRejection, check_docx
from parser_service.formats.handlers import handler_version
from parser_service.formats.result import (
    STATE_OK,
)
from parser_service.formats.sniff import KIND_EXTENSIONS
from parser_service.settings import (
    DEADLINE_ENV,
    DEFAULT_MAX_OUTPUT_MB,
    DEFAULT_REMOTE_REQUEST_TIMEOUT_S,
    DOCX_GUARD_ENV,
    ENABLED_KINDS_ENV,
    MAX_OUTPUT_ENV,
    RAW_CAP_ENV,
    REMOTE_REQUEST_TIMEOUT_ENV,
    split_kinds,
)
from parser_service.version import PARSER_VERSION

logger = logging.getLogger(__name__)

# File states in status.json.
FILE_COMPLETE = "complete"
FILE_FAILED = "failed"
FILE_IN_PROGRESS = "in_progress"

PAGE_MARKER = "==== page {number} ==="
_PAGE_MARKER_RE = re.compile(r"^==== page \d+(?: \([^)\n]*\))? ===\n\n?", re.MULTILINE)

_DEFAULT_MAX_ATTEMPTS = 3


class PipelineConfigMismatch(RuntimeError):
    """The work directory was started under a different pipeline config."""


class PageRecordError(RuntimeError):
    """A page's stored result exists but cannot be used (unreadable, or lost in an archive)."""


class KindNotEnabled(RuntimeError):
    """The unit's kind is not in this page service's ``PARSER_ENABLED_KINDS`` (HTTP 409)."""


def default_max_attempts() -> int:
    """Attempt limit per page (``PARSER_PAGE_MAX_ATTEMPTS``, default 3)."""
    return int(os.environ.get("PARSER_PAGE_MAX_ATTEMPTS", str(_DEFAULT_MAX_ATTEMPTS)))


def default_workers() -> int:
    """Worker processes (``PARSER_PAGE_WORKERS``, default min(4, cpus // 2)).

    Each worker holds its own Docling models (about 2-3 GB peak on OCR pages).
    """
    env = os.environ.get("PARSER_PAGE_WORKERS")
    if env is not None:
        return int(env)
    return max(1, min(4, (os.cpu_count() or 2) // 2))


# Docling decides the text of every docling-kept page, so its version is part of the
# config: an upgrade must not resume onto pages parsed by the old version.
DOCLING_VERSION: str = importlib.metadata.version("docling")


UNIT_PAGE = "page"
UNIT_DOCUMENT = "document"


def pipeline_config(kind: str = "pdf") -> dict[str, Any]:
    """The settings that change a ``kind`` unit's output. Pinned in the manifest at intake.

    A PDF page: the parser and Docling versions and the escalation settings. Every
    other kind leaves the escalation settings out (a VLM model change does not
    re-parse a DOCX), and txt, which never runs Docling, leaves Docling out too.
    Each config names its kind and the kind's handler version
    (``handlers.HANDLER_VERSIONS``), so every kind gets its own hash and a
    handler bump changes only its own kind's.
    """
    if kind not in KIND_EXTENSIONS:
        raise ValueError(f"unknown kind {kind!r}")
    if kind == "pdf":
        config: dict[str, Any] = {
            "parser_version": PARSER_VERSION,
            "docling_version": DOCLING_VERSION,
            "escalation_engine": os.environ.get("PARSER_ESCALATION_ENGINE", "vlm"),
            "vlm_model": os.environ.get("BEDROCK_VLM_MODEL", ""),
            "scan_fastpath": os.environ.get("PARSER_SCAN_FASTPATH", "").lower() in ("1", "true"),
            "arbitration": os.environ.get("PARSER_ESCALATION_ARBITRATION", "").lower()
            in ("1", "true"),
        }
    else:
        config = {"parser_version": PARSER_VERSION}
        if kind != "txt":
            config["docling_version"] = DOCLING_VERSION
    config["kind"] = kind
    config["handler_version"] = handler_version(kind)
    return config


def manifest_kind(manifest: Mapping[str, Any]) -> str:
    """The kind of a manifest's file. No ``kind`` (written before kinds existed) is pdf."""
    return str(manifest.get("kind") or "pdf")


def unit_of(kind: str) -> str:
    """What one unit of ``kind`` is: a ``page`` of a PDF, else the whole ``document``."""
    return UNIT_PAGE if kind == "pdf" else UNIT_DOCUMENT


def enabled_kinds() -> tuple[str, ...]:
    """The kinds intake routes (``PARSER_ENABLED_KINDS``, default every handled kind).

    ``settings.load_settings`` validates the value at start-up; here it is only
    split, like the other call-time reads.
    """
    return split_kinds(os.environ.get(ENABLED_KINDS_ENV))


def raw_cap_bytes(kind: str) -> int | None:
    """The raw size cap of a ``kind`` file in bytes (None: no cap). Read at call time.

    The caps are in MB (1,000,000 bytes) in ``RAW_CAP_ENV``; PDFs have none here.
    """
    if kind not in RAW_CAP_ENV:
        return None
    name, default = RAW_CAP_ENV[kind]
    return int(os.environ.get(name, str(default))) * 1_000_000


def max_output_mb() -> int:
    """The output cap of a single-unit document, in MB (``PARSER_MAX_OUTPUT_MB``)."""
    return int(os.environ.get(MAX_OUTPUT_ENV, str(DEFAULT_MAX_OUTPUT_MB)))


def output_cap_bytes(kind: str) -> int | None:
    """The cap on one unit's markdown in bytes, sent to the parser (None: no cap).

    Single-unit kinds only: a PDF page's output is bounded by the page, so PDF
    requests keep sending None. Read at call time.
    """
    if kind == "pdf":
        return None
    return max_output_mb() * 1_000_000


# Reason codes of deterministic unit failures (D5). A record with one of them, and
# with the ``limits`` behind it still equal to the current values, is final: it is
# never archived or re-run. Every other failure (an exception, ``docling_failed``,
# a worker or pod death) is retried under the attempt limit.
FINAL_REASON_CODES = frozenset(
    {"timeout", "zip_bomb", "too_complex", "output_too_large", "unsupported_type"}
)


def deadline_for(kind: str = "pdf") -> int:
    """The parse deadline of one unit of ``kind``, in seconds. Read at call time.

    Not part of ``pipeline_config``: a changed deadline never re-parses finished
    work; it only releases ``timeout`` records stored under another value.
    """
    if kind not in DEADLINE_ENV:
        raise ValueError(f"no parse deadline is defined for kind {kind!r}")
    name, default = DEADLINE_ENV[kind]
    return int(os.environ.get(name, str(default)))


def remote_request_timeout() -> int:
    """The driver's timeout for one page request (``PARSER_REMOTE_REQUEST_TIMEOUT_S``)."""
    return int(os.environ.get(REMOTE_REQUEST_TIMEOUT_ENV, str(DEFAULT_REMOTE_REQUEST_TIMEOUT_S)))


def docx_guard_limits() -> DocxLimits:
    """The DOCX cost guard's thresholds (``DOCX_GUARD_ENV``). Read at call time.

    ``settings.load_settings`` validates them at start-up. Not part of
    ``pipeline_config``: a changed threshold never re-parses finished work; it
    only releases ``zip_bomb`` and ``too_complex`` records stored under another
    value (spec D5).
    """
    values = {
        key: int(os.environ.get(name, str(default)))
        for key, (name, default) in DOCX_GUARD_ENV.items()
    }
    return DocxLimits(**values)


def current_limits(kind: str = "pdf") -> dict[str, Any]:
    """The current value of every limit a final record of ``kind`` can store.

    Read from the environment at call time, like ``pipeline_config``. A stored
    record's ``limits`` are compared with these values by ``_is_final``. A
    single-unit kind also has the output cap (``output_too_large``), and a DOCX
    the six guard thresholds (``zip_bomb``, ``too_complex``). A kind whose
    handler has not shipped yet may have no deadline: it then has no
    ``deadline_s`` (no ``timeout`` record of it can exist).
    """
    limits: dict[str, Any] = {}
    if kind in DEADLINE_ENV:
        limits["deadline_s"] = deadline_for(kind)
    if output_cap_bytes(kind) is not None:
        limits["max_output_mb"] = max_output_mb()
    if kind == "docx":
        limits.update(asdict(docx_guard_limits()))
    return limits


def _guard_rejection(kind: str, unit_path: Path) -> GuardRejection | None:
    """The service-side cost guard of one unit (spec D7): a rejection, or None to parse.

    DOCX only; every other kind is bounded by its raw cap and deadline.
    """
    if kind != "docx":
        return None
    return check_docx(unit_path, docx_guard_limits())


def _reason_code(reason: str | None) -> str | None:
    """The machine-readable code of a ``"<code>: <detail>"`` reason (None for no reason)."""
    if not reason:
        return None
    return reason.partition(":")[0].strip() or None


def _flag_list(flags: Any) -> list[str]:
    """``flags`` as a sorted list of distinct strings (none: an empty list)."""
    return sorted({str(flag) for flag in flags or ()})


def _is_final(record: Mapping[str, Any], limits_now: Mapping[str, Any]) -> bool:
    """Whether a stored record is a deterministic failure that must not run again.

    Final when its ``reason_code`` is in ``FINAL_REASON_CODES``, it has a ``limits``
    mapping, and every stored limit equals its current value. A record written
    before reason codes existed (no ``reason_code``) is never final. A raised (or
    otherwise changed) limit releases the record: it is then retried like any
    other non-ok record, under the same attempt counter.
    """
    if record.get("state") == STATE_OK or record.get("reason_code") not in FINAL_REASON_CODES:
        return False
    limits = record.get("limits")
    if not isinstance(limits, dict):
        return False
    return all(key in limits_now and limits_now[key] == value for key, value in limits.items())


def config_hash(config: dict[str, Any] | None = None) -> str:
    """12 hex digits naming a pipeline config: the second level of ``work/``.

    A change of model, engine or parser version gets a new hash, so its pages are
    parsed into their own directory instead of mixing with (or being refused by)
    the old ones.
    """
    canonical = json.dumps(pipeline_config() if config is None else config, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def strip_page_markers(markdown: str) -> str:
    """Remove the ``==== page N ===`` lines (evaluation gold has no markers)."""
    return _PAGE_MARKER_RE.sub("", markdown)
