"""
result.py — the result of parsing one unit.

A handler returns a ``Reply``: ``(state, reason, reason_code, markdown, extra record
fields)``. The page pipeline, the parse worker and every handler share this contract,
so it lives here rather than in any one of them.
"""

from __future__ import annotations

from typing import Any

# Page states. ``pending`` appears only in status.json (a page with no record yet).
STATE_OK = "ok"
STATE_DEGRADED = "degraded"
STATE_FAILED = "failed"
STATE_PENDING = "pending"

# A reply: (state, reason, reason_code, markdown, extra record fields).
Reply = tuple[str, str | None, str | None, str, dict[str, Any]]

# Exception text kept in a reply's reason and in log lines (spec D9).
_MAX_EXCEPTION_CHARS = 500


def exception_reply(exc: BaseException) -> Reply:
    """A ``failed`` reply for an exception raised while parsing (text cut at 500)."""
    text = f"{type(exc).__name__}: {exc}"[:_MAX_EXCEPTION_CHARS]
    return "failed", f"exception: {text}", "exception", "", {}


def apply_output_cap(kind: str, reply: Reply, cap_bytes: int | None) -> Reply:
    """Replace a single-unit reply whose markdown is over ``cap_bytes`` (spec D5).

    PDF pages are never capped (a page's output is bounded by the page). The
    stored ``limits`` are in MB (1,000,000 bytes), as the setting is.
    """
    if kind == "pdf" or cap_bytes is None:
        return reply
    size = len(reply[3].encode("utf-8"))
    if size <= cap_bytes:
        return reply
    max_output_mb = cap_bytes / 1_000_000
    limits = {"max_output_mb": int(max_output_mb) if max_output_mb.is_integer() else max_output_mb}
    return (
        "failed",
        f"output_too_large: {size} bytes of markdown, over the cap of {cap_bytes}",
        "output_too_large",
        "",
        {"output_bytes": size, "limits": limits},
    )
