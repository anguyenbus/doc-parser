"""
test_retry.py — Task Group 1 tests for ``src/parser_service/retry.py``.

Pure, fully offline (NO network, NO boto3 import). ``time.sleep`` is patched so
the suite runs in milliseconds. Covers transient/permanent classification,
bounded retry, Retry-After honoring, and botocore ``ClientError`` code mapping.

Run ONLY these tests (task 1.3):
    uv run pytest tests/test_retry.py
"""

from __future__ import annotations

import pytest

from parser_service.retry import is_transient, retry_call, with_retry


# ---------------------------------------------------------------------------
# Fake exceptions mirroring SDK shapes (no botocore dependency required)
# ---------------------------------------------------------------------------


class _StatusError(Exception):
    """Exception carrying a numeric ``status_code`` like some HTTP SDKs."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _HeaderError(Exception):
    """Exception exposing a ``response.headers`` mapping (Retry-After)."""

    def __init__(self, message: str, headers: dict[str, str]) -> None:
        super().__init__(message)

        class _Resp:
            pass

        self.response = _Resp()
        self.response.headers = headers


class _ClientError(Exception):
    """Minimal botocore-style ``ClientError``: ``response['Error']['Code']``."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.response = {"Error": {"Code": code, "Message": message}}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``time.sleep`` in retry so no real time passes; record calls."""
    calls: list[float] = []
    monkeypatch.setattr("parser_service.retry.time.sleep", lambda s: calls.append(s))
    # Expose the recorded sleeps on the module for assertions.
    monkeypatch.setattr("parser_service.retry._TEST_SLEEPS", calls, raising=False)


def _sleeps() -> list[float]:
    import parser_service.retry as r

    return getattr(r, "_TEST_SLEEPS", [])


# ---------------------------------------------------------------------------
# Core retry behavior
# ---------------------------------------------------------------------------


def test_success_on_first_try_calls_once() -> None:
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        return "ok"

    assert retry_call(fn, max_attempts=3) == "ok"
    assert calls["n"] == 1
    assert _sleeps() == []


def test_throttle_then_success_sleeps_between() -> None:
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _StatusError("throttled", status_code=429)
        return "ok"

    result = retry_call(fn, max_attempts=3, jitter=False)
    assert result == "ok"
    assert calls["n"] == 3
    # Two failures before success => two sleeps.
    assert len(_sleeps()) == 2


def test_exhausts_retries_reraises_last() -> None:
    def fn() -> str:
        raise _StatusError("still throttled", status_code=503)

    with pytest.raises(_StatusError):
        retry_call(fn, max_attempts=3, jitter=False)
    # max_attempts=3 => two sleeps (after attempt 0 and attempt 1).
    assert len(_sleeps()) == 2


def test_permanent_error_raises_immediately_no_retry() -> None:
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        raise _StatusError("403 forbidden", status_code=403)

    with pytest.raises(_StatusError):
        retry_call(fn, max_attempts=5, jitter=False)
    assert calls["n"] == 1
    assert _sleeps() == []


def test_retry_after_header_honored_over_backoff() -> None:
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _HeaderError("rate limit", headers={"Retry-After": "7"})
        return "ok"

    assert retry_call(fn, max_attempts=3, jitter=False, max_sleep=30.0) == "ok"
    assert _sleeps() == [7.0]


def test_with_retry_decorator_form() -> None:
    calls = {"n": 0}

    @with_retry(max_attempts=3, jitter=False)
    def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise _StatusError("timeout", status_code=504)
        return "ok"

    assert fn() == "ok"
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_is_transient_status_codes() -> None:
    assert is_transient(_StatusError("x", 429)) is True
    assert is_transient(_StatusError("x", 408)) is True
    assert is_transient(_StatusError("x", 425)) is True
    assert is_transient(_StatusError("x", 500)) is True
    assert is_transient(_StatusError("x", 503)) is True
    assert is_transient(_StatusError("x", 404)) is False
    assert is_transient(_StatusError("x", 400)) is False


def test_is_transient_botocore_codes() -> None:
    assert is_transient(_ClientError("ThrottlingException")) is True
    assert is_transient(_ClientError("TooManyRequestsException")) is True
    assert is_transient(_ClientError("ProvisionedThroughputExceededException")) is True
    assert is_transient(_ClientError("ServiceUnavailable")) is True
    assert is_transient(_ClientError("AccessDeniedException")) is False
    assert is_transient(_ClientError("UnrecognizedClientException")) is False
    assert is_transient(_ClientError("InvalidSignatureException")) is False
    assert is_transient(_ClientError("ValidationException")) is False


def test_permanent_wins_ties() -> None:
    # A message mentioning "rate limit" but classified permanent by code/status.
    assert is_transient(_StatusError("rate limit hit (unauthorized)", 401)) is False
    assert is_transient(_ClientError("AccessDeniedException", "rate limit")) is False
