"""Step-by-step demo of item #18 — correct per-engine cost accounting + budget cap.

Drives the REAL code offline (no AWS/credentials): only the paid engine call
(`call_vlm`) is stubbed. Each step asserts the documented behavior, so the script
exits non-zero on any regression.

    PYTHONPATH=src uv run python scripts/demo_cost_budget.py
"""
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for p in (_ROOT / "src", _ROOT / "scripts"):
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

import parser_service.markdown_pipeline as mp  # noqa: E402
from parser_service.quality_gate import Decision  # noqa: E402
from parser_service.vlm_client import _increment_vlm_call_count  # noqa: E402
import parse_batch as pb  # noqa: E402

FIX = _ROOT / "tests/fixtures"


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------------------
# STEP 0 — race-free PER-INVOCATION counts (the headline correctness fix)
# ---------------------------------------------------------------------------
hr("STEP 0 — parse_to_markdown returns this invocation's own call_counts")

# Force every page to escalate, and stub the engine to a clean success. The stub
# calls the SAME increment helper the real call_vlm uses on success, so the
# (thread-local) counter advances exactly as in production — we bypass only the
# paid AWS network call, not the counting. (No AWS.)
def _fake_vlm(*a, **k):
    _increment_vlm_call_count()
    return {"elements": [{"type": "paragraph", "text": "clean engine text for page"}]}


mp.call_vlm = _fake_vlm
mp.evaluate_page = lambda *a, **k: Decision("promote_to_vlm", "forced_for_demo", layer=1)

res2 = mp.parse_to_markdown(FIX / "digital_simple.pdf")       # 2 pages
print(f"  digital_simple.pdf (2 pages): call_counts = {res2['call_counts']}")
assert res2["call_counts"] == {"vlm": 2, "textract": 0}, res2["call_counts"]

res3 = mp.parse_to_markdown(FIX / "three_page_digital.pdf")   # 3 pages
print(f"  three_page_digital.pdf (3 pages): call_counts = {res3['call_counts']}")
assert res3["call_counts"] == {"vlm": 3, "textract": 0}, res3["call_counts"]
print("  OK: each invocation reports exactly its own escalation calls (2 vs 3).")

hr("STEP 0b — the RACE fix: two parses CONCURRENTLY, counts don't cross-contaminate")
# Under the OLD module-global counter, thread B's reset_*() at the start of its
# parse would corrupt the count thread A is about to read. With thread-local
# counters each worker thread has its own counter, so both are correct.
with ThreadPoolExecutor(max_workers=2) as ex:
    fut_a = ex.submit(mp.parse_to_markdown, FIX / "digital_simple.pdf")      # expect vlm=2
    fut_b = ex.submit(mp.parse_to_markdown, FIX / "three_page_digital.pdf")  # expect vlm=3
    a = fut_a.result()["call_counts"]
    b = fut_b.result()["call_counts"]
print(f"  thread A (2-page) -> {a}")
print(f"  thread B (3-page) -> {b}")
assert a == {"vlm": 2, "textract": 0}, a
assert b == {"vlm": 3, "textract": 0}, b
print("  OK: concurrent parses each keep their OWN counts (thread-local; would")
print("      cross-contaminate under the old shared module global).")


# ---------------------------------------------------------------------------
# STEP 1 — both-engine cost model (fixes the Textract $0 bug)
# ---------------------------------------------------------------------------
hr("STEP 1 — Textract batches now report NON-ZERO cost; mixed batches sum both")


def _file(vlm=0, textract=0):
    """A per-file batch result record (as the batch builds from call_counts)."""
    return {"vlm_calls": vlm, "textract_calls": textract}


# Before this change: cost = total_vlm_calls * price → a Textract-only batch = $0.
textract_only = [_file(textract=4), _file(textract=6)]          # 10 Textract pages
summary_t = pb._compute_cost_summary(textract_only)
print(f"  Textract-only batch (10 pages): {summary_t}")
assert summary_t["estimated_cost_usd"] > 0, "Textract batch must NOT be $0 anymore"
assert summary_t["total_textract_calls"] == 10
assert summary_t["bedrock_cost_usd"] == 0.0

mixed = [_file(vlm=3), _file(textract=5), _file(vlm=1, textract=2)]  # 4 vlm, 7 textract
summary_m = pb._compute_cost_summary(mixed)
print(f"  Mixed batch (4 VLM calls + 7 Textract pages): {summary_m}")
expected = round(4 * pb._AVG_COST_PER_CALL + 7 * pb.TEXTRACT_PRICE_PER_PAGE, 6)
assert summary_m["estimated_cost_usd"] == expected
assert summary_m["estimated_cost_usd"] == round(
    summary_m["bedrock_cost_usd"] + summary_m["textract_cost_usd"], 6
)
assert summary_m["cost_is_estimate"] is True
print(f"  OK: estimated_cost_usd == bedrock ({summary_m['bedrock_cost_usd']}) + "
      f"textract ({summary_m['textract_cost_usd']}); flagged an ESTIMATE.")
print(f"  (Textract price is PROVISIONAL: ${pb.TEXTRACT_PRICE_PER_PAGE}/page — "
      f"pending live ap-southeast-2 verification.)")


# ---------------------------------------------------------------------------
# STEP 2 — pre-flight estimate (worst-case bound over page counts)
# ---------------------------------------------------------------------------
hr("STEP 2 — pre-flight estimate: Σ pages × per-engine cost (worst-case bound)")
pre = pb._preflight_estimate([2, 3, 5], "textract")   # 10 pages if EVERY page escalated
print(f"  worst-case Textract estimate for docs of 2+3+5 pages: ${pre}")
assert pre == round(10 * pb.TEXTRACT_PRICE_PER_PAGE, 6)
print("  OK: an upper bound assuming every page escalates (most stay on Docling).")


# ---------------------------------------------------------------------------
# STEP 3 — document-level --budget-usd cap (BudgetTracker)
# ---------------------------------------------------------------------------
hr("STEP 3 — --budget-usd stops escalating further files once spend trips the cap")

# Each file here costs 5 * TEXTRACT_PRICE_PER_PAGE ≈ $0.095. Budget $0.20 → the cap
# trips after ~2-3 files; later files are dispatched with escalation suppressed.
per_file = _file(textract=5)
budget = pb.BudgetTracker(budget_usd=0.20)
print(f"  budget = ${budget.budget_usd};  per-file spend = ${pb._file_cost(per_file):.4f}")
escalated, suppressed = [], []
for i in range(5):
    if budget.allows_escalation():
        escalated.append(i)
        budget.record(per_file, i)          # this file escalated → adds spend
    else:
        suppressed.append(i)                # Docling-only, no engine cost added
        budget.record(_file(), i)           # a suppressed file adds $0
    print(f"    file {i}: {'ESCALATE' if i in escalated else 'suppressed (Docling-only)':<26} "
          f"running_spend=${budget.running_spend:.4f}")

print(f"  summary: {budget.summary_fields()}")
assert budget.budget_exceeded is True
assert suppressed, "expected later files to be suppressed once the cap tripped"
assert budget.exceeded_at_file_index == escalated[-1]
print(f"  OK: escalated files {escalated}, then suppressed {suppressed}; cap tripped "
      f"at file {budget.exceeded_at_file_index}.")

hr("STEP 3b — no --budget-usd (None): unchanged behavior, always allows escalation")
nobudget = pb.BudgetTracker(budget_usd=None)
for i in range(5):
    assert nobudget.allows_escalation() is True
    nobudget.record(per_file, i)
print(f"  5 files, no budget: budget_exceeded = {nobudget.summary_fields()['budget_exceeded']} "
      f"(always allows; no behavior change)")
assert nobudget.budget_exceeded is False

hr("ALL STEPS PASSED — cost accounting + budget cap behave exactly as specified.")
