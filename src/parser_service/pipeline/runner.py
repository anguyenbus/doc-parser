"""
runner.py — the contract between the pipeline and a parse runner.

``units._process_page`` hands a unit to a ``ParseRunner`` as a ``Request`` and gets a
``Reply`` back, or one of the errors below. ``service/worker.py`` implements it with a
killable worker process; the contract lives here so the pipeline never imports the
service.
"""

from __future__ import annotations

from typing import Protocol

from parser_service.formats.result import Reply

# A request: (kind, unit_path, page_number, escalate, output_cap_bytes or None).
Request = tuple[str, str, int, bool, int | None]


class ParseRunner(Protocol):
    """Runs one parse out of process under a deadline (``service.worker.ParseWorker``).

    ``run`` returns the worker's reply, or raises ``WorkerTimeout`` (the worker
    was killed and reaped) or ``WorkerDied`` (it died on its own).
    """

    @property
    def pid(self) -> int | None: ...

    def run(self, request: Request, deadline_s: float) -> Reply: ...


class WorkerError(RuntimeError):
    """Base class of the parse worker's errors."""


class WorkerNotReady(WorkerError):
    """No warm worker: it is starting, respawning, or was stopped."""


class WorkerStartError(WorkerError):
    """A new worker did not report ``ready`` (it failed, died or timed out)."""


class WorkerTimeout(WorkerError):
    """The worker sent no reply within the deadline; it was killed and reaped."""

    def __init__(self, deadline_s: float, pid: int | None) -> None:
        self.deadline_s = deadline_s
        self.pid = pid
        super().__init__(f"parse worker {pid} sent no reply within {deadline_s} s; killed")


class WorkerDied(WorkerError):
    """The worker died during a request (EOF on the pipe). No record is written."""

    def __init__(self, pid: int | None, exitcode: int | None) -> None:
        self.pid = pid
        self.exitcode = exitcode
        super().__init__(f"parse worker {pid} died during the parse (exit code {exitcode})")
