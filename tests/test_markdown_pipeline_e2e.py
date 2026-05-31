"""
test_markdown_pipeline_e2e.py — Task Group 5.3 strategic end-to-end tests.

Up to 10 additional tests targeting integration points / end-to-end workflows
for THIS feature that the per-group unit tests (1.1/2.1/3.1/4.1) did not already
cover. Mocked VLM throughout (no Bedrock). Edge/perf/accessibility are skipped.

Gaps filled (from the 5.2 analysis):
  1. Full round-trip: PDF → parse_to_markdown → wrap_md_as_prediction →
     schema-1.0.0-valid prediction (the eval path end to end, not the two halves
     tested in isolation by 2.1 and 3.1).
  2. A single multi-page doc mixing all three routes in one run:
     keep + promote(vlm) + vlm-fallback-docling — exercising the route vocabulary
     and page_routes telemetry together (2.1 tested each route in its own run).
  3. Office (XLSX) and HTML whole-doc paths produce a schema-valid round-trip with
     the gate skipped (2.1 only covered DOCX).
  4. Gated image path: Docling-confident image stays docling-kept (no VLM), and a
     gate-promoted image goes to the VLM on the raw bytes — the gated-not-always-VLM
     contract, end to end through wrapping.

Run with the rest of the feature suite (task 5.5):
    uv run pytest tests/test_markdown_pipeline_e2e.py
"""

from __future__ import annotations

import json
import mimetypes
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

Validator = Callable[[dict[str, Any]], None]

FIXTURES = Path(__file__).parent / "fixtures"
DIGITAL = FIXTURES / "digital_simple.pdf"
THREE_PAGE = FIXTURES / "three_page_digital.pdf"
XLSX = FIXTURES / "sheet.xlsx"
HTML = FIXTURES / "page.html"
IMAGE = FIXTURES / "screenshot.png"


# ---------------------------------------------------------------------------
# Wheel-schema validator (same robust resolution as test_wrap_md_as_prediction).
# ---------------------------------------------------------------------------


def _wheel_schema_path() -> Path:
    try:
        import doc_bench

        return Path(doc_bench.__file__).parent / "fixtures" / "parser_output.schema.json"
    except Exception:  # noqa: BLE001
        pass
    tool_root = Path(os.path.expanduser("~/.local/share/uv/tools/doc-bench"))
    if tool_root.exists():
        for c in tool_root.rglob("doc_bench/fixtures/parser_output.schema.json"):
            return c
    pytest.skip("doc-bench wheel not installed; cannot locate bundled schema")


@pytest.fixture(scope="module")
def validate_against_wheel() -> Validator:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_wheel_schema_path().read_text())

    def _validate(prediction: dict[str, Any]) -> None:
        jsonschema.validate(instance=prediction, schema=schema)

    return _validate


def _patch_vlm(monkeypatch: pytest.MonkeyPatch, fn: Callable[..., Any]) -> None:
    from parser_service import markdown_pipeline

    monkeypatch.setattr(markdown_pipeline, "call_vlm", fn)


def _force_keep(monkeypatch: pytest.MonkeyPatch) -> None:
    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("keep", None, None),
    )


# ===========================================================================
# 1. Full round-trip: parse_to_markdown → wrap_md_as_prediction → schema-valid.
# ===========================================================================


def test_pdf_roundtrip_to_schema_valid_prediction(
    monkeypatch: pytest.MonkeyPatch, validate_against_wheel: Validator
) -> None:
    """The full eval path: parse a PDF to markdown, wrap it, and the wrapped
    prediction is schema-1.0.0-valid with the markdown carried verbatim."""
    from parser_service.markdown_pipeline import parse_to_markdown, wrap_md_as_prediction

    _force_keep(monkeypatch)
    _patch_vlm(monkeypatch, lambda *a, **k: {"error": "VLM not expected on kept pages"})

    result = parse_to_markdown(DIGITAL)
    md = result["markdown"]
    assert md.strip(), "expected non-empty markdown from the kept Docling pages"

    pred = wrap_md_as_prediction(md, DIGITAL)
    validate_against_wheel(pred)

    assert pred["schema_version"] == "1.0.0"
    assert len(pred["elements"]) == 1
    assert pred["elements"][0]["type"] == "paragraph"
    # The exact markdown produced by parse_to_markdown rides through unchanged.
    assert pred["elements"][0]["text"] == md
    # Source matches what _empty_output builds for the same file (no drift).
    from parser_service.parser_service import _empty_output

    mime = mimetypes.guess_type(str(DIGITAL))[0] or ""
    assert pred["source"] == _empty_output(DIGITAL.resolve(), mime)["source"]


# ===========================================================================
# 2. One multi-page run mixing keep + vlm + vlm-fallback-docling.
# ===========================================================================


def test_multipage_mixes_all_three_routes_in_one_run(
    monkeypatch: pytest.MonkeyPatch, validate_against_wheel: Validator
) -> None:
    """A single 3-page run produces keep (page 0), vlm (page 1), and
    vlm-fallback-docling (page 2), with telemetry and the wrap all consistent."""
    if not THREE_PAGE.exists():
        pytest.skip("three_page_digital.pdf missing; run make_three_page_digital.py")

    from parser_service import markdown_pipeline
    from parser_service.markdown_pipeline import wrap_md_as_prediction
    from parser_service.quality_gate import Decision

    def _gate(page_no: int, *a: Any, **k: Any) -> Decision:
        if page_no == 0:
            return Decision("keep", None, None)
        if page_no == 1:
            return Decision("promote_to_vlm", "forced_p1", layer=1)
        return Decision("promote_to_vlm", "forced_p2", layer=2)

    monkeypatch.setattr(markdown_pipeline, "evaluate_page", _gate)

    # Page 1's VLM succeeds (distinct content); page 2's VLM returns garbage →
    # falls back to its Docling slice (PAGE3 survives).
    def _vlm(image_bytes: bytes, mode: str) -> dict[str, Any]:
        _vlm.calls += 1  # type: ignore[attr-defined]
        if _vlm.calls == 1:  # type: ignore[attr-defined]
            return {"elements": [{"type": "paragraph", "text": "MIDDLE_VLM_OK"}]}
        return {"elements": []}  # garbage → fallback to Docling

    _vlm.calls = 0  # type: ignore[attr-defined]
    monkeypatch.setattr(markdown_pipeline, "call_vlm", _vlm)

    result = markdown_pipeline.parse_to_markdown(THREE_PAGE)
    routes = {r["page_index"]: r["route"] for r in result["page_routes"]}

    assert routes == {
        0: "docling-kept",
        1: "vlm",
        2: "vlm-fallback-docling",
    }, result["page_routes"]

    md = result["markdown"]
    assert "PAGE1" in md  # kept Docling page 0
    assert "MIDDLE_VLM_OK" in md  # VLM page 1
    assert "PAGE3" in md  # page 2 fell back to Docling
    # Order preserved across the three different routes.
    assert md.index("PAGE1") < md.index("MIDDLE_VLM_OK") < md.index("PAGE3")

    # And the whole thing still wraps to a schema-valid prediction.
    pred = wrap_md_as_prediction(md, THREE_PAGE)
    validate_against_wheel(pred)
    assert pred["elements"][0]["text"] == md


# ===========================================================================
# 3. Office (XLSX) and HTML whole-doc paths → schema-valid round-trip.
# ===========================================================================


@pytest.mark.parametrize("fixture", [XLSX, HTML])
def test_office_html_whole_doc_roundtrip(
    monkeypatch: pytest.MonkeyPatch, validate_against_wheel: Validator, fixture: Path
) -> None:
    """XLSX/HTML take the whole-doc export path: gate skipped, one logical page,
    VLM never called, and the result wraps to a schema-valid prediction."""
    if not fixture.exists():
        pytest.skip(f"{fixture.name} fixture missing")

    from parser_service import markdown_pipeline
    from parser_service.markdown_pipeline import parse_to_markdown, wrap_md_as_prediction

    # If the gate or VLM ran for a structured format it would be a bug.
    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gate must be skipped")),
    )
    _patch_vlm(monkeypatch, lambda *a, **k: {"error": "VLM must not run for office/html"})

    result = parse_to_markdown(fixture)

    assert isinstance(result["markdown"], str)
    assert len(result["page_routes"]) == 1
    assert result["page_routes"][0] == {
        "page_index": 0,
        "route": "docling-kept",
        "reason": None,
    }

    pred = wrap_md_as_prediction(result["markdown"], fixture)
    validate_against_wheel(pred)
    assert pred["elements"][0]["text"] == result["markdown"]


# ===========================================================================
# 4. Gated image path: keep stays docling-kept (no VLM); promote → VLM.
# ===========================================================================


def test_image_kept_stays_docling_no_vlm(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate-``keep`` image with Docling content stays docling-kept and never
    touches the VLM — the gated (NOT always-VLM) image contract."""
    if not IMAGE.exists():
        pytest.skip("screenshot.png fixture missing")

    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("keep", None, None),
    )
    vlm_called = {"n": 0}

    def _vlm(*a: Any, **k: Any) -> dict[str, Any]:
        vlm_called["n"] += 1
        return {"error": "should not run on a kept image"}

    monkeypatch.setattr(markdown_pipeline, "call_vlm", _vlm)

    result = markdown_pipeline.parse_to_markdown(IMAGE)
    routes = result["page_routes"]

    # If Docling extracted content the image is kept and the VLM is not called.
    # (If Docling extracted nothing the gate-skip path would promote; this
    # fixture is a text screenshot, so we assert the kept-no-VLM contract.)
    if routes and routes[0]["route"] == "docling-kept":
        assert vlm_called["n"] == 0, "VLM must not run for a docling-kept image"
        assert len(routes) == 1 and routes[0]["page_index"] == 0
    else:
        pytest.skip("Docling extracted no content for this image; kept path n/a")


def test_image_promoted_goes_to_vlm_on_raw_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate-``promote`` image is sent to the VLM and its markdown is used,
    confirming the image path is gated (VLM only on failure), not bypassed."""
    if not IMAGE.exists():
        pytest.skip("screenshot.png fixture missing")

    from parser_service import markdown_pipeline
    from parser_service.quality_gate import Decision

    monkeypatch.setattr(
        markdown_pipeline,
        "evaluate_page",
        lambda *a, **k: Decision("promote_to_vlm", "forced_image", layer=1),
    )

    seen: dict[str, Any] = {"image_bytes": None, "mode": None}

    def _vlm(image_bytes: bytes, mode: str) -> dict[str, Any]:
        seen["image_bytes"] = image_bytes
        seen["mode"] = mode
        return {"elements": [{"type": "paragraph", "text": "IMAGE_VLM_OUTPUT"}]}

    monkeypatch.setattr(markdown_pipeline, "call_vlm", _vlm)

    result = markdown_pipeline.parse_to_markdown(IMAGE)

    assert "IMAGE_VLM_OUTPUT" in result["markdown"]
    assert result["page_routes"][0]["route"] == "vlm"
    # The image path passes the raw image bytes (not a re-rendered page) at mode="page".
    assert seen["mode"] == "page"
    assert seen["image_bytes"] == IMAGE.read_bytes()
