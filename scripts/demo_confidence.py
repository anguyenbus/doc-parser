"""Step-by-step confirmation of the document/page confidence score (§2.3).

Runs the REAL scorer (`parser_service.confidence`) and the REAL `parse_to_markdown`
pipeline. No AWS / credentials: the only thing stubbed is the paid engine call
(`call_vlm` / `analyze_page`) in the end-to-end step. Each step asserts the
documented behavior, so the script exits non-zero on any regression.

    PYTHONPATH=src uv run python scripts/demo_confidence.py
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import parser_service.markdown_pipeline as mp  # noqa: E402
from parser_service.confidence import document_confidence, page_confidence  # noqa: E402
from parser_service.quality_gate import Decision  # noqa: E402

_ORIG_EVALUATE = mp.evaluate_page  # restore between runs


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------------------
# STEP 0 — the pure per-page tier mapping (route + signals -> [0,1])
# ---------------------------------------------------------------------------
hr("STEP 0 — per-page scorer: each route maps to its documented tier")

TIER_CASES = [
    ("docling-kept (clean digital)",        {"route": "docling-kept"},                              0.95),
    ("engine success, quality PASSED",       {"route": "vlm", "engine_quality_passes": True},        0.85),
    ("engine success (arbitration-off shape)", {"route": "vlm", "vlm_quality_passes": True},         0.85),
    ("rejected-kept-docling (arbitration)",  {"route": "vlm-rejected-kept-docling"},                 0.70),
    ("engine kept but quality FAILED",       {"route": "vlm", "engine_quality_passes": False},       0.60),
    ("engine errored -> docling fallback",   {"route": "vlm-fallback-docling"},                      0.50),
    ("error/empty page (engine route, no signal)", {"route": "vlm"},                                 0.15),
]
for label, record, expected in TIER_CASES:
    got = page_confidence(record)
    print(f"  {got:.2f}  (expect {expected:.2f})  {label}")
    assert got == expected, f"{label}: {got} != {expected}"

scores = [page_confidence(r) for _, r, _ in TIER_CASES]
# Monotonicity: docling-kept > engine-pass > rejected > engine-fail > fallback > error.
ordered = [0.95, 0.85, 0.85, 0.70, 0.60, 0.50, 0.15]
assert scores == ordered, scores
assert scores[0] > scores[1] > scores[3] > scores[4] > scores[5] > scores[6]
print("  OK: tiers exact + strictly ordered (docling-kept > engine-pass > rejected > "
      "engine-fail > fallback > error).")

# ---------------------------------------------------------------------------
# STEP 1 — document score is a CONTENT-WEIGHTED mean (weight = n_chars)
# ---------------------------------------------------------------------------
hr("STEP 1 — content-weighting: a long clean doc survives one tiny bad page")

long_clean_tiny_bad = [
    {"route": "docling-kept", "n_chars": 2000},        # big, clean
    {"route": "vlm-fallback-docling", "n_chars": 20},  # tiny, weak
]
doc = document_confidence(long_clean_tiny_bad)
print(f"  2000 clean chars @0.95 + 20 weak chars @0.50  -> document = {doc:.4f}")
assert 0.90 < doc <= 0.95, doc  # stays near the clean tier, not dragged to the mean of tiers
print("  OK: one tiny bad page barely moves the score (weighted by characters, not pages).")

inverted = [
    {"route": "docling-kept", "n_chars": 20},          # tiny, clean
    {"route": "vlm-fallback-docling", "n_chars": 2000},# big, weak
]
doc2 = document_confidence(inverted)
print(f"  20 clean chars @0.95 + 2000 weak chars @0.50 -> document = {doc2:.4f}")
assert 0.50 <= doc2 < 0.55, doc2
# Convex combination: document score can never leave the [min_page, max_page] band.
assert 0.50 <= doc <= 0.95 and 0.50 <= doc2 <= 0.95
print("  OK: a long weak page pulls the score toward its tier; result stays within "
      "[min_page, max_page].")

# ---------------------------------------------------------------------------
# STEP 2 — degenerate cases collapse to 0.0
# ---------------------------------------------------------------------------
hr("STEP 2 — degenerate cases -> 0.0")
assert document_confidence([]) == 0.0
assert document_confidence([{"route": "docling-kept", "n_chars": 0}]) == 0.0
print("  empty page_routes -> 0.0 ; all n_chars == 0 -> 0.0   OK")


# ---------------------------------------------------------------------------
# STEP 3 — end to end on REAL PDFs (real Docling + gate; engine call stubbed)
# ---------------------------------------------------------------------------
def run_pdf(pdf, *, engine_result, force_keep=False):
    """Parse a real PDF offline. The engine call is ALWAYS stubbed (no AWS). When
    ``force_keep`` is set, the gate is forced to ``keep`` so every page with Docling
    content stays ``docling-kept`` (the tiny synthetic fixtures are otherwise
    low-graded by Docling and promoted)."""
    mp.call_vlm = lambda *a, **k: engine_result
    mp.analyze_page = lambda *a, **k: engine_result
    mp.evaluate_page = (
        (lambda *a, **k: Decision("keep", None, None)) if force_keep else _ORIG_EVALUATE
    )
    os.environ.pop("PARSER_ESCALATION_ARBITRATION", None)  # advisory feature; arbitration off
    return mp.parse_to_markdown(pdf)


def show(result):
    conf = result["confidence"]
    print(f"  document confidence : {conf['document']:.4f}")
    for p in conf["pages"]:
        route = next(r["route"] for r in result["page_routes"] if r["page_index"] == p["page_index"])
        print(f"    page {p['page_index']}: confidence={p['confidence']:.2f}  route={route}")
    warns = [w for w in result["warnings"] if w.get("code") == "low_confidence_page"]
    print(f"  low_confidence_page warnings: {len(warns)}")
    for w in warns:
        print(f"    - page {w.get('page_index')}: {w.get('message')}")
    # Advisory / non-gating: markdown is still produced no matter the score.
    print(f"  markdown produced   : {len(result['markdown'])} chars  "
          f"(keys: {sorted(result.keys())})")
    return conf, warns


hr("STEP 3a — all-Docling-kept PDF (gate forced to keep): high confidence, no warnings")
print("  [gate forced to keep — these synthetic fixtures are low-graded by Docling; "
      "forcing keep exercises the all-clean-Docling scoring path]")
res_a = run_pdf(_ROOT / "tests/fixtures/digital_simple.pdf",
                engine_result={"error": "engine not needed"}, force_keep=True)
conf_a, warns_a = show(res_a)
assert set(res_a.keys()) >= {"markdown", "page_routes", "warnings", "confidence"}
assert conf_a["document"] >= 0.90, conf_a["document"]
assert warns_a == []
print("  OK: clean digital doc scores high and raises no advisory warnings.")

hr("STEP 3b — mixed PDF, engine SIMULATED DOWN: page flagged, doc score still honest")
res_b = run_pdf(_ROOT / "tests/fixtures/mixed.pdf",
                engine_result={"error": "simulated engine outage"}, force_keep=False)
conf_b, warns_b = show(res_b)
# The scanned page rendered nothing (n_chars == 0) and scored at the error tier ->
# it is flagged as a low-confidence page, yet (being weightless) it does not drag
# the content-weighted document score down. Advisory warning != gating.
assert len(warns_b) >= 1, "expected a low_confidence_page warning for the failed page"
assert res_b["markdown"], "parsing still produced markdown despite the low score (non-gating)"
print("  OK: the failed page is surfaced as a low_confidence_page warning; parsing was "
      "NOT blocked (advisory only).")

hr("ALL STEPS PASSED — confidence scoring behaves exactly as specified.")
