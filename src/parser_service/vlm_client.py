"""
vlm_client.py

All AWS Bedrock API communication for the parser service.

Public API:
  call_vlm(image_bytes, mode) -> dict   # never raises; returns {"error": ...} on failure
  preflight_vlm() -> None               # raises VlmConfigError if env/deps are missing
  VlmConfigError                        # distinguishes config errors from per-call errors
  _build_bedrock_request(image_bytes, prompt) -> dict   # pure function
  _safe_parse(raw) -> dict
  get_vlm_call_count() -> int
  reset_vlm_call_count() -> None
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from typing import Any, Literal

logger = logging.getLogger(__name__)

Mode = Literal["table", "page"]

# -------------------------------------------------------------------------
# Prompts (module-level constants)
# -------------------------------------------------------------------------

TABLE_PROMPT = """You are a document table extraction system.

Look at the image of a single table and output its structure as JSON:

{
  "rows": <int, total rows including headers>,
  "cols": <int, total cols>,
  "header_rows": <int, default 1>,
  "cells": [
    {"row": <0-indexed>, "col": <0-indexed>, "text": "<cell text>",
     "row_span": <int, default 1>, "col_span": <int, default 1>}
  ]
}

Rules:
- Include every cell, including empty ones (text = "")
- For merged cells, emit ONE entry at the top-left with the correct spans;
  do NOT emit the cells the span covers
- Output ONLY the JSON object, no markdown fences, no commentary
- If the image is not a table or unreadable, output {"error": "<reason>"}
"""

PAGE_PROMPT = """You are a document page extraction system.

Look at the image of a single document page and output its content as an
ordered JSON object with elements in natural reading order:

{
  "elements": [
    {
      "type": "<heading|paragraph|list|table|figure|caption|footnote|header|footer|page_number|equation|code_block>",
      "text": "<plain text of the element>",
      "level": <int, only for headings (1-6); omit otherwise>,
      "table": <table object as defined below; only for type=table; omit otherwise>
    }
  ]
}

Table object (when type=table):
{
  "rows": <int>, "cols": <int>, "header_rows": <int>,
  "cells": [{"row": <int>, "col": <int>, "text": "<text>",
             "row_span": <int>, "col_span": <int>}]
}

Rules:
- Use natural reading order (top-to-bottom, left-to-right for single-column;
  follow columns for multi-column layouts)
- Use the element type that best fits each block of content
- Transcribe ALL visible text verbatim, including every number, axis label,
  data value, and legend entry inside charts, figures, and graphs. Do not
  summarize, paraphrase, or omit any visible text.
- Capture EVERY list item as its own entry; never collapse or drop items.
- For charts/figures, emit a `figure` element whose `text` contains the caption
  AND every data label/value shown in the graphic.
- Preserve each heading separately with its `level`; do not merge headings.
- Completeness over cleanliness: if text is visible, it must appear in output.
- Do not invent content; but include ALL content that IS visible in the image
- Output ONLY the JSON object, no markdown fences, no commentary
- If the image is unreadable, output {"error": "<reason>"}
"""

# -------------------------------------------------------------------------
# VLM call counter (per-worker-thread)
# -------------------------------------------------------------------------
# Backed by threading.local() so reset / increment / read are isolated per
# worker thread. Each file in a batch parses start-to-finish on exactly one
# ThreadPoolExecutor worker (parse_to_markdown spawns no threads/asyncio
# itself), so a concurrent neighbor's reset_vlm_call_count() at the start of
# ITS parse can never corrupt the count this thread is about to read. The
# public get_/reset_ API shape is unchanged; single-threaded callers on the
# main thread are unaffected.
_vlm_counter = threading.local()


def get_vlm_call_count() -> int:
    """Return the number of successful VLM calls on THIS thread since last reset."""
    return getattr(_vlm_counter, "count", 0)


def reset_vlm_call_count() -> None:
    """Reset THIS thread's VLM call counter to zero (called at the start of each parse())."""
    _vlm_counter.count = 0


def _increment_vlm_call_count() -> None:
    """Increment THIS thread's VLM call counter by one (on a successful call)."""
    _vlm_counter.count = getattr(_vlm_counter, "count", 0) + 1


# -------------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------------


def call_vlm(image_bytes: bytes, mode: Mode) -> dict[str, Any]:
    """Send an image to AWS Bedrock Claude Sonnet and return parsed JSON.

    Args:
        image_bytes: Raw PNG or JPEG bytes of the image to analyze.
        mode: "table" uses TABLE_PROMPT; "page" uses PAGE_PROMPT.

    Returns:
        Parsed JSON dict on success. {"error": "<reason>"} on any failure.
        Never raises.
    """
    try:
        import boto3  # AWS SDK — only imported at call time

        prompt = TABLE_PROMPT if mode == "table" else PAGE_PROMPT
        body = _build_bedrock_request(image_bytes, prompt)

        model_id = os.environ["BEDROCK_VLM_MODEL"]
        region = os.environ.get("AWS_REGION", "us-east-1")

        client = boto3.client("bedrock-runtime", region_name=region)

        # NOTE: invoke_model does not accept per-call tags via the SDK.
        # Cost attribution uses IAM role/resource tags at the AWS account level
        # with tag key 'parser-service'. Do not attempt to pass a 'tags'
        # parameter to invoke_model — it will be rejected by the API.
        resp = client.invoke_model(
            modelId=model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )

        payload = json.loads(resp["body"].read())
        raw_text = "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        )

        result = _safe_parse(raw_text)
        _increment_vlm_call_count()
        return result

    except Exception as exc:
        logger.warning("VLM call failed (mode=%s): %s", mode, exc)
        return {"error": str(exc)}


def _detect_media_type(image_bytes: bytes) -> str:
    """Detect the Claude-supported media type from an image's magic bytes.

    Claude accepts image/png, image/jpeg, image/gif, image/webp. We sniff the
    actual bytes rather than trusting the file extension, because the direct
    image-input path forwards bytes unchanged and a wrong media_type can be
    rejected by the API. Falls back to image/png.
    """
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _build_bedrock_request(image_bytes: bytes, prompt: str) -> dict[str, Any]:
    """Build the body dict for bedrock-runtime invoke_model.

    Pure function — reads BEDROCK_VLM_MODEL env var but does NOT call boto3.
    Suitable for reuse in Batch Inference job submission.

    Args:
        image_bytes: Raw image bytes (PNG, JPEG, GIF, or WebP).
        prompt: The text prompt to send alongside the image.

    Returns:
        Dict with keys: anthropic_version, max_tokens, temperature, messages.
    """
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 8192,
        "temperature": 0.0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": _detect_media_type(image_bytes),
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }


def _safe_parse(raw: str) -> dict[str, Any]:
    """Parse VLM text output, stripping markdown fences if present.

    Args:
        raw: Raw string from the VLM response.

    Returns:
        Parsed dict on success.
        {"error": "invalid_json: ...", "raw_preview": raw[:500]} on JSONDecodeError.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        # Drop the opening fence line (```json or ```)
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        # Drop the closing fence line if present
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines)
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except json.JSONDecodeError as exc:
        return {"error": f"invalid_json: {exc}", "raw_preview": raw[:500]}
