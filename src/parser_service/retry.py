"""Retry helper — bounded exponential backoff with transient-error detection.

Modeled on ``references/pdfmux/src/pdfmux/retry.py``. AWS Bedrock / Textract
calls fail transiently under load: throttling, provisioned-throughput limits,
5xx, connection blips. This helper wraps a callable with a small, honest retry
loop that retries transient failures and fails fast on permanent ones.

Design goals:
    - Retry transient errors (rate limit / throttling / timeout / 5xx /
      429/408/425).
    - Never retry permanent errors (auth / forbidden / validation / 404 / 4xx).
      Permanent classification wins ties.
    - Classify botocore ``ClientError`` by its ``Error.Code``.
    - Honor a ``Retry-After`` header when present.
    - Cap total wait time so a stuck endpoint can't hang the pipeline.
    - Log every retry at WARNING with the attempt number.

Dependency-free (stdlib only). Never imports boto3/botocore.

Usage::

    @with_retry(max_attempts=3, backoff_base=2.0)
    def invoke(...):
        ...

    result = retry_call(fn, *args, max_attempts=3, backoff_base=2.0)
"""

from __future__ import annotations

import functools
import logging
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger("parser_service.retry")

F = TypeVar("F", bound=Callable[..., Any])


# Substrings that strongly indicate a transient failure. Conservative — we'd
# rather under-retry than retry a permanent error repeatedly.
_TRANSIENT_HINTS: tuple[str, ...] = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "rate_limit",
    "too many requests",
    "throttl",  # throttling / throttled / ThrottlingException
    "timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "connection error",
    "temporarily unavailable",
    "service unavailable",
    "serviceunavailable",
    "provisionedthroughput",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "502",
    "503",
    "504",
    "overloaded",
)

# Substrings that indicate a permanent failure — never retry.
_PERMANENT_HINTS: tuple[str, ...] = (
    "invalid api key",
    "api key not valid",
    "unauthorized",
    "forbidden",
    "permission denied",
    "accessdenied",
    "access denied",
    "authentication",
    "unrecognizedclient",
    "invalidsignature",
    "validationexception",
    "validation error",
    "not found",
    "404",
    "400 bad request",
    "invalid request",
)

# botocore ``ClientError`` ``Error.Code`` values — the authoritative signal when
# present (checked before the string/status heuristics).
_TRANSIENT_AWS_CODES: frozenset[str] = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "TooManyRequestsException",
        "ProvisionedThroughputExceededException",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "RequestTimeout",
        "RequestTimeoutException",
        "InternalServerError",
        "InternalServerException",
    }
)
_PERMANENT_AWS_CODES: frozenset[str] = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "UnrecognizedClient",
        "UnrecognizedClientException",
        "InvalidSignature",
        "InvalidSignatureException",
        "ValidationException",
    }
)


def _aws_error_code(exc: BaseException) -> str | None:
    """Extract a botocore ``ClientError`` ``Error.Code``, if present."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if isinstance(code, str) and code:
                return code
    return None


def _err_text(exc: BaseException) -> str:
    """Best-effort lowercase string for substring matching."""
    parts: list[str] = [type(exc).__name__, str(exc)]
    for attr in ("status_code", "code", "http_status"):
        v = getattr(exc, attr, None)
        if v is not None and not callable(v):
            parts.append(str(v))
    return " ".join(parts).lower()


def is_transient(exc: BaseException) -> bool:
    """Return True if ``exc`` looks like a transient/retryable failure.

    Precedence: an AWS ``Error.Code`` is authoritative when present; otherwise a
    permanent hint (or a 4xx status) wins ties, then a transient hint or status.
    """
    # 1. Authoritative AWS Error.Code (permanent checked first — wins ties).
    code = _aws_error_code(exc)
    if code is not None:
        if code in _PERMANENT_AWS_CODES:
            return False
        if code in _TRANSIENT_AWS_CODES:
            return True
        # Unknown code falls through to the generic heuristics below.

    text = _err_text(exc)

    # 2. Permanent hints win ties (e.g. a 401 that mentions "rate limit").
    for hint in _PERMANENT_HINTS:
        if hint in text:
            return False

    # 3. Numeric status code on the exception object (some SDKs attach it).
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "http_status", None)
    if isinstance(status, int):
        if status in (408, 425, 429) or 500 <= status < 600:
            return True
        if 400 <= status < 500:
            return False

    # 4. Transient hints.
    for hint in _TRANSIENT_HINTS:
        if hint in text:
            return True

    return False


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Extract a ``Retry-After`` delay (seconds) from the exception, or None."""
    for attr_chain in (("response", "headers"), ("headers",)):
        obj: Any = exc
        try:
            for a in attr_chain:
                obj = getattr(obj, a, None)
                if obj is None:
                    break
            if obj is None or not hasattr(obj, "get"):
                continue
            value = obj.get("Retry-After") or obj.get("retry-after")
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        except Exception:  # noqa: BLE001 — best-effort extraction
            continue
    return None


def _sleep_for(attempt: int, backoff_base: float, max_sleep: float, jitter: bool) -> float:
    """Compute and execute the backoff sleep. Returns seconds slept."""
    delay = min(max_sleep, backoff_base**attempt)
    if jitter:
        delay = delay * (0.5 + random.random() / 2.0)  # 50–100% of computed delay
    time.sleep(delay)
    return delay


def with_retry(
    max_attempts: int = 3,
    backoff_base: float = 2.0,
    *,
    max_sleep: float = 30.0,
    jitter: bool = True,
    transient: Callable[[BaseException], bool] | None = None,
) -> Callable[[F], F]:
    """Decorator: wrap a function with bounded exponential-backoff retry.

    Args:
        max_attempts: Total tries including the first call. 1 disables retry.
        backoff_base: Base for exponential delay (``base ** attempt``).
        max_sleep: Cap on each individual sleep between attempts (seconds).
        jitter: If True, randomize each delay to 50–100% of computed.
        transient: Override the default transient-detection predicate.

    Returns:
        A decorator that retries the wrapped function on transient errors.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    is_transient_fn = transient or is_transient

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: BaseException | None = None
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except BaseException as exc:
                    last_exc = exc
                    if not is_transient_fn(exc):
                        raise
                    if attempt >= max_attempts - 1:
                        raise

                    retry_after = _retry_after_seconds(exc)
                    if retry_after is not None:
                        delay = min(max_sleep, retry_after)
                        time.sleep(delay)
                    else:
                        delay = _sleep_for(attempt + 1, backoff_base, max_sleep, jitter)

                    logger.warning(
                        "retry %d/%d for %s after %.2fs: %s",
                        attempt + 1,
                        max_attempts - 1,
                        getattr(fn, "__name__", "fn"),
                        delay,
                        exc,
                    )
            # Unreachable — the final attempt either returns or raises above.
            assert last_exc is not None
            raise last_exc

        return wrapper  # type: ignore[return-value]

    return decorator


def retry_call(
    fn: Callable[..., Any],
    *args: Any,
    max_attempts: int = 3,
    backoff_base: float = 2.0,
    max_sleep: float = 30.0,
    jitter: bool = True,
    transient: Callable[[BaseException], bool] | None = None,
    **kwargs: Any,
) -> Any:
    """Imperative form of ``with_retry`` — run a callable with retries."""
    wrapped = with_retry(
        max_attempts=max_attempts,
        backoff_base=backoff_base,
        max_sleep=max_sleep,
        jitter=jitter,
        transient=transient,
    )(fn)
    return wrapped(*args, **kwargs)
