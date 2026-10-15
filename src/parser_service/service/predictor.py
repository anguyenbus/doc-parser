"""
predictor.py — the page pipeline's page step as a KServe custom predictor.

One request is one page of a file that the caller has already split into the
store (``parse_file_in_store(..., executor="kserve")`` does the intake, the
archive step, the status and the stitch):

    POST /v1/models/page-parser:predict
    {"instances": [{"work_id": "<sha256>/<config hash>", "page": 7, "escalate": true, "max_attempts": 3}]}
    -> {"predictions": [{"work_id": ..., "page": 7, "state": "ok", "attempt": 1, ...}]}

The page's record is written to the store by this pod (first write wins), and the
response only summarises it, so a repeated request is answered from the store.

- One page at a time per pod: a request that arrives while a page is being parsed
  is refused with 429 before any work, and the caller retries it. Queueing stays
  in the caller, and the pod's CPU is an honest autoscaling signal.
- The store location comes from this pod's settings (``PARSER_STORE_LOCATION``),
  never from a request, so a caller cannot make the service write elsewhere.
- The parse runs in one long-lived worker process (``worker.ParseWorker``,
  spec D4), never in this process: Docling and torch are only imported there.
  ``load()`` starts the worker and waits until it has parsed the bundled warm-up
  page, so ready means the models are loaded.
- A page that runs past ``PARSER_DEADLINE_PDF_PAGE_S`` gets its worker killed
  and one ``failed`` record with ``reason_code`` ``timeout`` (HTTP 200). A worker
  that dies on its own writes no record (HTTP 500); the caller resends the page.
- After a timeout or a death the model is not ready (HTTP 503, no attempt
  counted, the readiness probe fails) until a new worker, started on a
  background thread, reports ready. Three failed starts in a row: the service
  sends itself SIGTERM, and the kubelet restarts the container.
- Work for another pipeline config (``PipelineConfigMismatch``), or of a kind
  missing from this pod's ``PARSER_ENABLED_KINDS`` (``KindNotEnabled``), is
  refused with 409; a file or page the store does not have, with 400. The unit
  is routed on the manifest's ``kind``.
- Metrics (``observability.py``: units, flags, parse time, input and output
  sizes, guard rejections, timeouts, worker restarts) are on KServe's
  ``/metrics`` route; ``unit_done``, ``guard_reject``, ``unit_timeout`` and
  ``worker_restart`` are logged as one JSON object per line (spec D9).

Run (the container entrypoint):
    uv run python -m parser_service.service.predictor
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from fastapi import HTTPException
from kserve import Model, ModelServer
from kserve.errors import InvalidInput

from parser_service.observability import (
    count_timeout,
    count_worker_restart,
    log_event,
    service_metrics,
    truncate,
)
from parser_service.pipeline.config import KindNotEnabled, PipelineConfigMismatch, deadline_for
from parser_service.pipeline.remote_runner import KSERVE_MODEL_NAME, process_page_request
from parser_service.pipeline.runner import ParseRunner, WorkerDied, WorkerNotReady, WorkerStartError
from parser_service.service.worker import ParseWorker
from parser_service.settings import load_settings
from parser_service.storage.store import Store, open_store

logger = logging.getLogger(__name__)

_DEFAULT_GRACE_PERIOD = 170
_WORK_ID_RE = re.compile(r"[0-9a-f]{64}/[0-9a-f]{12}")
# Spawns in a row that fail to reach ``ready`` before the service gives up and
# sends itself SIGTERM (spec D4).
_MAX_FAILED_SPAWNS = 3
# Seconds ``stop`` waits for an in-flight respawn to end.
_STOP_RESPAWN_JOINS = 60

# Built at import, so /metrics lists every metric before the first request.
service_metrics()


class ManagedWorker(ParseRunner, Protocol):
    """What the model needs of its parse worker (``worker.ParseWorker``)."""

    @property
    def ready(self) -> bool: ...

    def start(self) -> int: ...

    def kill(self) -> None: ...


# Builds the worker; the argument is the model's ``on_lost`` hook.
WorkerFactory = Callable[[Callable[[str], None]], ManagedWorker]


def _sigterm_self() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


def parse_instance(payload: Any) -> tuple[str, int, bool, int]:
    """``(work_id, page, escalate, max_attempts)`` from a V1 request, or ``InvalidInput``.

    The work id is ``<source sha256>/<config hash>`` (``work/<work id>/`` in the store).
    """
    instances = payload.get("instances") if isinstance(payload, dict) else None
    if not isinstance(instances, list) or len(instances) != 1:
        raise InvalidInput('expected {"instances": [one page]}')
    instance = instances[0]
    if not isinstance(instance, dict):
        raise InvalidInput("the instance must be an object")
    work_id = instance.get("work_id")
    page = instance.get("page")
    escalate = instance.get("escalate", True)
    max_attempts = instance.get("max_attempts")
    if not isinstance(work_id, str) or not _WORK_ID_RE.fullmatch(work_id):
        raise InvalidInput(f"work_id must be <sha256>/<config hash>, got {work_id!r}")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise InvalidInput(f"page must be a positive integer, got {page!r}")
    if not isinstance(escalate, bool):
        raise InvalidInput(f"escalate must be true or false, got {escalate!r}")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise InvalidInput(f"max_attempts must be a positive integer, got {max_attempts!r}")
    return work_id, page, escalate, max_attempts


class PageParserModel(Model):  # type: ignore[misc]
    """Parses one page per request into ``store``, in its parse worker.

    Args:
        name: The KServe model name.
        store: Where the pages are read and the records written.
        worker_factory: Builds the parse worker from the ``on_lost`` hook
            (default: the production ``ParseWorker``).
        terminate: Called after ``_MAX_FAILED_SPAWNS`` failed starts in a row
            (default: SIGTERM to this process).
    """

    def __init__(
        self,
        name: str,
        store: Store,
        *,
        worker_factory: WorkerFactory | None = None,
        terminate: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(name)
        self._store = store
        self._slot = threading.Lock()
        factory = worker_factory or (lambda on_lost: ParseWorker(on_lost=on_lost))
        self._worker = factory(self._on_worker_lost)
        self._terminate = terminate or _sigterm_self
        self._stopping = False
        # The cause of a worker loss during the request in hand (the slot is held).
        self._lost_cause: str | None = None
        self._respawn_thread: threading.Thread | None = None

    def load(self) -> bool:
        """Start the parse worker and wait until it is warm. Docling loads there."""
        self.ready = False
        return self._spawn_until_ready()

    def start(self) -> None:
        """KServe's start hook. Readiness stays the worker's (the default sets it true)."""

    def stop(self) -> None:
        """KServe's shutdown hook (after the server drained): kill the worker's group."""
        self._stopping = True
        self.ready = False
        self._worker.kill()
        # A respawn in flight may have spawned after that kill: kill until it ends
        # (a kill makes its start fail at once; bounded all the same).
        thread = self._respawn_thread
        if thread is None or thread is threading.current_thread():
            return
        for _ in range(_STOP_RESPAWN_JOINS):
            thread.join(1.0)
            self._worker.kill()
            if not thread.is_alive():
                return

    async def healthy(self) -> bool:
        """The readiness probe and KServe's own predict gate follow the worker."""
        return self._is_ready()

    def _is_ready(self) -> bool:
        return bool(self.ready) and self._worker.ready and not self._stopping

    def _spawn_until_ready(self) -> bool:
        """Start a worker; after ``_MAX_FAILED_SPAWNS`` failures in a row, terminate."""
        for failures in range(1, _MAX_FAILED_SPAWNS + 1):
            if self._stopping:
                return False
            try:
                pid = self._worker.start()
            except WorkerStartError as exc:
                if self._stopping:
                    return False
                count_worker_restart("start_failed")
                logger.error("parse worker start %d failed: %s", failures, str(exc)[:500])
                log_event("worker_restart", cause="start_failed", state="failed")
                continue
            self.ready = True
            logger.info("parse worker %d ready", pid)
            return True
        logger.critical(
            "%d parse worker starts in a row failed; sending SIGTERM", _MAX_FAILED_SPAWNS
        )
        self._terminate()
        return False

    def _on_worker_lost(self, cause: str) -> None:
        """``ParseWorker``'s hook after a timeout or a death (the worker is reaped)."""
        self.ready = False
        self._lost_cause = cause
        count_worker_restart(cause)
        if self._stopping:
            return
        thread = threading.Thread(
            target=self._respawn, args=(cause,), name="parse-worker-respawn", daemon=True
        )
        self._respawn_thread = thread
        thread.start()

    def _respawn(self, cause: str) -> None:
        started = time.monotonic()
        ok = self._spawn_until_ready()
        log_event(
            "worker_restart",
            cause=cause,
            state="ready" if ok else "failed",
            parse_worker_pid=self._worker.pid if ok else None,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )

    async def predict(
        self,
        payload: Any,
        headers: dict[str, str] | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        work_id, page, escalate, max_attempts = parse_instance(payload)
        if not self._is_ready():
            raise HTTPException(status_code=503, detail="not ready: the parse worker is starting")
        if not self._slot.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="busy: this pod parses one page at a time")
        try:
            # Checked again under the slot: no attempt is counted without a worker.
            if not self._is_ready():
                raise HTTPException(
                    status_code=503, detail="not ready: the parse worker is starting"
                )
            self._lost_cause = None
            parse_worker_pid = self._worker.pid
            summary = await asyncio.to_thread(
                process_page_request,
                self._store,
                work_id,
                page,
                escalate,
                max_attempts,
                runner=self._worker,
            )
        except (PipelineConfigMismatch, KindNotEnabled) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise InvalidInput(str(exc)) from exc
        except WorkerNotReady as exc:
            raise HTTPException(status_code=503, detail=f"not ready: {exc}") from exc
        except WorkerDied as exc:
            logger.error("page %d of %s: %s; no record written", page, work_id, exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            self._slot.release()
        if self._lost_cause == "timeout":
            kind = str(summary.get("kind") or "pdf")
            count_timeout(kind)
            log_event(
                "unit_timeout",
                work_id=work_id,
                kind=kind,
                unit=page,
                state=summary.get("state"),
                reason_code=summary.get("reason_code"),
                attempt=summary.get("attempt"),
                deadline_s=deadline_for(kind),
                parse_worker_pid=parse_worker_pid,
                elapsed_seconds=summary.get("elapsed_seconds"),
            )
        # The reason may hold exception text: cut to 500 characters (spec D9).
        logger.info("page_done %s", json.dumps(truncate(summary), sort_keys=True))
        return {"predictions": [summary]}


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("PARSER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings(escalate=True)
    store = open_store(settings.store_location, region=settings.aws_region)
    model = PageParserModel(
        KSERVE_MODEL_NAME,
        store,
        worker_factory=lambda on_lost: ParseWorker(
            start_timeout_s=settings.worker_start_timeout_s, on_lost=on_lost
        ),
    )
    if not model.load():
        raise SystemExit("the parse worker never became ready")
    grace = int(os.environ.get("PARSER_GRACE_PERIOD", str(_DEFAULT_GRACE_PERIOD)))
    ModelServer(grace_period=grace).start([model])


if __name__ == "__main__":
    main()
