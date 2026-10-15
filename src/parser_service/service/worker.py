"""
page_worker.py — the page service's long-lived parse worker process (spec D4, Option A).

The page service keeps the store I/O, the attempt counter, the guards and the
record write. One worker process per pod does the parsing, so a parse that runs
past its deadline can be killed without killing the service:

    service process                          worker process (spawned)
    ---------------                          ------------------------
    ParseWorker.start()  ── spawn ─────────► os.setsid(); warm-up (Docling loaded
                         ◄── ("ready", pid)   here, never in the service)
    ParseWorker.run(request, deadline)
        send (kind, unit_path, page_number, escalate, output_cap_bytes)
                                             parse → (state, reason, reason_code,
                         ◄── reply              markdown, extra)
        conn.poll(deadline) expires:
        SIGKILL the worker's process group, join it, raise WorkerTimeout
        EOF on the pipe (the worker died on its own):
        reap it, raise WorkerDied

- The worker runs in its own session (``os.setsid`` at start), so
  ``os.killpg(pid, SIGKILL)`` also kills any grandchild, and SIGKILL needs no
  cooperation from native code (a hung pdfium or Docling call, SIGTERM ignored).
- A Python exception inside the parse becomes a ``failed`` reply; it never kills
  the worker.
- The worker enforces the output cap before it replies, so an oversized text
  never crosses the pipe. PDF pages have no output cap.
- The worker never writes to the store: only the service writes records, so a
  timed-out unit gets exactly one record (``page_pipeline._process_page``).
- ``start()`` kills and reaps the previous worker before it spawns the next, so a
  pod never holds two working sets.

``ParseWorker`` is the ``runner`` that ``page_pipeline._process_page`` accepts.
The parse target and the warm-up are module-level functions (``spawn`` pickles
them by reference); tests pass their own sleeping or crashing targets.
"""

from __future__ import annotations

import atexit
import logging
import multiprocessing
import multiprocessing.util  # noqa: F401 — its atexit hook must be registered before ours
import os
import signal
import threading
import time
import weakref
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path

from parser_service.formats.handlers import parse_unit
from parser_service.formats.result import Reply, apply_output_cap, exception_reply
from parser_service.settings import DEFAULT_WORKER_START_TIMEOUT_S

logger = logging.getLogger(__name__)

WARMUP_PDF = Path(__file__).parents[1] / "assets" / "warmup.pdf"
WORKER_START_TIMEOUT_ENV = "PARSER_WORKER_START_TIMEOUT_S"

# How long ``kill`` waits for the SIGKILLed worker to be reaped.
_JOIN_TIMEOUT_S = 30.0
# How long a worker that closed its pipe is given to exit on its own.
_DEATH_WAIT_S = 5.0
# Exception text kept in a reply's reason and in log lines (spec D9).
_MAX_EXCEPTION_CHARS = 500

# A request: (kind, unit_path, page_number, escalate, output_cap_bytes or None).
Request = tuple[str, str, int, bool, int | None]
# A parse target: (kind, unit_path, page_number, escalate) -> Reply.
ParseTarget = Callable[[str, str, int, bool], Reply]


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


# ---------------------------------------------------------------------------
# Worker side (runs in the spawned process)
# ---------------------------------------------------------------------------


def warm_up() -> None:
    """The production warm-up: reuse one converter and parse the bundled page.

    Loads the Docling models in the worker, so the first real request is warm.
    """
    from parser_service.parsing.document import enable_converter_reuse, parse_to_markdown

    enable_converter_reuse()
    parse_to_markdown(WARMUP_PDF, escalate=False)


def parse_request(kind: str, unit_path: str, page_number: int, escalate: bool) -> Reply:
    """The production parse target: ``handlers.parse_unit`` for the request's kind.

    A kind with no handler in this image raises, which ``_worker_main`` turns into
    a ``failed`` reply. The output cap is applied by ``_worker_main``.
    """
    return parse_unit(kind, Path(unit_path), page_number, escalate)


def _worker_main(conn: Connection, target: ParseTarget, warmup: Callable[[], None] | None) -> None:
    """The worker process: new session, warm-up, ``ready``, then serve requests.

    Exits when the pipe closes (the service went away) or on a ``None`` request.
    """
    os.setsid()
    try:
        if warmup is not None:
            warmup()
    except BaseException as exc:  # noqa: BLE001 — reported to the service, then exit
        conn.send(("start_failed", f"{type(exc).__name__}: {exc}"[:_MAX_EXCEPTION_CHARS]))
        conn.close()
        os._exit(1)
    conn.send(("ready", os.getpid()))
    while True:
        try:
            request = conn.recv()
        except (EOFError, OSError):
            return
        if request is None:
            return
        kind, unit_path, page_number, escalate, cap_bytes = request
        try:
            reply = target(kind, unit_path, page_number, escalate)
        except Exception as exc:  # noqa: BLE001 — a parse error is a reply, not a dead worker
            logger.error("page %s: parse raised %s", page_number, type(exc).__name__)
            reply = exception_reply(exc)
        reply = apply_output_cap(kind, reply, cap_bytes)
        try:
            conn.send(reply)
        except (EOFError, OSError):
            return
        except Exception as exc:  # noqa: BLE001 — e.g. an unpicklable field in extra
            conn.send(exception_reply(exc))


# ---------------------------------------------------------------------------
# Service side
# ---------------------------------------------------------------------------

# Every live handle, so the service's exit never leaves a worker behind.
_HANDLES: weakref.WeakSet[ParseWorker] = weakref.WeakSet()


def _kill_all_workers() -> None:
    for handle in list(_HANDLES):
        handle.kill()


# Registered after multiprocessing's own exit hook, so it runs first (LIFO): the
# worker is killed before multiprocessing joins its non-daemon children.
atexit.register(_kill_all_workers)


def _default_start_timeout() -> float:
    return float(os.environ.get(WORKER_START_TIMEOUT_ENV, str(DEFAULT_WORKER_START_TIMEOUT_S)))


def _wait_exit(pid: int, seconds: float) -> None:
    """Wait up to ``seconds`` for child ``pid`` to exit, WITHOUT reaping it.

    The unreaped child keeps its PID, and with it its process group id, reserved,
    so the group can then be SIGKILLed (grandchildren included) with no risk of
    hitting a process that reused the id.
    """
    give_up = time.monotonic() + seconds
    while True:
        try:
            if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None:
                return
        except ChildProcessError:
            return
        if time.monotonic() >= give_up:
            return
        time.sleep(0.01)


class ParseWorker:
    """Handle on the one parse worker of a page service pod.

    Not a pool: one request at a time (the caller's slot lock ensures it).
    ``start`` and ``kill`` are safe to call from any thread.

    Args:
        target: Module-level parse function run in the worker for each request.
        warmup: Module-level function run once in the worker before ``ready``
            (None: no warm-up).
        start_timeout_s: How long ``start`` waits for ``ready``
            (default ``PARSER_WORKER_START_TIMEOUT_S``, 300).
        on_lost: Called with ``"timeout"`` or ``"died"`` after a worker was killed
            or found dead during ``run`` and has been reaped. The page service uses
            it to respawn on a background thread.
    """

    def __init__(
        self,
        target: ParseTarget = parse_request,
        warmup: Callable[[], None] | None = warm_up,
        *,
        start_timeout_s: float | None = None,
        on_lost: Callable[[str], None] | None = None,
    ) -> None:
        self._target = target
        self._warmup = warmup
        self._start_timeout_s = (
            _default_start_timeout() if start_timeout_s is None else start_timeout_s
        )
        self._on_lost = on_lost
        self._life = threading.Lock()
        self._process: multiprocessing.process.BaseProcess | None = None
        self._conn: Connection | None = None
        self._ready = False
        _HANDLES.add(self)

    @property
    def pid(self) -> int | None:
        """PID of the current worker process (None when there is none)."""
        process = self._process
        return process.pid if process is not None else None

    @property
    def ready(self) -> bool:
        """True from ``ready`` until the worker is killed, found dead or stopped."""
        return self._ready

    def start(self) -> int:
        """Kill and reap any current worker, spawn a new one, wait for ``ready``.

        Returns the new worker's PID. Raises ``WorkerStartError`` (with the worker
        reaped) when it reports a start failure, dies, is killed meanwhile, or is
        not ready within the start timeout. The lock is not held while waiting, so
        ``kill`` (for example at shutdown) never waits for a warm-up.
        """
        with self._life:
            self._kill_locked()
            context = multiprocessing.get_context("spawn")
            parent_conn, child_conn = context.Pipe(duplex=True)
            process = context.Process(
                target=_worker_main,
                args=(child_conn, self._target, self._warmup),
                name="parse-worker",
            )
            process.start()
            # Only the worker holds the child end now, so its death is EOF here.
            child_conn.close()
            self._process, self._conn = process, parent_conn
        pid = process.pid
        assert pid is not None
        started = time.monotonic()
        problem: str | None = None
        try:
            if not parent_conn.poll(self._start_timeout_s):
                problem = f"not ready within {self._start_timeout_s} s"
            else:
                message = parent_conn.recv()
                if message[0] != "ready":
                    problem = f"start failed: {message[1]}"
        except (EOFError, OSError):
            problem = "died or was killed while starting"
        with self._life:
            if self._process is not process:
                raise WorkerStartError(f"parse worker {pid} was replaced or killed while starting")
            if problem is not None:
                _wait_exit(pid, _DEATH_WAIT_S if problem.startswith("died") else 0.0)
                exitcode = self._kill_locked()
                raise WorkerStartError(f"parse worker {pid} {problem} (exit code {exitcode})")
            self._ready = True
        logger.info("parse worker %d ready after %.1f s", pid, time.monotonic() - started)
        return pid

    def run(self, request: Request, deadline_s: float) -> Reply:
        """Send one request and wait at most ``deadline_s`` seconds for its reply.

        Raises ``WorkerNotReady`` when there is no warm worker, ``WorkerTimeout``
        after the deadline (the worker's process group is SIGKILLed and reaped
        first), and ``WorkerDied`` when the worker died during the request.
        """
        conn, pid = self._conn, self.pid
        if not self._ready or conn is None:
            raise WorkerNotReady("no warm parse worker")
        try:
            conn.send(request)
            if conn.poll(deadline_s):
                reply: Reply = conn.recv()
                return reply
        except (EOFError, OSError):
            self._ready = False
            exitcode = self._reap_dead()
            logger.error("parse worker %s died during a request (exit code %s)", pid, exitcode)
            self._lost("died")
            raise WorkerDied(pid, exitcode) from None
        # The deadline expired: no longer ready before the kill.
        self._ready = False
        self.kill()
        logger.error("parse worker %s: no reply within %s s; killed", pid, deadline_s)
        self._lost("timeout")
        raise WorkerTimeout(deadline_s, pid)

    def kill(self) -> None:
        """SIGKILL the worker's process group and reap it. Idempotent."""
        with self._life:
            self._kill_locked()

    def _reap_dead(self) -> int | None:
        """Reap a worker found dead, and its grandchildren; return its exit code."""
        with self._life:
            pid = self.pid
            if pid is not None:
                _wait_exit(pid, _DEATH_WAIT_S)
            return self._kill_locked()

    def _kill_locked(self) -> int | None:
        """SIGKILL the group, reap the worker, drop it; return its exit code."""
        self._ready = False
        process, conn = self._process, self._conn
        self._process = self._conn = None
        if conn is not None:
            conn.close()
        if process is None:
            return None
        pid = process.pid
        if pid is not None:
            # The worker leads its own group once it ran os.setsid. Before that
            # the group does not exist yet, so the process is also killed itself.
            # Not reaped yet, so neither id can belong to another process.
            for kill, ident in ((os.killpg, pid), (os.kill, pid)):
                try:
                    kill(ident, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        process.join(_JOIN_TIMEOUT_S)
        exitcode = process.exitcode
        if exitcode is None:
            logger.error("parse worker %s was not reaped within %s s", pid, _JOIN_TIMEOUT_S)
        else:
            process.close()
        return exitcode

    def _lost(self, cause: str) -> None:
        if self._on_lost is None:
            return
        try:
            self._on_lost(cause)
        except Exception:  # noqa: BLE001 — the caller's hook must not mask the outcome
            logger.exception("parse worker on_lost hook failed")
