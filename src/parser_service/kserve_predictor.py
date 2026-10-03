"""
kserve_predictor.py — the page pipeline's page step as a KServe custom predictor.

One request is one page of a file that the caller has already split into the
store (``parse_pdf_pages_in_store(..., executor="kserve")`` does the split, the
archive step, the status and the stitch):

    POST /v1/models/page-parser:predict
    {"instances": [{"file_id": "...", "page": 7, "escalate": true, "max_attempts": 3}]}
    -> {"predictions": [{"file_id": ..., "page": 7, "state": "ok", "attempt": 1, ...}]}

The page's record is written to the store by this pod (first write wins), and the
response only summarises it, so a repeated request is answered from the store.

- One page at a time per pod: a request that arrives while a page is being parsed
  is refused with 429 before any work, and the caller retries it. Queueing stays
  in the caller, and the pod's CPU is an honest autoscaling signal.
- The store location comes from this pod's settings (``PARSER_STORE_LOCATION``),
  never from a request, so a caller cannot make the service write elsewhere.
- Readiness means the models are loaded: ``load()`` parses a bundled one-page PDF
  before the server reports the model ready.
- A file split under another pipeline config (``PipelineConfigMismatch``) is
  refused with 409; a file or page the store does not have, with 400.

Run (the container entrypoint):
    uv run python -m parser_service.kserve_predictor
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from kserve import Model, ModelServer
from kserve.errors import InvalidInput

from parser_service.markdown_pipeline import enable_converter_reuse, parse_to_markdown
from parser_service.page_pipeline import (
    KSERVE_MODEL_NAME,
    PipelineConfigMismatch,
    process_page_request,
)
from parser_service.settings import load_settings
from parser_service.store import Store, open_store

logger = logging.getLogger(__name__)

WARMUP_PDF = Path(__file__).parent / "assets" / "warmup.pdf"
_DEFAULT_GRACE_PERIOD = 170


def parse_instance(payload: Any) -> tuple[str, int, bool, int]:
    """``(file_id, page, escalate, max_attempts)`` from a V1 request, or ``InvalidInput``."""
    instances = payload.get("instances") if isinstance(payload, dict) else None
    if not isinstance(instances, list) or len(instances) != 1:
        raise InvalidInput('expected {"instances": [one page]}')
    instance = instances[0]
    if not isinstance(instance, dict):
        raise InvalidInput("the instance must be an object")
    file_id = instance.get("file_id")
    page = instance.get("page")
    escalate = instance.get("escalate", True)
    max_attempts = instance.get("max_attempts")
    if not isinstance(file_id, str) or not file_id or "/" in file_id or file_id.startswith("."):
        raise InvalidInput(f"file_id must be a file id, got {file_id!r}")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise InvalidInput(f"page must be a positive integer, got {page!r}")
    if not isinstance(escalate, bool):
        raise InvalidInput(f"escalate must be true or false, got {escalate!r}")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise InvalidInput(f"max_attempts must be a positive integer, got {max_attempts!r}")
    return file_id, page, escalate, max_attempts


class PageParserModel(Model):  # type: ignore[misc]
    """Parses one page per request into ``store``."""

    def __init__(self, name: str, store: Store, *, warmup: bool = True) -> None:
        super().__init__(name)
        self._store = store
        self._warmup = warmup
        self._slot = threading.Lock()

    def load(self) -> bool:
        enable_converter_reuse()
        if self._warmup:
            parse_to_markdown(WARMUP_PDF, escalate=False)
        self.ready = True
        return True

    async def predict(
        self,
        payload: Any,
        headers: dict[str, str] | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        file_id, page, escalate, max_attempts = parse_instance(payload)
        if not self._slot.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="busy: this pod parses one page at a time")
        try:
            summary = await asyncio.to_thread(
                process_page_request, self._store, file_id, page, escalate, max_attempts
            )
        except PipelineConfigMismatch as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise InvalidInput(str(exc)) from exc
        finally:
            self._slot.release()
        logger.info("page_done %s", json.dumps(summary, sort_keys=True))
        return {"predictions": [summary]}


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("PARSER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings(escalate=True)
    store = open_store(settings.store_location, region=settings.aws_region)
    model = PageParserModel(KSERVE_MODEL_NAME, store)
    model.load()
    grace = int(os.environ.get("PARSER_GRACE_PERIOD", str(_DEFAULT_GRACE_PERIOD)))
    ModelServer(grace_period=grace).start([model])


if __name__ == "__main__":
    main()
