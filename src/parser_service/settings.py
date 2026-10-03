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

from parser_service.store import location_kind

logger = logging.getLogger(__name__)

_ENGINES = ("vlm", "textract")
_DEFAULT_ENGINE = "vlm"
_DEFAULT_MAX_ATTEMPTS = 3
_TRUE = ("1", "true")
_FALSE = ("", "0", "false")


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
    )
    logger.info(
        "settings: store_location=%s aws_region=%s escalate=%s escalation_engine=%s "
        "vlm_model=%s page_workers=%s page_max_attempts=%d scan_fastpath=%s arbitration=%s "
        "executor=%s kserve_url=%s",
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
    )
    return settings
