"""End-to-end confirmation of escalation output arbitration (Engine Hardening §1.5).

Drives the REAL ``_vlm_page_markdown`` seam and the REAL ``_measure_text_quality``
decision. Only the network engine call (``call_vlm`` / ``analyze_page``) is mocked, and
``image_bytes`` is supplied so neither AWS nor ``render_page`` is touched — so this runs
offline, in any region, with no credentials.

Run from the repo root:

    PYTHONPATH=src uv run python scripts/demo_arbitration.py
"""
import os
import sys
from pathlib import Path

# Make the script self-bootstrapping: put the repo's ``src/`` on sys.path so it
# imports cleanly under the IDE debugger / bare ``python`` too (not just when
# ``PYTHONPATH=src`` is set). scripts/ -> repo root -> src.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import parser_service.markdown_pipeline as mp  # noqa: E402
from parser_service.quality_gate import _measure_text_quality  # noqa: E402

# A genuinely CLEAN English paragraph (should PASS the quality proxy) and a
# genuinely GARBLED page of short alnum tokens (should FAIL it).
CLEAN = (
    "The quarterly financial report summarizes revenue growth across all regional "
    "business units during the fiscal year, including operating margins and cash flow."
)
# A DISTINCT clean paragraph for the "engine clean" case, so the harness can tell
# engine-vs-docling apart by string identity (both pass the proxy).
CLEAN_ENGINE = (
    "Net income rose twelve percent on stronger enterprise demand and disciplined "
    "cost control, with free cash flow reaching a record for the period."
)
GARBLED = " ".join(
    ["x3q9", "z7w2", "k4j1", "b8n5", "q2x6", "z9w3", "k1j7", "b3n8", "x5q2", "z1w8"] * 6
)

CLEAN_ELEMS = {"elements": [{"type": "paragraph", "text": CLEAN_ENGINE}]}
GARBLED_ELEMS = {"elements": [{"type": "paragraph", "text": GARBLED}]}


def _fake_engine(result):
    """Return a stand-in for call_vlm/analyze_page that yields fixed elements."""
    def _call(*args, **kwargs):
        return result
    return _call


def run(label, *, engine, arbitration, engine_result, docling_fallback, reason):
    # Configure the seam via env (read at call time).
    os.environ["PARSER_ESCALATION_ENGINE"] = engine
    if arbitration is None:
        os.environ.pop("PARSER_ESCALATION_ARBITRATION", None)
    else:
        os.environ["PARSER_ESCALATION_ARBITRATION"] = arbitration

    # Mock ONLY the network engine call.
    mp.call_vlm = _fake_engine(engine_result)
    mp.analyze_page = _fake_engine(engine_result)

    container = {"warnings": []}
    routes = []
    out = mp._vlm_page_markdown(
        Path("/dev/null"),
        page_idx=0,
        container=container,
        page_routes=routes,
        docling_fallback=docling_fallback,
        reason=reason,
        layer=None,
        image_bytes=b"not-used-because-mocked",
    )
    rec = routes[0]
    kept = "DOCLING" if out == docling_fallback else ("ENGINE" if out else "EMPTY")
    print(f"\n### {label}")
    print(f"  engine={engine}  flag={os.environ.get('PARSER_ESCALATION_ARBITRATION', '<unset>')}  promotion_reason={reason!r}")
    print(f"  -> route          : {rec['route']}")
    print(f"  -> arbitration    : {rec.get('arbitration', '(key absent)')}")
    print(f"  -> kept output    : {kept}")
    print(f"  -> record keys    : {sorted(rec.keys())}")
    return rec, kept


def main():
    print("=" * 78)
    print("STEP 0 — confirm the quality proxy classifies the test strings as intended")
    print("=" * 78)
    cs = _measure_text_quality(CLEAN)
    gs = _measure_text_quality(GARBLED)
    print(f"  CLEAN   passes={cs.passes}  failing={cs.failing_signals}")
    print(f"  GARBLED passes={gs.passes}  failing={gs.failing_signals}")
    assert cs.passes is True and gs.passes is False, "test strings not classified as intended"
    print("  OK: CLEAN passes, GARBLED fails.")

    print("\n" + "=" * 78)
    print("STEP 1 — arbitration decisions through the real seam")
    print("=" * 78)

    # A: Layer-1 promotion, engine garbled, docling clean, flag ON -> keep DOCLING
    recA, keptA = run(
        "A. VLM garbled + clean Docling + Layer-1 reason + flag ON  => revert to Docling",
        engine="vlm", arbitration="1",
        engine_result=GARBLED_ELEMS, docling_fallback=CLEAN,
        reason="docling_low_grade=poor",
    )
    assert recA["route"] == "vlm-rejected-kept-docling" and keptA == "DOCLING"
    assert recA["arbitration"] == "kept-docling"

    # B: COVERAGE promotion, engine garbled, docling clean, flag ON -> keep ENGINE (!)
    recB, keptB = run(
        "B. VLM garbled + clean Docling + COVERAGE reason + flag ON => keep engine (never revert coverage)",
        engine="vlm", arbitration="true",
        engine_result=GARBLED_ELEMS, docling_fallback=CLEAN,
        reason="low_coverage: extracted 5 of 100 text-layer tokens",
    )
    assert recB["route"] == "vlm" and keptB == "ENGINE"
    assert recB["arbitration"] == "kept-engine"

    # C: same as A but flag OFF -> byte-identical legacy (keep garbled engine, no arbitration key)
    recC, keptC = run(
        "C. Same as A but flag OFF                                  => legacy: keep engine, no arbitration key",
        engine="vlm", arbitration=None,
        engine_result=GARBLED_ELEMS, docling_fallback=CLEAN,
        reason="docling_low_grade=poor",
    )
    assert recC["route"] == "vlm" and keptC == "ENGINE"
    assert "arbitration" not in recC
    assert set(recC.keys()) == {
        "page_index", "route", "reason", "vlm_quality_passes", "vlm_quality_failing_signals",
    }

    # D: engine CLEAN, flag ON -> keep engine (nothing to arbitrate)
    recD, keptD = run(
        "D. VLM clean + flag ON                                     => keep engine",
        engine="vlm", arbitration="1",
        engine_result=CLEAN_ELEMS, docling_fallback=CLEAN,
        reason="docling_low_grade=poor",
    )
    assert recD["route"] == "vlm" and keptD == "ENGINE" and recD["arbitration"] == "kept-engine"

    # E: Textract variant of A -> textract-specific rejected route
    recE, keptE = run(
        "E. Textract garbled + clean Docling + Layer-1 + flag ON    => textract-rejected-kept-docling",
        engine="textract", arbitration="1",
        engine_result=GARBLED_ELEMS, docling_fallback=CLEAN,
        reason="docling_low_grade=poor",
    )
    assert recE["route"] == "textract-rejected-kept-docling" and keptE == "DOCLING"

    print("\n" + "=" * 78)
    print("ALL ASSERTIONS PASSED — arbitration behaves exactly as specified.")
    print("=" * 78)


if __name__ == "__main__":
    main()
