"""Run a REAL PDF through the REAL pipeline to watch escalation arbitration.

This runs the genuine path — real Docling conversion, real quality gate, real
``parse_to_markdown`` — on an actual PDF. The ONLY thing stubbed is the paid,
region-locked engine call (``call_vlm`` / ``analyze_page``): it is replaced with a
stand-in that returns deliberately GARBLED text, i.e. the exact failure mode
arbitration is meant to catch (a hallucinating / degraded engine response).

With ``PARSER_ESCALATION_ARBITRATION=1``, any page the gate PROMOTES *and* for which
Docling produced clean output (a non-coverage promotion) should be routed
``*-rejected-kept-docling`` — the clean Docling markdown is kept over the garbled
engine output. Pages with no Docling output (pure scans) carry ``docling_fallback=None``
so arbitration correctly no-ops there.

Usage (no AWS, no credentials needed):

    PYTHONPATH=src uv run python scripts/demo_arbitration_pdf.py [PATH_TO_PDF]

Default PDF: tests/fixtures/mixed.pdf
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import parser_service.markdown_pipeline as mp  # noqa: E402
from parser_service.quality_gate import Decision  # noqa: E402

# Deliberately garbled "engine" output — short alnum tokens that fail the quality
# proxy (garbled_token_ratio high, dict_hit_rate ~0). This simulates a bad VLM/
# Textract response so we can watch arbitration reject it in favor of Docling.
GARBLED = " ".join(
    ["x3q9", "z7w2", "k4j1", "b8n5", "q2x6", "z9w3", "k1j7", "b3n8", "x5q2", "z1w8"] * 6
)
_GARBLED_RESULT = {"elements": [{"type": "paragraph", "text": GARBLED}]}


def _fake_engine(*args, **kwargs):
    """Stand-in for call_vlm / analyze_page — always returns garbled elements."""
    return _GARBLED_RESULT


def main():
    args = [a for a in sys.argv[1:] if a != "--force-promote"]
    force_promote = "--force-promote" in sys.argv
    pdf = Path(args[0]) if args else _ROOT / "tests/fixtures/mixed.pdf"
    if not pdf.is_file():
        sys.exit(f"PDF not found: {pdf}")

    # Turn arbitration ON and stub ONLY the paid engine call. Docling + the gate run
    # for real. (Engine is 'vlm' by default; the stub covers both engines.)
    os.environ["PARSER_ESCALATION_ARBITRATION"] = "1"
    os.environ.setdefault("PARSER_ESCALATION_ENGINE", "vlm")
    mp.call_vlm = _fake_engine
    mp.analyze_page = _fake_engine

    if force_promote:
        # Simulate a Layer-1 low-confidence grade on EVERY page that has Docling
        # output, so a clean digital PDF exercises the arbitration branch. The real
        # gate is bypassed ONLY for the promote decision; the reason is a Layer-1
        # style string (NOT 'low_coverage:'), so arbitration is eligible to fire.
        def _forced_promote(page_idx, result, page_elems, page_text_layer_tokens=None):
            return Decision(
                action="promote_to_vlm",
                reason="docling_low_grade=poor (forced for demo)",
                layer=1,
            )
        mp.evaluate_page = _forced_promote
        print("  [--force-promote] gate forced to a Layer-1 promotion so clean-Docling "
              "pages reach the arbitration branch.\n")

    print(f"Parsing (real Docling + real gate, engine call stubbed → garbled): {pdf}")
    print(f"  PARSER_ESCALATION_ARBITRATION={os.environ['PARSER_ESCALATION_ARBITRATION']}  "
          f"PARSER_ESCALATION_ENGINE={os.environ['PARSER_ESCALATION_ENGINE']}\n")

    result = mp.parse_to_markdown(pdf)

    print("=" * 78)
    print("PER-PAGE ROUTES")
    print("=" * 78)
    for r in result["page_routes"]:
        arb = r.get("arbitration", "-")
        reason = r.get("reason")
        print(f"  page {r['page_index']}: route={r['route']:<28} "
              f"arbitration={arb:<12} reason={reason}")
        if "engine_quality_passes" in r:
            print(f"           engine_quality_passes={r['engine_quality_passes']}  "
                  f"docling_quality_passes={r.get('docling_quality_passes')}")

    rejected = [r for r in result["page_routes"] if "rejected-kept-docling" in r["route"]]
    print("\n" + "=" * 78)
    if rejected:
        print(f"ARBITRATION FIRED on {len(rejected)} page(s): garbled engine output was "
              f"rejected and clean Docling markdown was kept.")
    else:
        print("Arbitration did not fire on this document (no page had BOTH a non-coverage "
              "promotion AND clean Docling output to keep). Try another fixture, e.g. "
              "tests/fixtures/scanned.pdf.")
    print("=" * 78)

    if result["warnings"]:
        print("\nWarnings:")
        for w in result["warnings"]:
            print(f"  - [{w.get('code')}] {w.get('message')}")


if __name__ == "__main__":
    main()
