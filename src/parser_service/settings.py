"""
settings.py — one start-up check of the environment, for the scripts.

``load_settings(...)`` reads the environment once, validates it against what the
caller is about to do, and returns an immutable ``Settings``. Every problem found
is reported together in one ``SettingsError``; the effective settings are logged
in one line. New code takes what it needs from the returned object as explicit
parameters.

Called by a script at start-up. Nothing is read at import time, and the library
(``parse_pdf_pages`` and below) never calls it: the existing call-time reads of
the environment (``pipeline_config``, ``default_workers``, the engine clients) are
unchanged, and this check only refuses a run they would get wrong.

No credentials live here: access is the instance or pod role.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

from parser_service.formats.handlers import has_handler
from parser_service.formats.sniff import KINDS
from parser_service.storage.store import location_kind

logger = logging.getLogger(__name__)

_ENGINES = ("vlm", "textract")
_DEFAULT_ENGINE = "vlm"
_DEFAULT_MAX_ATTEMPTS = 3
_TRUE = ("1", "true")
_FALSE = ("", "0", "false")

# Parse deadlines (seconds), by unit kind: the environment key and its default.
# A deadline bounds one unit on the page service; it is a limit, not part of the
# pipeline config, so changing it never re-parses finished work.
DEADLINE_ENV: dict[str, tuple[str, int]] = {
    "pdf": ("PARSER_DEADLINE_PDF_PAGE_S", 600),
    "txt": ("PARSER_DEADLINE_TXT_S", 60),  # provisional until the corpus inventory
    "docx": ("PARSER_DEADLINE_DOCX_S", 300),  # provisional until the corpus inventory
    "html": ("PARSER_DEADLINE_HTML_S", 300),  # provisional until the corpus inventory
    # eml and msg share one key (the email handler): provisional until the corpus inventory
    "eml": ("PARSER_DEADLINE_EMAIL_S", 120),
    "msg": ("PARSER_DEADLINE_EMAIL_S", 120),
}
# The driver's timeout for one page request.
REMOTE_REQUEST_TIMEOUT_ENV = "PARSER_REMOTE_REQUEST_TIMEOUT_S"
DEFAULT_REMOTE_REQUEST_TIMEOUT_S = 900
# How long the page service waits for a new parse worker to report ready.
DEFAULT_WORKER_START_TIMEOUT_S = 300
# The kinds intake routes (a comma list); every other file fails with
# ``unsupported_type``. The kill switch of a kind (spec D1, Rollout). Ships with every
# kind that has a tested handler in this image: an explicit list, so a Docling upgrade
# that reads a new format does not switch it on by itself.
ENABLED_KINDS_ENV = "PARSER_ENABLED_KINDS"
DEFAULT_ENABLED_KINDS: tuple[str, ...] = KINDS
# Raw size caps of single-unit kinds, checked at intake (``too_large``): the
# environment key and its default, in MB (1,000,000 bytes). The group that adds a
# kind's handler adds its cap here. A kind with no entry has no raw cap.
RAW_CAP_ENV: dict[str, tuple[str, int]] = {
    "txt": ("PARSER_MAX_TXT_MB", 50),  # provisional until the corpus inventory
    "docx": ("PARSER_MAX_DOCX_MB", 100),  # provisional until the corpus inventory
    "html": ("PARSER_MAX_HTML_MB", 50),  # provisional until the corpus inventory
    # eml and msg share one key: provisional until the corpus inventory
    "eml": ("PARSER_MAX_EMAIL_MB", 50),
    "msg": ("PARSER_MAX_EMAIL_MB", 50),
}
# The cap on one single-unit document's markdown, in MB (1,000,000 bytes): over it
# the parse worker replies ``output_too_large`` instead of the text. PDF pages have
# no output cap. Provisional until the corpus inventory.
MAX_OUTPUT_ENV = "PARSER_MAX_OUTPUT_MB"
DEFAULT_MAX_OUTPUT_MB = 20
# The smallest embedded image, in pixels (width x height), that sets a document's
# ``images_not_ocrd`` flag (spec D6). Read by the parse worker; a flag never
# changes a unit's state, so this is neither a limit nor part of the config.
IMAGES_MIN_PIXELS_ENV = "PARSER_IMAGES_MIN_PIXELS"
DEFAULT_IMAGES_MIN_PIXELS = 40_000
# The DOCX cost guard's thresholds (spec D7), checked by the page service before a
# DOCX reaches the parse worker: the key stored in a rejected record's ``limits``
# -> (the environment key, its default). MB are 1,000,000 bytes; the ratio is
# expanded:compressed. Provisional until the corpus inventory.
DOCX_GUARD_ENV: dict[str, tuple[str, int]] = {
    "docx_max_expanded_mb": ("PARSER_DOCX_MAX_EXPANDED_MB", 300),
    "docx_max_part_mb": ("PARSER_DOCX_MAX_PART_MB", 200),
    "docx_max_ratio": ("PARSER_DOCX_MAX_RATIO", 100),
    "docx_max_entries": ("PARSER_DOCX_MAX_ENTRIES", 10_000),
    "docx_max_paragraphs": ("PARSER_DOCX_MAX_PARAGRAPHS", 50_000),
    "docx_max_table_cells": ("PARSER_DOCX_MAX_TABLE_CELLS", 100_000),
}
# The request timeout must exceed the largest deadline by this much: room for the
# source download, the guards, the kill of a timed-out worker and the record write.
_DEADLINE_MARGIN_S = 120


class SettingsError(ValueError):
    """The environment cannot be used for this run. ``problems`` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Settings:
    """The validated settings of one run."""

    store_location: str
    store_kind: str  # "local" or "s3"
    aws_region: str | None
    vlm_model: str | None
    escalation_engine: str
    page_workers: int | None  # None: the pipeline's own default
    page_max_attempts: int
    scan_fastpath: bool
    arbitration: bool
    executor: str = "local"  # "local" (worker processes) or "kserve" (the page service)
    kserve_url: str | None = None
    kserve_concurrency: int | None = None  # None: the pipeline's own default
    deadline_pdf_page_s: int = DEADLINE_ENV["pdf"][1]
    remote_request_timeout_s: int = DEFAULT_REMOTE_REQUEST_TIMEOUT_S
    worker_start_timeout_s: int = DEFAULT_WORKER_START_TIMEOUT_S
    enabled_kinds: tuple[str, ...] = DEFAULT_ENABLED_KINDS


def _integer(
    env: Mapping[str, str], name: str, minimum: int, default: int | None, problems: list[str]
) -> int | None:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = minimum - 1
    if value < minimum:
        problems.append(f"{name} must be an integer of {minimum} or more, got {raw!r}")
        return default
    return value


def _flag(env: Mapping[str, str], name: str, problems: list[str]) -> bool:
    raw = env.get(name, "").lower()
    if raw not in _TRUE + _FALSE:
        problems.append(f"{name} must be unset, empty, 1, true, 0 or false, got {env[name]!r}")
    return raw in _TRUE


def split_kinds(raw: str | None) -> tuple[str, ...]:
    """The kinds named in a ``PARSER_ENABLED_KINDS`` value, lower-cased, in order, once each.

    None (unset) gives ``DEFAULT_ENABLED_KINDS``. Names are not checked here.
    """
    if raw is None:
        return DEFAULT_ENABLED_KINDS
    kinds: list[str] = []
    for part in raw.split(","):
        kind = part.strip().lower()
        if kind and kind not in kinds:
            kinds.append(kind)
    return tuple(kinds)


def _enabled_kinds(env: Mapping[str, str], problems: list[str]) -> tuple[str, ...]:
    """``PARSER_ENABLED_KINDS``: known kinds, each with a handler and a deadline in this image.

    A kind whose handler has not shipped yet is refused, so it cannot be turned on
    before its code exists (files would route to a missing handler).
    """
    raw = env.get(ENABLED_KINDS_ENV)
    kinds = split_kinds(raw)
    if not kinds:
        problems.append(f"{ENABLED_KINDS_ENV} must name at least one kind, got {raw!r}")
        return DEFAULT_ENABLED_KINDS
    unknown = [kind for kind in kinds if kind not in KINDS]
    if unknown:
        problems.append(
            f"{ENABLED_KINDS_ENV} names unknown kind(s) {', '.join(unknown)}; "
            f"known: {', '.join(KINDS)}"
        )
    missing = [kind for kind in kinds if kind in KINDS and not has_handler(kind)]
    if missing:
        problems.append(
            f"{ENABLED_KINDS_ENV} enables {', '.join(missing)}, which has no handler in this image"
        )
    no_deadline = [
        kind for kind in kinds if kind in KINDS and has_handler(kind) and kind not in DEADLINE_ENV
    ]
    if no_deadline:
        problems.append(
            f"{ENABLED_KINDS_ENV} enables {', '.join(no_deadline)}, which has no parse deadline"
        )
    return kinds


def _size_caps(env: Mapping[str, str], problems: list[str]) -> None:
    """Every raw cap in ``RAW_CAP_ENV``, the output cap, the image flag's pixel
    threshold and every DOCX guard threshold in ``DOCX_GUARD_ENV`` is a positive
    integer."""
    for name, default in dict(RAW_CAP_ENV.values()).items():  # a shared key is read once
        _integer(env, name, 1, default, problems)
    _integer(env, MAX_OUTPUT_ENV, 1, DEFAULT_MAX_OUTPUT_MB, problems)
    _integer(env, IMAGES_MIN_PIXELS_ENV, 1, DEFAULT_IMAGES_MIN_PIXELS, problems)
    for name, default in DOCX_GUARD_ENV.values():
        _integer(env, name, 1, default, problems)


def _deadlines(
    env: Mapping[str, str], enabled: tuple[str, ...], problems: list[str]
) -> tuple[int, int, int]:
    """``(PDF-page deadline, request timeout, worker start timeout)``, all positive integers.

    The driver's request timeout must be at least the largest parse deadline of an
    enabled kind plus ``_DEADLINE_MARGIN_S``, so a page service always answers
    (with a ``timeout`` record) before the driver gives up on the request and
    sends the page again.
    """
    found = len(problems)
    # Each key is read (and reported) once, also when kinds share it (eml and msg).
    by_name = {
        name: _integer(env, name, 1, default, problems)
        for name, default in dict(DEADLINE_ENV.values()).items()
    }
    deadlines = {kind: by_name[name] for kind, (name, _default) in DEADLINE_ENV.items()}
    request_timeout = _integer(
        env, REMOTE_REQUEST_TIMEOUT_ENV, 1, DEFAULT_REMOTE_REQUEST_TIMEOUT_S, problems
    )
    worker_start_timeout = _integer(
        env, "PARSER_WORKER_START_TIMEOUT_S", 1, DEFAULT_WORKER_START_TIMEOUT_S, problems
    )
    assert request_timeout is not None and worker_start_timeout is not None
    in_use = {kind: value for kind, value in deadlines.items() if kind in enabled}
    if len(problems) == found and in_use:  # every value is valid: check their order
        kind, largest = max(in_use.items(), key=lambda item: item[1] or 0)
        assert largest is not None
        if request_timeout < largest + _DEADLINE_MARGIN_S:
            problems.append(
                f"{REMOTE_REQUEST_TIMEOUT_ENV} must be at least the largest parse deadline plus "
                f"{_DEADLINE_MARGIN_S} s: {DEADLINE_ENV[kind][0]} is {largest}, so at least "
                f"{largest + _DEADLINE_MARGIN_S}, got {request_timeout}"
            )
    pdf_page = deadlines["pdf"]
    assert pdf_page is not None
    return pdf_page, request_timeout, worker_start_timeout


def load_settings(
    *,
    store_location: str | None = None,
    input_is_s3: bool = False,
    escalate: bool = True,
    executor: str | None = None,
    kserve_url: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Validate the environment for one run and return its settings.

    Args:
        store_location: The caller's override of ``PARSER_STORE_LOCATION`` (a
            ``--work-dir`` argument), or None to use the environment.
        executor: The caller's override of ``PARSER_EXECUTOR`` (``local`` or
            ``kserve``). kserve needs ``PARSER_KSERVE_URL`` (or ``kserve_url``) and
            an ``s3://`` store, which the page service writes to.
        kserve_url: The caller's override of ``PARSER_KSERVE_URL``.
        input_is_s3: The input is an ``s3://`` URI (it then needs ``AWS_REGION``).
        escalate: Engine calls may be made (they need ``AWS_REGION``, and the VLM
            engine needs ``BEDROCK_VLM_MODEL``).
        environ: The environment to read (default ``os.environ``).

    Raises:
        SettingsError: one error naming every missing or invalid value.
    """
    env = dict(os.environ if environ is None else environ)
    problems: list[str] = []

    configured = env.get("PARSER_STORE_LOCATION") or None
    location = store_location or configured
    if store_location and configured and store_location != configured:
        logger.info(
            "store location: --work-dir %s overrides PARSER_STORE_LOCATION %s",
            store_location,
            configured,
        )
    store_kind = "local"
    if location is None:
        problems.append("no store location: set PARSER_STORE_LOCATION or pass --work-dir")
    else:
        try:
            store_kind = location_kind(location)
        except ValueError as exc:
            problems.append(
                f"store location is not a directory path or s3://bucket[/prefix]: {exc}"
            )

    engine = env.get("PARSER_ESCALATION_ENGINE", _DEFAULT_ENGINE)
    if engine not in _ENGINES:
        problems.append(f"PARSER_ESCALATION_ENGINE must be vlm or textract, got {engine!r}")

    region = env.get("AWS_REGION", "").strip() or None
    needs_region = [
        reason
        for needed, reason in (
            (store_kind == "s3", "the store location is s3://"),
            (input_is_s3, "the input is s3://"),
            (escalate, "escalation is on"),
        )
        if needed
    ]
    if region is None and needs_region:
        problems.append(f"AWS_REGION is required ({', '.join(needs_region)})")

    vlm_model = env.get("BEDROCK_VLM_MODEL", "").strip() or None
    if vlm_model is None and escalate and engine == "vlm":
        problems.append("BEDROCK_VLM_MODEL is required (escalation is on and the engine is vlm)")

    workers = _integer(env, "PARSER_PAGE_WORKERS", 0, None, problems)
    max_attempts = _integer(env, "PARSER_PAGE_MAX_ATTEMPTS", 1, _DEFAULT_MAX_ATTEMPTS, problems)
    scan_fastpath = _flag(env, "PARSER_SCAN_FASTPATH", problems)
    arbitration = _flag(env, "PARSER_ESCALATION_ARBITRATION", problems)

    executor = executor or env.get("PARSER_EXECUTOR", "").strip() or "local"
    url = kserve_url or env.get("PARSER_KSERVE_URL", "").strip() or None
    concurrency = _integer(env, "PARSER_KSERVE_CONCURRENCY", 1, None, problems)
    if executor not in ("local", "kserve"):
        problems.append(f"PARSER_EXECUTOR must be local or kserve, got {executor!r}")
    elif executor == "kserve":
        if url is None:
            problems.append("PARSER_KSERVE_URL is required (the executor is kserve)")
        elif not url.startswith(("http://", "https://")):
            problems.append(f"PARSER_KSERVE_URL must be an http:// or https:// URL, got {url!r}")
        if location is not None and store_kind != "s3":
            problems.append(
                "the kserve executor needs an s3:// store location: the page service "
                "writes the page records there"
            )

    enabled_kinds = _enabled_kinds(env, problems)
    _size_caps(env, problems)
    deadline_pdf_page, request_timeout, worker_start_timeout = _deadlines(
        env, enabled_kinds, problems
    )

    if problems or location is None or max_attempts is None:
        raise SettingsError(problems)

    settings = Settings(
        store_location=location,
        store_kind=store_kind,
        aws_region=region,
        vlm_model=vlm_model,
        escalation_engine=engine,
        page_workers=workers,
        page_max_attempts=max_attempts,
        scan_fastpath=scan_fastpath,
        arbitration=arbitration,
        executor=executor,
        kserve_url=url if executor == "kserve" else None,
        kserve_concurrency=concurrency,
        deadline_pdf_page_s=deadline_pdf_page,
        remote_request_timeout_s=request_timeout,
        worker_start_timeout_s=worker_start_timeout,
        enabled_kinds=enabled_kinds,
    )
    logger.info(
        "settings: store_location=%s aws_region=%s escalate=%s escalation_engine=%s "
        "vlm_model=%s page_workers=%s page_max_attempts=%d scan_fastpath=%s arbitration=%s "
        "executor=%s kserve_url=%s deadline_pdf_page_s=%d remote_request_timeout_s=%d "
        "worker_start_timeout_s=%d enabled_kinds=%s",
        settings.store_location,
        settings.aws_region,
        escalate,
        settings.escalation_engine,
        settings.vlm_model,
        "default" if settings.page_workers is None else settings.page_workers,
        settings.page_max_attempts,
        settings.scan_fastpath,
        settings.arbitration,
        settings.executor,
        settings.kserve_url,
        settings.deadline_pdf_page_s,
        settings.remote_request_timeout_s,
        settings.worker_start_timeout_s,
        ",".join(settings.enabled_kinds),
    )
    return settings
