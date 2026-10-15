"""
local_runner.py — Run units in a local process pool (the ``local`` executor).
"""

from __future__ import annotations

import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from typing import Any

from parser_service.observability import (
    truncate,
)
from parser_service.parsing.document import enable_converter_reuse
from parser_service.pipeline.config import logger
from parser_service.pipeline.layout import _read_attempts, _work_prefix
from parser_service.pipeline.units import _finalize_exhausted, _process_page
from parser_service.storage.store import Store, open_store


def _init_worker(threads: int) -> None:
    """Worker start-up: bound the thread count and reuse one Docling converter."""
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))
    enable_converter_reuse()


# One store per worker process, opened on the first page it is given.
_WORKER_STORES: dict[tuple[str, str | None], Store] = {}


def _process_page_task(
    location: str,
    region: str | None,
    work_id: str,
    page_number: int,
    escalate: bool,
    max_attempts: int,
    kind: str = "pdf",
) -> dict[str, Any]:
    """``_process_page`` for a worker process, which opens its own store."""
    store = _WORKER_STORES.get((location, region))
    if store is None:
        store = _WORKER_STORES[(location, region)] = open_store(location, region=region)
    return _process_page(store, work_id, page_number, escalate, max_attempts, kind=kind)


def _run_pool(
    location: str,
    region: str | None,
    work_id: str,
    pages: list[int],
    workers: int,
    escalate: bool,
    max_attempts: int,
    kind: str = "pdf",
) -> set[int]:
    """Run ``pages`` in a process pool; return the pages whose task returned.

    A page missing from the returned set means the pool broke (a worker died)
    before that page's task came back.
    """
    workers = max(1, min(workers, len(pages)))
    threads = max(1, (os.cpu_count() or 1) // workers)
    finished: set[int] = set()
    with ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_init_worker,
        initargs=(threads,),
    ) as pool:
        futures = {
            pool.submit(
                _process_page_task, location, region, work_id, n, escalate, max_attempts, kind
            ): n
            for n in pages
        }
        for future in as_completed(futures):
            page_number = futures[future]
            try:
                record = future.result()
            except BrokenProcessPool:
                continue
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "page %d: task raised %s: %s",
                    page_number,
                    type(exc).__name__,
                    truncate(str(exc)),
                )
                finished.add(page_number)
                continue
            finished.add(page_number)
            logger.info(
                "page %d: %s%s",
                page_number,
                record.get("state"),
                f" ({truncate(record.get('reason'))})" if record.get("reason") else "",
            )
    return finished


def _run_pages(
    store: Store,
    location: str,
    region: str | None,
    work_id: str,
    pages: list[int],
    workers: int,
    escalate: bool,
    max_attempts: int,
    kind: str = "pdf",
) -> None:
    """Process ``pages`` once each, surviving worker deaths.

    ``workers <= 0`` runs in this process on ``store``, one page at a time
    (debugging). Otherwise pages run in a pool whose workers each open the store at
    ``location``. When a worker dies the pool breaks; the pages that had started
    are then re-run ONE PER POOL so a page that keeps killing its worker only uses
    up its own attempts, and the rest go back to a normal pool.
    """
    if workers <= 0:
        enable_converter_reuse()
        for page_number in pages:
            _process_page(store, work_id, page_number, escalate, max_attempts, kind=kind)
        return

    remaining = list(pages)
    while remaining:
        before = {n: _read_attempts(store, work_id, n) for n in remaining}
        finished = _run_pool(
            location, region, work_id, remaining, workers, escalate, max_attempts, kind
        )
        remaining = [n for n in remaining if n not in finished]
        if not remaining:
            return

        in_doubt = [n for n in remaining if _read_attempts(store, work_id, n) > before[n]]
        logger.error(
            "a worker died; re-running %d in-flight page(s) in isolation: %s",
            len(in_doubt),
            in_doubt,
        )
        if not in_doubt:
            raise RuntimeError(
                f"worker pool broke before any page started in "
                f"{store.location(_work_prefix(work_id))}"
            )
        for page_number in in_doubt:
            while True:
                if _read_attempts(store, work_id, page_number) >= max_attempts:
                    _finalize_exhausted(store, work_id, page_number, kind)
                    break
                if _run_pool(
                    location, region, work_id, [page_number], 1, escalate, max_attempts, kind
                ):
                    break
        remaining = [n for n in remaining if n not in in_doubt]
