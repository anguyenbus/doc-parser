# pdfmux insights for doc-parser

**Date:** 2026-06-30
**Author:** research pass over `references/pdfmux` vs. `src/parser_service`
**Status:** findings only — no code changed. Plan to follow.

## Purpose

`references/pdfmux` is a self-auditing, multi-backend PDF extractor (ranks #2 on
opendataloader-bench at zero cost). This doc mines it for mechanisms worth porting
into doc-parser, filtered hard through our constraints. It is deliberately
adversarial: most of pdfmux is **not** applicable to us.

## Hard constraints (govern everything below)

1. **No PyMuPDF / `fitz`.** AGPL — license not open. Several pdfmux mechanisms are
   *implemented* on `fitz` (table detection, column detection, doc-handle cache).
   The **ideas** transfer; the **implementation** must be redone on `pypdfium2`
   (already a dep) or on Docling's bbox provenance (`item.prov[0].bbox`, already
   extracted in `_docling_item_to_element`).
2. **Engines limited to Docling, Textract, and Bedrock (Sonnet or Haiku).** No
   RapidOCR / Surya / Marker / OpenDataLoader / Gemini / GPT / Ollama. Our 2-engine
   escalation design (Docling-first → VLM **or** Textract) stays. No new backends.

## What to explicitly IGNORE from pdfmux

These are the bulk of pdfmux and are wrong for us:

- The 5-backend extractor registry and priority-ordered fallback **chain**
  (RapidOCR→Surya→Docling→Marker→…). We have exactly two escalation targets.
- BYOK multi-provider auto-discovery (Gemini/Claude/GPT/Ollama via YAML +
  entry_points). We are Bedrock-only, region-locked to `ap-southeast-2`, no Opus.
- The `~135 MB` streaming memory claim — that is a PyMuPDF property. Docling's
  ~1.6 GB/worker working set dominates us regardless; not transferable.
- Anything calling `fitz` directly (`pdf_cache.get_doc`, `page.find_tables`,
  `page.get_text("blocks")`).

Adopting any of the above would bloat an intentionally lean design.

---

## Findings (prioritized)

### Tier 1 — high value, fixes a real gap, low effort

#### 1. Arbitrate Docling vs. escalation output; keep the better one ⭐ (headline)

**pdfmux mechanism:** its entire edge is *measure-and-keep-the-better*. After
re-extracting a low-confidence page it does `if re_page.confidence >
original_page.confidence: keep re_page` (`agentic.py`). It never trusts an
escalation blindly.

**Our gap (confirmed by reading the code):**
`src/parser_service/markdown_pipeline.py:447-458` (`_vlm_page_markdown`). We
**already compute** `signals = _measure_text_quality(engine_md)`, record it, and
then `return engine_md` **unconditionally**. The Docling output is right there in
scope as `docling_fallback`, used **only** when the engine produces *structurally*
nothing (error / non-list / empty / whitespace). We never compare the engine
output's quality against the Docling output we already hold.

The docstring rationalizes this ("re-rejecting would just return the worse
output") — but that assumes engine ≥ Docling **always**, which our own
`docs/escalation-engine-comparison.md` contradicts (VLM hallucinated captions,
omitted content). Today those ship as the final answer with **zero guard**.

**Proposed change:** when `signals.passes` is False **and** the Docling fallback
*passes* its own `_measure_text_quality`, keep Docling and record a new route
(e.g. `vlm-rejected-kept-docling` / `textract-rejected-kept-docling`). ~15 lines.
No new dependency. Works for both engines (shared seam).

**Adversarial caveats:**
- Keep it asymmetric. We promoted *because* Docling looked bad, so the prior
  favors the engine. Only override when the engine output fails the heuristics
  **and** Docling's passes — otherwise we'd undo legitimate escalations.
- Ship behind an env flag (e.g. `PARSER_ESCALATION_ARBITRATION=1`) and A/B on the
  bench (NED/TEDS) before flipping the default. The heuristic is a proxy for
  quality, not ground truth.

**Effort:** Low. Highest leverage change in this doc.

#### 2. Retry with backoff + transient/permanent classification

**pdfmux mechanism:** every engine/LLM call is wrapped in `@with_retry`
(`retry.py`): exponential backoff + jitter, honors `Retry-After`, retries on
`429/408/425/5xx`, but fails **immediately** on auth/permanent 4xx
(`is_transient()`).

**Our gap:** `textract_client.analyze_page` (`textract_client.py:100-111`) and
`vlm_client.call_vlm` make **one** call; on *any* exception they return
`{"error": ...}` → silent fallback to Docling. On batch runs, Bedrock
`ThrottlingException` and Textract `ProvisioningThroughputExceeded` /
`ThrottlingException` are routine and transient. Today every throttled page
**silently downgrades to Docling**, quietly corrupting benchmark numbers and prod
quality. Directly relevant since `run_model_compare.sh` runs at concurrency.

**Proposed change:** a small retry helper (exp backoff + jitter, honor
`Retry-After`, classify boto3 error codes: retry `Throttling*` /
`ProvisioningThroughputExceeded` / `ServiceUnavailable` / 5xx; do **not** retry
`AccessDenied*` / `ValidationException` / `InvalidParameter*`). Apply in both
`textract_client` and `vlm_client`. Keep the never-raises contract — exhausting
retries still returns `{"error": ...}`.

**Adversarial caveat:** bound total wait so a throttled batch doesn't stall for
minutes per page; cap attempts (e.g. 3) and max sleep. Add jitter to avoid
thundering-herd across concurrent workers.

**Effort:** Low–medium.

#### 3. Content-addressed cache for the Docling pass (eval-loop accelerator)

**pdfmux mechanism:** SHA-256(file) + params → cached result on disk; re-runs go
from ~14 s to ~0.05 s (`result_cache.py`). (Library-agnostic — does **not** use
fitz. The fitz-based `pdf_cache.py` is the *handle* cache; ignore that one.)

**Our gap:** we re-run Docling every time. Docling is our dominant cost
(60–115 s/page on scans). The active benchmarking workflow runs the **same 11
docs** across Sonnet 3.5 / 4.6 / Haiku / Textract — re-paying the full Docling tax
on every run even though only the escalation engine changes.

**Proposed change:** cache the Docling element output (pre-escalation) keyed on
`(file_sha256, docling_version, render_dpi)`. The escalation layer stays live, so
swapping engines/models reuses cached Docling. Scope it to the eval/dev harness
first; opt-in for prod.

**Adversarial caveat:** **version-pin the key.** Docling version drift is a known
issue (see memory); a stale cache silently serving old-parser output across a
Docling upgrade is an invisible correctness failure. Include `docling_version` and
`render_dpi` in the key — not just the file hash. Add a cache-bust/TTL escape
hatch.

**Effort:** Medium (mostly the keying + invalidation discipline).

### Tier 2 — worth doing, cheap, narrower

#### 4. A document-level confidence score

**pdfmux mechanism:** `compute_document_confidence` (`audit.py`) — content-weighted
average of per-page scores (weight by char count), minus an OCR/escalation-ratio
penalty, returning a single 0–1 plus human-readable warnings.

**Our gap:** we emit `page_routes` but **no aggregate quality number**. RAG
consumers have nothing to threshold on.

**Proposed change:** aggregate the per-page signals we already compute in
`quality_gate` into a doc-level 0–1, weighted by char count, with an
escalation-ratio penalty. Surface in the `parse_to_markdown` return / telemetry.

**Why char-weighting matters:** one bad half-page image shouldn't tank a 20-page
doc to 0.5. Weighting by extracted length is statistically honest.

**Effort:** Low (pure aggregation of existing signals).

#### 5. Dedicated mojibake signal in the Layer-2 gate

**pdfmux mechanism:** explicit mojibake regex (`â€`, `Ã©`, …) in page scoring.

**Our blind spot:** mojibake is Latin-1 **printable**, so it passes our
`_ASCII_PRINTABLE_MIN ≥ 0.90` check *and* slips the garbled-token check in
`quality_gate.py`. A page of encoding-corrupted text currently passes the gate and
never escalates.

**Proposed change:** add a small mojibake-pattern signal to `_measure_text_quality`
/ Layer-2 heuristics so corrupted-encoding pages get promoted.

**Effort:** Low (a regex + threshold + wire into existing signal set).

### Tier 3 — flagged; higher effort or already on our radar

#### 6. Reading-order reconciliation for Textract multi-column

**Already a known lever:** `docs/escalation-engine-comparison.md` §7.2 notes
Textract emits multi-column pages in the wrong order; sequential NED penalizes it
(order-independent NED shows ~96% vs ~63%).

**pdfmux mechanism (the transferable part):** `column_reorder.py` detects columns
**conservatively** (returns None when uncertain), reorders, then **A/B-compares a
self-consistency score and only switches if measurably better** — a safe way to
ship a risky reorder with no regression. *Note:* pdfmux's column detection reads
`page.get_text("blocks")` via fitz — **we cannot use that.** Reimplement column
detection on Textract's own `Geometry.BoundingBox` (we already have block
geometry) or Docling bbox provenance. The reusable idea is the **A/B guard**, not
the fitz plumbing.

**Effort:** Medium; Textract-specific.

#### 7. Budget cap on escalation

**pdfmux mechanism:** stops escalating when a cost budget is exceeded.

**Our gap:** no ceiling — a batch escalates every promoted page unbounded; and our
own TODO notes Textract cost telemetry is missing. A
`PARSER_MAX_ESCALATED_PAGES` / `$`-cap (skip-with-warning past the limit) bounds
runaway Bedrock/Textract spend on a large or degraded corpus. Pairs with closing
the cost-telemetry gap.

**Effort:** Low–medium.

#### 8. Multi-signal table detection as a table-aware escalation trigger

**pdfmux mechanism:** tables are detected by a **voting** score (drawn gridlines +
number-density + column alignment + whitespace columns + `find_tables`), requiring
≥2 signals — far fewer false positives than any single signal.

**Our gap:** our gate keys on text-quality + coverage + Docling confidence. A table
Docling mangles can still pass the **text** gate (the surrounding text looks fine)
while the table *structure* is garbage — and never escalate. *Note:* pdfmux's
signals include `page.find_tables()` / `get_text` via fitz — **not usable.**
Reimplement on `pypdfium2` (line/rect objects, text positions) or lean on Docling's
own table-confidence if exposed. The reusable idea is **voting across multiple weak
signals** before triggering an expensive table escalation.

**Effort:** Medium–high. Lowest priority; flag only.

---

## Recommended sequence

1. **#1 arbitration** behind an env flag — highest leverage, ~15 lines, A/B on bench.
2. **#2 retry wrapper** — stops silent throttle-downgrades polluting benchmarks.
3. **#3 Docling cache** — directly accelerates the model-compare loop we run now.
4. **#4 doc confidence** + **#5 mojibake signal** — cheap quality/telemetry wins.
5. Defer **#6 / #7 / #8** to a later pass; reimplement any fitz-based mechanism on
   `pypdfium2` / Textract geometry / Docling provenance — never PyMuPDF.

## One-line takeaway

pdfmux's whole edge is *measure-and-keep-the-better*; we built the measurement
(`_measure_text_quality` on engine output) and then deliberately disabled the
decision. Re-enabling it (#1), plus retry (#2) and a Docling cache (#3), captures
the transferable value without adding a single backend or touching PyMuPDF.
