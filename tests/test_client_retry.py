"""
test_client_retry.py — Task Group 2 tests: retry wrapping of the inner AWS calls
in ``vlm_client.call_vlm`` (``invoke_model``) and ``textract_client.analyze_page``
(``analyze_document``).

Fully offline: boto3 is mocked via ``sys.modules`` (mirrors ``test_vlm_client.py``
/ ``test_textract_client.py``). ``time.sleep`` in the retry helper is patched so
no real time passes.

Asserts:
  - a throttle-then-success is retried and returns the parsed success payload;
  - the success counter increments only once (on eventual success);
  - an exhausted throttle returns ``{"error": ..., "error_kind": "throttled"}``;
  - a permanent error returns ``{"error": ...}`` with NO ``error_kind`` and is not
    retried.

Run ONLY these tests (task 2.4):
    uv run pytest tests/test_client_retry.py
"""

from __future__ import annotations

import json
import sys
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock

import pytest

from parser_service.textract_client import (
    analyze_page,
    get_textract_call_count,
    reset_textract_call_count,
)
from parser_service.vlm_client import (
    call_vlm,
    get_vlm_call_count,
    reset_vlm_call_count,
)

SAMPLE_IMAGE = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


class _ClientError(Exception):
    """botocore-style ClientError: ``response['Error']['Code']``."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.response = {"Error": {"Code": code, "Message": message}}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("parser_service.retry.time.sleep", lambda s: None)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEDROCK_VLM_MODEL", "test-model-id")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# VLM: invoke_model wrapped in retry
# ---------------------------------------------------------------------------


def _vlm_success_body() -> str:
    elements = {"elements": [{"type": "paragraph", "text": "hello"}]}
    return json.dumps({"content": [{"type": "text", "text": json.dumps(elements)}]})


def _install_boto3(monkeypatch: pytest.MonkeyPatch, mock_client: MagicMock) -> None:
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_client
    monkeypatch.setitem(sys.modules, "boto3", mock_boto3)


def test_call_vlm_throttle_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _vlm_success_body()
    calls = {"n": 0}

    def _side_effect(**kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _ClientError("ThrottlingException", "slow down")
        return {"body": BytesIO(body.encode())}

    client = MagicMock()
    client.invoke_model.side_effect = _side_effect
    _install_boto3(monkeypatch, client)

    reset_vlm_call_count()
    result = call_vlm(SAMPLE_IMAGE, mode="page")

    assert "error" not in result
    assert result.get("elements") == [{"type": "paragraph", "text": "hello"}]
    assert calls["n"] == 2  # one retry observed
    assert get_vlm_call_count() == 1  # counter increments only on success


def test_call_vlm_throttle_exhausted_marks_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.invoke_model.side_effect = _ClientError("ThrottlingException", "slow down")
    _install_boto3(monkeypatch, client)

    reset_vlm_call_count()
    result = call_vlm(SAMPLE_IMAGE, mode="page")

    assert "error" in result
    assert result.get("error_kind") == "throttled"
    assert get_vlm_call_count() == 0


def test_call_vlm_permanent_error_no_error_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.invoke_model.side_effect = _ClientError("AccessDeniedException", "nope")
    _install_boto3(monkeypatch, client)

    reset_vlm_call_count()
    result = call_vlm(SAMPLE_IMAGE, mode="page")

    assert "error" in result
    assert "error_kind" not in result
    # Permanent => not retried (single invoke_model call).
    assert client.invoke_model.call_count == 1


# ---------------------------------------------------------------------------
# Textract: analyze_document wrapped in retry
# ---------------------------------------------------------------------------


def _textract_success_resp() -> dict[str, Any]:
    return {"Blocks": []}


def test_analyze_page_throttle_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def _side_effect(**kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _ClientError("ProvisionedThroughputExceededException")
        return _textract_success_resp()

    client = MagicMock()
    client.analyze_document.side_effect = _side_effect
    _install_boto3(monkeypatch, client)

    reset_textract_call_count()
    result = analyze_page(SAMPLE_IMAGE)

    assert "error" not in result
    assert result.get("elements") == []
    assert calls["n"] == 2
    assert get_textract_call_count() == 1


def test_analyze_page_throttle_exhausted_marks_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.analyze_document.side_effect = _ClientError("ThrottlingException")
    _install_boto3(monkeypatch, client)

    reset_textract_call_count()
    result = analyze_page(SAMPLE_IMAGE)

    assert "error" in result
    assert result.get("error_kind") == "throttled"
    assert get_textract_call_count() == 0


def test_analyze_page_permanent_error_no_error_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.analyze_document.side_effect = _ClientError("ValidationException", "bad doc")
    _install_boto3(monkeypatch, client)

    reset_textract_call_count()
    result = analyze_page(SAMPLE_IMAGE)

    assert "error" in result
    assert "error_kind" not in result
    assert client.analyze_document.call_count == 1
