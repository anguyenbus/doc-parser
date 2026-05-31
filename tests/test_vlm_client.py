"""
test_vlm_client.py

Unit tests for vlm_client.py — VLM client with mocked boto3.

boto3 is imported inside call_vlm() at call time to avoid mandatory AWS
dependencies at import. Tests patch it via sys.modules.
"""

from __future__ import annotations

import json
import sys
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock

import pytest

from parser_service.vlm_client import (
    _build_bedrock_request,
    _safe_parse,
    call_vlm,
    get_vlm_call_count,
    reset_vlm_call_count,
)

SAMPLE_IMAGE = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # minimal fake PNG bytes


def _make_boto3_mock(response_body: str) -> MagicMock:
    """Build a boto3 module mock that returns response_body from invoke_model.

    Uses side_effect to return a fresh BytesIO on every call,
    preventing exhausted-stream errors on multiple calls.
    """
    encoded = response_body.encode()

    def _invoke_model_side_effect(**kwargs: Any) -> dict[str, Any]:
        return {"body": BytesIO(encoded)}

    mock_client = MagicMock()
    mock_client.invoke_model.side_effect = _invoke_model_side_effect
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_client
    return mock_boto3


# ---------------------------------------------------------------------------
# Test 1: call_vlm with mocked boto3 returning valid JSON
# ---------------------------------------------------------------------------


def test_call_vlm_valid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """call_vlm returns dict with expected keys when boto3 returns valid JSON."""
    table_data = {
        "rows": 2,
        "cols": 3,
        "header_rows": 1,
        "cells": [{"row": 0, "col": 0, "text": "A", "row_span": 1, "col_span": 1}],
    }
    response_body = json.dumps({"content": [{"type": "text", "text": json.dumps(table_data)}]})

    monkeypatch.setenv("BEDROCK_VLM_MODEL", "test-model-id")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    mock_boto3 = _make_boto3_mock(response_body)
    monkeypatch.setitem(sys.modules, "boto3", mock_boto3)

    reset_vlm_call_count()
    result = call_vlm(SAMPLE_IMAGE, mode="table")

    assert "error" not in result, f"Unexpected error: {result.get('error')}"
    assert result.get("rows") == 2
    assert result.get("cols") == 3
    assert isinstance(result.get("cells"), list)


# ---------------------------------------------------------------------------
# Test 2: call_vlm with markdown-fenced JSON response
# ---------------------------------------------------------------------------


def test_call_vlm_markdown_fenced_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """_safe_parse strips markdown fences and returns correct dict."""
    table_data = {"rows": 1, "cols": 2, "header_rows": 1, "cells": []}
    fenced_text = f"```json\n{json.dumps(table_data)}\n```"
    response_body = json.dumps({"content": [{"type": "text", "text": fenced_text}]})

    monkeypatch.setenv("BEDROCK_VLM_MODEL", "test-model-id")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    mock_boto3 = _make_boto3_mock(response_body)
    monkeypatch.setitem(sys.modules, "boto3", mock_boto3)

    reset_vlm_call_count()
    result = call_vlm(SAMPLE_IMAGE, mode="table")

    assert "error" not in result, f"Unexpected error: {result.get('error')}"
    assert result.get("rows") == 1
    assert result.get("cols") == 2


# ---------------------------------------------------------------------------
# Test 3: call_vlm with boto3 raising an exception — must never raise
# ---------------------------------------------------------------------------


def test_call_vlm_never_raises_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """call_vlm returns {'error': ...} when boto3 raises, and never re-raises."""
    monkeypatch.setenv("BEDROCK_VLM_MODEL", "test-model-id")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    mock_boto3 = MagicMock()
    mock_boto3.client.return_value.invoke_model.side_effect = Exception("network error")
    monkeypatch.setitem(sys.modules, "boto3", mock_boto3)

    reset_vlm_call_count()
    # Must NOT raise — this is the contract
    result = call_vlm(SAMPLE_IMAGE, mode="page")

    assert "error" in result
    assert "network error" in result["error"]


# ---------------------------------------------------------------------------
# Test 4: _build_bedrock_request is a pure function
# ---------------------------------------------------------------------------


def test_build_bedrock_request_is_pure(monkeypatch: pytest.MonkeyPatch) -> None:
    """_build_bedrock_request returns a correctly structured dict, does not call boto3."""
    monkeypatch.setenv("BEDROCK_VLM_MODEL", "test-model-id")

    mock_boto3 = MagicMock()
    monkeypatch.setitem(sys.modules, "boto3", mock_boto3)

    result = _build_bedrock_request(SAMPLE_IMAGE, "test prompt")

    # boto3.client must not have been called
    mock_boto3.client.assert_not_called()

    assert "anthropic_version" in result
    assert result["max_tokens"] == 8192
    assert result["temperature"] == 0.0
    assert "messages" in result
    assert len(result["messages"]) == 1
    assert result["messages"][0]["role"] == "user"
    content = result["messages"][0]["content"]
    assert len(content) == 2
    assert content[0]["type"] == "image"
    assert content[1]["type"] == "text"
    assert content[1]["text"] == "test prompt"


# ---------------------------------------------------------------------------
# Additional: _safe_parse with bare ``` fence
# ---------------------------------------------------------------------------


def test_safe_parse_bare_fence() -> None:
    """_safe_parse handles bare ``` fence (not ```json)."""
    data = {"key": "value"}
    fenced = f"```\n{json.dumps(data)}\n```"
    result = _safe_parse(fenced)
    assert result == data


def test_safe_parse_json_fence() -> None:
    """_safe_parse handles ```json fence."""
    data = {"rows": 3, "cols": 2}
    fenced = f"```json\n{json.dumps(data)}\n```"
    result = _safe_parse(fenced)
    assert result == data


def test_safe_parse_invalid_json() -> None:
    """_safe_parse returns error dict on invalid JSON."""
    result = _safe_parse("not valid json {{{")
    assert "error" in result
    assert "invalid_json" in result["error"]
    assert "raw_preview" in result


# ---------------------------------------------------------------------------
# VLM call counter tests
# ---------------------------------------------------------------------------


def test_vlm_call_counter_increments(monkeypatch: pytest.MonkeyPatch) -> None:
    """_vlm_call_count increments on each successful call."""
    page_data: dict[str, Any] = {"elements": []}
    response_body = json.dumps({"content": [{"type": "text", "text": json.dumps(page_data)}]})

    monkeypatch.setenv("BEDROCK_VLM_MODEL", "test-model-id")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    # Use side_effect for fresh BytesIO on every invoke_model call
    mock_boto3 = _make_boto3_mock(response_body)
    monkeypatch.setitem(sys.modules, "boto3", mock_boto3)

    reset_vlm_call_count()
    assert get_vlm_call_count() == 0
    call_vlm(SAMPLE_IMAGE, mode="page")
    assert get_vlm_call_count() == 1
    call_vlm(SAMPLE_IMAGE, mode="page")
    assert get_vlm_call_count() == 2
    reset_vlm_call_count()
    assert get_vlm_call_count() == 0
