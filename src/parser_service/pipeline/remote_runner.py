"""
remote_runner.py — Run units on the KServe page service (the ``kserve`` executor): the driver's
client and the service's side of a request.
"""

from __future__ import annotations

import http.client
import json
import random
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from parser_service.formats.sniff import KIND_EXTENSIONS, KINDS
from parser_service.pipeline import layout
from parser_service.pipeline.config import (
    KindNotEnabled,
    PipelineConfigMismatch,
    config_hash,
    enabled_kinds,
    logger,
    manifest_kind,
    pipeline_config,
)
from parser_service.pipeline.layout import _manifest_key, _read_attempts, _read_json, _work_prefix
from parser_service.pipeline.units import ParseRunner, _finalize_exhausted, _process_page
from parser_service.settings import (
    ENABLED_KINDS_ENV,
)
from parser_service.storage.store import Store

EXECUTOR_LOCAL = "local"
EXECUTOR_KSERVE = "kserve"
KSERVE_MODEL_NAME = "page-parser"

# 429: the pod is parsing another page; 503: the model is not ready yet. Neither
# reached _process_page, so neither costs an attempt.
_REMOTE_BUSY_CODES = (429, 503)
_REMOTE_BUSY_DEADLINE = 3600.0
# Failed requests allowed beyond max_attempts before a page that never starts
# (no attempt counted) is reported instead of retried forever.
_REMOTE_EXTRA_ERRORS = 5
_sleep = time.sleep  # replaced in tests


class RemotePageError(RuntimeError):
    """The page service refused a page for a reason a retry cannot fix, or never took it."""


def kserve_predict_url(endpoint: str) -> str:
    """The V1 predict URL of the page model at ``endpoint`` (``http://host[:port]``)."""
    return f"{endpoint.rstrip('/')}/v1/models/{KSERVE_MODEL_NAME}:predict"


def process_page_request(
    store: Store,
    work_id: str,
    page_number: int,
    escalate: bool,
    max_attempts: int,
    *,
    runner: ParseRunner | None = None,
) -> dict[str, Any]:
    """Serve one unit request: the page service's side of the remote executor.

    The file must already be taken in under ``store``, under this process's
    pipeline config for its kind: the work id's config hash must be the hash of
    some kind's ``pipeline_config``, and the manifest's config must equal
    ``pipeline_config(manifest kind)``. A unit parsed with other settings than
    the ones pinned at intake would silently mix outputs, so a mismatch raises
    ``PipelineConfigMismatch``. A kind missing from this pod's
    ``PARSER_ENABLED_KINDS`` raises ``KindNotEnabled``. A page number outside the
    file raises ``ValueError``. The unit is routed on the manifest's ``kind``.
    Otherwise this is
    ``_process_page``: a page already recorded is returned as stored, and a
    page-level failure is a ``failed`` record, never an exception. With ``runner``
    (the page service's parse worker) the parse runs there under the deadline, and
    a worker death raises ``WorkerDied`` with no record written.

    Returns a summary of the record; the record itself is in the store.
    """
    requested_config = work_id.rpartition("/")[2]
    known = {config_hash(pipeline_config(kind)): kind for kind in KINDS}
    if requested_config not in known:
        raise PipelineConfigMismatch(
            f"{work_id} is work for config {requested_config} but this page service runs "
            f"none of that hash (pdf: {config_hash()} {pipeline_config()})"
        )
    manifest = _read_json(store, _manifest_key(work_id))
    if not isinstance(manifest, dict):
        raise ValueError(f"{store.location(_work_prefix(work_id))} has no manifest; split it first")
    kind = manifest_kind(manifest)
    if kind not in KIND_EXTENSIONS:
        raise ValueError(f"{work_id} has a manifest of unknown kind {kind!r}")
    if known[requested_config] != kind or manifest.get("config") != pipeline_config(kind):
        raise PipelineConfigMismatch(
            f"{work_id} was taken in with config {manifest.get('config')} but this page "
            f"service runs {pipeline_config(kind)} for {kind}"
        )
    if kind not in enabled_kinds():
        raise KindNotEnabled(
            f"{work_id} is a {kind} file, and {kind} is not in this page service's "
            f"{ENABLED_KINDS_ENV} ({','.join(enabled_kinds())})"
        )
    if not 1 <= page_number <= int(manifest["page_count"]):
        raise ValueError(f"{work_id} has {manifest['page_count']} pages, not page {page_number}")
    record = _process_page(
        store, work_id, page_number, escalate, max_attempts, runner=runner, kind=kind
    )
    return {
        "work_id": work_id,
        "kind": kind,
        "page": page_number,
        "state": record.get("state"),
        "reason": record.get("reason"),
        "reason_code": record.get("reason_code"),
        "attempt": record.get("attempt"),
        "route": record.get("route"),
        "elapsed_seconds": record.get("elapsed_seconds"),
        "worker_host": record.get("worker_host"),
    }


def _post_page(
    url: str, work_id: str, page_number: int, escalate: bool, max_attempts: int, timeout: float
) -> tuple[int, dict[str, Any] | None, str]:
    """POST one page; return ``(status, body, error detail)``.

    One connection per request (``Connection: close``): a kept-alive connection
    stays on the pod it first reached, so pods added by a scale-out would get no
    pages. A transport error (refused, reset, timeout) raises.
    """
    instance = {
        "work_id": work_id,
        "page": page_number,
        "escalate": escalate,
        "max_attempts": max_attempts,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps({"instances": [instance]}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Connection": "close"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read()), ""
    except urllib.error.HTTPError as exc:
        return exc.code, None, exc.read().decode("utf-8", "replace")[:500]


def _remote_page(
    store: Store,
    url: str,
    work_id: str,
    page_number: int,
    escalate: bool,
    max_attempts: int,
    stop: threading.Event,
    request_timeout: float,
) -> dict[str, Any]:
    """Send one page until it has a result; return the summary or the record.

    Each request waits at most ``request_timeout`` seconds
    (``PARSER_REMOTE_REQUEST_TIMEOUT_S``), which settings keep above every parse
    deadline, so a page that times out on the service is answered, not resent.

    - 200: the service parsed the page (or returned its stored record).
    - 429/503: the service did not start it. Retried with jittered backoff at no
      attempt cost, for at most ``busy_deadline`` seconds of continuous refusals.
    - Any other 4xx: a request the service will never accept (config mismatch, a
      page or file it does not know). Raises ``RemotePageError``.
    - A transport error or other 5xx: the page may have started on a pod that died.
      The store decides: a record means the page is done; an attempt counter at
      the limit means the page is finalized ``failed``; otherwise it is sent again.
      A page whose requests keep failing without it ever starting raises.
    """
    timeout, busy_deadline = request_timeout, _REMOTE_BUSY_DEADLINE
    busy_since: float | None = None
    backoff = 0.5
    errors = 0
    while True:
        if stop.is_set():
            raise RemotePageError(f"page {page_number}: stopped after another page failed")
        try:
            status, body, detail = _post_page(
                url, work_id, page_number, escalate, max_attempts, timeout
            )
        except (OSError, http.client.HTTPException) as exc:  # URLError is an OSError
            status, body, detail = 0, None, f"{type(exc).__name__}: {exc}"

        if status == 200 and body and body.get("predictions"):
            return dict(body["predictions"][0])
        if status in _REMOTE_BUSY_CODES:
            now = time.monotonic()
            busy_since = now if busy_since is None else busy_since
            if now - busy_since > busy_deadline:
                raise RemotePageError(
                    f"page {page_number}: the page service refused it for {busy_deadline:.0f}s "
                    f"({status}: {detail})"
                )
            _sleep(random.uniform(0.5, 1.0) * backoff)
            backoff = min(backoff * 2, 8.0)
            continue
        if 400 <= status < 500:
            raise RemotePageError(
                f"page {page_number}: the page service refused it ({status}: {detail})"
            )

        busy_since, backoff = None, 0.5
        errors += 1
        logger.warning(
            "page %d: request failed (%s); checking the store", page_number, detail or status
        )
        record = layout._load_record_checked(store, work_id, page_number)
        if record is not None:
            return record
        if _read_attempts(store, work_id, page_number) >= max_attempts:
            return _finalize_exhausted(store, work_id, page_number)
        if errors > max_attempts + _REMOTE_EXTRA_ERRORS:
            raise RemotePageError(
                f"page {page_number}: {errors} requests failed without the page starting; "
                f"last: {detail or status}"
            )
        _sleep(random.uniform(0.5, 2.0))


def _run_pages_remote(
    store: Store,
    endpoint: str,
    work_id: str,
    pages: list[int],
    concurrency: int,
    escalate: bool,
    max_attempts: int,
    request_timeout: float,
) -> None:
    """Process ``pages`` once each on the page service at ``endpoint``.

    Up to ``concurrency`` requests are in flight; the service admits one page per
    pod and refuses the rest with 429, so the queue lives here and the service
    scales on its own load. The first ``RemotePageError`` stops the other pages
    and propagates; pages without a record stay ``pending``.
    """
    url = kserve_predict_url(endpoint)
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(pages)))) as pool:
        futures = {
            pool.submit(
                _remote_page,
                store,
                url,
                work_id,
                n,
                escalate,
                max_attempts,
                stop,
                request_timeout,
            ): n
            for n in pages
        }
        try:
            for future in as_completed(futures):
                page_number = futures[future]
                summary = future.result()
                logger.info(
                    "page %d: %s%s%s",
                    page_number,
                    summary.get("state"),
                    f" ({summary.get('reason')})" if summary.get("reason") else "",
                    f" on {summary['worker_host']}" if summary.get("worker_host") else "",
                )
        except BaseException:
            stop.set()
            for future in futures:
                future.cancel()
            raise
