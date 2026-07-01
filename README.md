# doc-parser

Turn any document — **PDF, image, DOCX, XLSX, or HTML — into one clean Markdown
file a RAG pipeline can consume.** doc-parser is a **Docling-first, escalation-fallback
hybrid**: Docling parses every page on CPU; a per-page quality gate sends only the
pages Docling struggles with (scanned, garbled, under-extracted) to a stronger
escalation engine — **AWS Bedrock Claude (VLM)** or **AWS Textract**. Most pages stay
on Docling (fast, cheap); the escalation engine is the safety net.

- **Input:** `.pdf`, `.png/.jpg/.jpeg/.tif/.tiff`, `.docx`, `.xlsx/.xlsm`, `.html/.htm`
- **Output:** one `.md` file per document (the product). A schema JSON also exists,
  but it is a test wrapper for benchmarking — not the shipped artifact.

Two guarantees hold throughout: a document **never crashes the run** (all failures
land in `warnings` / `failures.json`), and **an escalation engine is contacted only
when a page escalates** — so HTML/Office and clean digital PDFs run **fully offline**.

A guided notebook for data scientists is at
[`notebooks/walkthrough.ipynb`](notebooks/walkthrough.ipynb).

## Quickstart

```bash
cd doc-parser
uv sync                          # install deps (Docling, pypdfium2, pypdf, boto3, …)

# Only needed if a page may escalate (PDFs the gate promotes, images, scans).
# HTML / DOCX / XLSX and clean digital PDFs are Docling-only and need no AWS.
export AWS_REGION=ap-southeast-2
export BEDROCK_VLM_MODEL=au.anthropic.claude-sonnet-4-6   # latest Sonnet (au.* cross-region profile)
```

### One document → Markdown
```bash
# print markdown to stdout
uv run python scripts/parse_one.py --input report.pdf --format md

# write report.md (or into a directory)
uv run python scripts/parse_one.py --input page.html --format md --output out/
```
`--format`: `md` (the markdown product) · `json` (legacy element JSON — the current
default, kept during the markdown-first transition) · `both`.

### A folder (or S3 prefix) → one `.md` each
```bash
uv run python scripts/parse_batch.py --input ./inbox --output ./out --concurrency 4
uv run python scripts/parse_batch.py --input s3://bucket/in --output s3://bucket/out --budget-usd 5.00
```
Writes `out/<name>.md` per document, plus `route_stats.csv` (which pages went Docling
vs an engine) and `failures.json`, and prints a cost summary. Knobs: `PARSER_CONCURRENCY`,
`PARSER_RENDER_DPI` (default 144), `PARSER_MAX_PAGES`, `--timeout-per-file`,
`--budget-usd` (see [Cost accounting & budget cap](#cost-accounting--budget-cap)).
`--retry-on-throttle` is **deprecated / a no-op** — retry now lives inside the
escalation clients. `--emit-test-json` additionally writes the benchmark JSON wrapper
(eval only).

### Programmatic
```python
from pathlib import Path
from parser_service.markdown_pipeline import parse_to_markdown

result = parse_to_markdown(Path("report.pdf"))
result["markdown"]      # the RAG-ready Markdown string (the product)
result["page_routes"]   # provenance, one per page (see route vocabulary below)
result["confidence"]    # {"document": float, "pages": [{page_index, confidence}]} — advisory only
result["call_counts"]   # {"vlm": int, "textract": int} — this invocation's successful escalation calls
result["warnings"]      # never raises — failures surface here
```

Each `page_routes` record is `{page_index, route, reason, n_chars}` (plus extra
telemetry keys on escalated/arbitrated pages). `n_chars` is the length of the
markdown shipped for that page and is the weight used by the document confidence.

**Route vocabulary** (`route` values):

| route | meaning |
| --- | --- |
| `docling-kept` | gate kept Docling's page markdown (no engine call) |
| `vlm` / `textract` | the escalation engine's markdown replaced the page |
| `vlm-fallback-docling` / `textract-fallback-docling` | engine errored/emptied → gate-flagged Docling shipped |
| `vlm-rejected-kept-docling` / `textract-rejected-kept-docling` | arbitration kept a clean Docling page over a garbled engine page (opt-in; below) |

`reason` carries the gate's reason for promoting, or a mode marker: `scan_fastpath`
(fast-path) or `throttled` (retry exhausted on a throttle).

## Escalation engine

`PARSER_ESCALATION_ENGINE` (default `vlm`) chooses which engine parses the pages the
quality gate promotes. Docling still handles confident pages either way; this only
swaps the escalation engine, and both emit the **same element-JSON shape**, so the
downstream renderer and Docling-fallback behavior are identical.

```bash
export PARSER_ESCALATION_ENGINE=vlm       # (default) Bedrock Claude Sonnet on promoted pages
export PARSER_ESCALATION_ENGINE=textract  # AWS Textract synchronous AnalyzeDocument (LAYOUT+TABLES)
```

`textract` sends the rendered page bytes directly — no S3, no async
`StartDocumentAnalysis`, one call per promoted page (≤10 MB, ~5 MB recommended). It
reuses `AWS_REGION` and the same instance-role credentials as the VLM and adds no new
heavy dependency (boto3 is already installed). The default stays `vlm`; flipping to
`textract` (or a per-page routing policy) is gated on a larger escalating corpus — see
[Evaluation](#evaluation).

## Optional feature toggles (all default OFF — opt-in)

Each toggle below defaults **OFF**; with it off the pipeline is byte-identical to the
baseline. They ship opt-in pending a benchmark A/B, and are turned on per run.

### Escalation output arbitration — `PARSER_ESCALATION_ARBITRATION=1`

A post-escalation *keep-the-better-of* chooser. After a page escalates and the engine
returns a successful, non-empty rendering, arbitration compares that engine markdown
against the Docling markdown already rendered for the same page and **keeps Docling**
when **all** hold: (1) the engine output fails the `_measure_text_quality` garble
proxy, (2) a real Docling rendering exists and its text passes the same proxy, and
(3) the promotion was **not** a coverage promotion (`reason` prefix `low_coverage:` —
never reverted, since Docling there is clean but *incomplete* and reverting would drop
the content escalation recovered). The gate's promote decision is untouched; this only
chooses between two already-produced renderings.

When it fires, the page is recorded under `vlm-rejected-kept-docling` /
`textract-rejected-kept-docling` (a **Docling-output** route — excluded from the
`vlm`/`textract` roll-up and from `vlm_pages`, still counted in the route sum
invariant), and the `page_routes` entry gains `engine_quality_*` / `docling_quality_*`
signals and an `arbitration` marker (`kept-engine` vs `kept-docling`).

### Scan fast-path — `PARSER_SCAN_FASTPATH=1`

Skip the dominant 60–115 s/page Docling CPU pass on documents that are **all**
image-only scans. Before `DocumentConverter.convert()`, a cheap probe classifies each
page: it qualifies iff `text_layer_tokens[page] == 0` **AND** the page has an embedded
image. The has-image check uses **`pypdf`** (BSD-3) — a resource-level `/XObject /Image`
walk, recursing into `/Form` XObjects — **not** PyMuPDF (AGPL), and **not** pypdfium2
(whose painted-content walk misses resource-declared images). When *every* page
qualifies, Docling would extract nothing anyway and each page would escalate, so
`convert()` is skipped entirely and each page routes straight to the engine with
`reason="scan_fastpath"`; page count/indices and the additive `n_chars` match the
normal path, so eval grading is unaffected (only `reason` differs). Recorded latency on
the 2-page all-scanned fixture: ~11.6 s → ~0.17 s.

- **OCR-skip risk (why it is opt-in).** The fast-path forfeits Docling's OCR. On a real
  scan where Docling OCR would have *succeeded* and the gate would have *kept* it,
  going straight to escalation changes the output. Flip it on only for batches you know
  are hopeless-for-Docling scans.
- **Mixed-document limitation.** `convert()` is whole-document; Docling has no per-page
  convert — so only *all-scanned* documents skip it. Any text-bearing page keeps its
  Docling pass and fallback.

## Confidence score (advisory, non-gating)

`parse_to_markdown` returns a `confidence` block — a 0–1 `document` score plus a
per-page map — derived entirely from the routing outcomes doc-parser already computes,
so a downstream RAG consumer can threshold low-confidence documents for review without
reverse-engineering the route vocabulary.

**It is advisory only.** The score **NEVER** gates parsing, routing, the quality gate,
the promote/keep decision, or the shipped Markdown — it is computed read-only over
`page_routes` after the fact. It is a transparent, **routing-derived heuristic**,
**NOT** a gold-NED/TEDS-calibrated probability of correctness.

Per-page base score (`src/parser_service/confidence.py`), keyed on the page `route`:

| route | score | meaning |
| --- | --- | --- |
| `docling-kept` | 0.95 | gate kept a clean digital Docling page |
| `vlm` / `textract`, quality signal passes | 0.85 | engine shipped and its text-quality signal passed |
| `*-rejected-kept-docling` | 0.70 | promoted, but the clean Docling render shipped |
| `vlm` / `textract`, signal fails but kept | 0.60 | engine shipped but its text-quality signal failed |
| `*-fallback-docling` | 0.50 | engine errored/emptied; gate-flagged Docling shipped |
| error / empty / unparseable page | 0.15 | no Docling and no engine output for the page |

The **document score** is the content-weighted mean `Σ(page_score × n_chars) / Σ(n_chars)`
— a convex combination, so it stays within `[min_page, max_page]` (one tiny bad page
can't tank a long clean doc). `Σ(n_chars) == 0` or no `page_routes` → `0.0`.

Pages below the advisory review threshold surface as additive `low_confidence_page`
warnings (`scope="page"`, page index, advisory message). The threshold
(`confidence.LOW_CONFIDENCE_THRESHOLD`, `0.65`) is derived as the midpoint between the
highest **untrusted** tier (engine-failing-kept, 0.60) and the lowest **trusted** tier
(rejected-kept-docling, 0.70), so it flags exactly the untrusted tiers (0.60 / 0.50 /
0.15) and never coincides with a tier value. Confidence lives only in the
`parse_to_markdown` return dict (not the route CSV).

## Cost accounting & budget cap

### Per-invocation call counts

`parse_to_markdown` returns `call_counts` `{"vlm": int, "textract": int}` — the
successful escalation calls **this invocation** made. The two counters
(`vlm_client` / `textract_client`) are backed by `threading.local()`, so under the
batch's default concurrency each file's counts are **race-free per worker thread** (a
concurrent neighbor's reset at the start of its own parse cannot corrupt another
thread's count). The public `get_*_call_count` / `reset_*_call_count` API is unchanged.

### Both-engine cost estimate

`scripts/parse_batch.py` sums the per-file `call_counts` and reports a both-engine cost
in the run summary (previously a Textract-only batch reported `$0`):

- **Bedrock (VLM) leg:** `total_vlm_calls × _AVG_COST_PER_CALL`, a per-call token model
  using **average** input/output tokens.
- **Textract leg:** `total_textract_calls × TEXTRACT_PRICE_PER_PAGE` (one
  `AnalyzeDocument` LAYOUT+TABLES call per promoted page).

The summary carries `estimated_cost_usd` (combined total), the per-engine breakdown
(`bedrock_cost_usd`, `textract_cost_usd`, `total_textract_calls`, `total_vlm_calls`),
and a pre-flight `preflight_worst_case_usd` = `Σ page_count × per-page cost` (a
worst-case bound, since the firing engine per page is unknown until the gate runs).

**The number is an ESTIMATE, not billed cost** (`cost_is_estimate: true`): the Bedrock
leg uses average tokens, not metered usage. `TEXTRACT_PRICE_PER_PAGE = $0.019/page` is
**PROVISIONAL** (the starting figure from `docs/escalation-engine-comparison.md`);
verifying the live `ap-southeast-2` LAYOUT+TABLES per-page price is a pending human/ops
step (see the code comment on the constant).

### `--budget-usd` document-level cap

`--budget-usd <float>` (default: disabled) is a spend ceiling enforced **at the
document boundary**. Running spend accumulates from each completed file's estimated
cost; once it would exceed the ceiling, escalation is halted and **every
subsequently-dispatched file parses via Docling only (no engine calls)** — the batch
never crashes mid-run. The stop shows in the summary via `budget_exceeded`,
`budget_exceeded_at_file_index`, and `running_spend_usd`.

The cap is **coarse**: checked at the document boundary with concurrent files, actual
spend can overshoot by ~one document's worth of in-flight escalation. Per-page hard
capping is out of scope (a mid-page shared spend counter under concurrency would
re-introduce the cross-thread race the thread-local counters removed).

## Retry + throttle observability

Both escalation clients (`vlm_client.call_vlm` → Bedrock `invoke_model`,
`textract_client.analyze_page` → Textract `analyze_document`) wrap **only** their inner
AWS call in a bounded exponential-backoff retry (`parser_service.retry`). Transient
failures — throttling / provisioned-throughput / timeout / connection / 5xx (incl.
429 / 408 / 425), and the botocore `ThrottlingException` /
`TooManyRequestsException` / `ProvisionedThroughputExceededException` /
`ServiceUnavailable` codes — are retried with 50–100% jitter, honoring a `Retry-After`
header when present. Permanent errors (auth / `AccessDenied*` / `ValidationException` /
404 / 4xx) **fail fast**. The retry lives inside the client's existing `try`, so the
never-raises `{"error": ...}` contract is unchanged when retries exhaust.

When retries are exhausted on a **throttle**, the client returns
`{"error": ..., "error_kind": "throttled"}`, and the escalation seam records that
page's `*-fallback-docling` route with `reason="throttled"` instead of the gate reason
— so a throttle storm is **visible** in `page_routes` (the route **vocabulary is
unchanged**; the throttle rides in `reason`). `route_stats` derives a `throttled_pages`
count (and a `route_stats.csv` column of the same name; the CSV is read by name via
`DictReader`, so adding a column is safe). Per-page route counts still sum to the page
count; every non-throttle path keeps today's gate reason.

## Architecture

```
doc ─► [scan fast-path?] ─► Docling parse ─► per page ─► quality gate ─┬─ confident ──► Docling page markdown
                                                                       └─ promote ──► render page ─► engine (VLM│Textract)
                                                                                       │  (empty/garbage? → keep Docling md)
                                                                                       └─ arbitration? keep the better of the two
                                          ─► concatenate pages in order ─► document.md  (+ page_routes, confidence, call_counts)
```

- **Docling** does the bulk of parsing on CPU; each page is serialized with the
  parser-owned `markdown.render_markdown` (chosen over Docling's native
  `export_to_markdown` — it matches the benchmark gold more closely; see
  [the route decision](agent-os/specs/2026-05-30-markdown-first-pipeline/planning/spike-route-decision.md)).
- **Quality gate** ([quality_gate.py](src/parser_service/quality_gate.py)), per page:
  *Layer 1* Docling confidence (POOR/FAIR → escalate); *coverage* (extracted ≪
  text-layer tokens → escalate, catches silent under-extraction); *Layer 2* text
  heuristics (garbled / repeated-char). Numeric-aware so chart/financial pages aren't
  mistaken for garble.
- **Escalation engines:** [vlm_client.py](src/parser_service/vlm_client.py) (Bedrock
  Claude) and [textract_client.py](src/parser_service/textract_client.py) (AWS
  Textract, with column-aware multi-column reading-order reconciliation). Both emit the
  same element-JSON; an empty engine result falls back to Docling for that page.
- **Format routing:** PDF → per-page gate; images → gated one-page path; DOCX / XLSX /
  HTML → whole-doc Docling export, gate skipped (one logical page).

JSON is produced only by `wrap_md_as_prediction` (a 1-element envelope around the
markdown) and only on the eval path — it is how the markdown is scored, not what ships.

## Evaluation

Quality is measured with the **doc-bench** wheel (frozen dataset samples, Docling
baselines, fixtures, schema, and the grader).

```bash
uv tool install --force ./doc_bench-0.1.0-py3-none-any.whl   # puts doc-bench* on PATH
scripts/run_benchmark.sh                                     # parse + grade, both engines
```
[run_benchmark.sh](scripts/run_benchmark.sh) parses each bundled doc to markdown, wraps
it as a prediction (`--emit-test-json`), grades it against the wheel's **bundled gold**
via the `doc-bench` CLI, and aggregates per-doc **NED** (text similarity ↑) + **TEDS**
(table structure ↑) with parse latency. See [EVAL_RUNBOOK.md](EVAL_RUNBOOK.md) and
[docs/escalation-engine-comparison.md](docs/escalation-engine-comparison.md).

On clean corpora doc-parser **matches the Docling baseline** (the hybrid's job is to
*not regress* Docling while rescuing degraded pages).

### Escalation engine & VLM model

Two knobs decide how escalated pages are parsed: the **engine**
(`PARSER_ESCALATION_ENGINE=vlm|textract`) and, for the VLM, the **model**
(`BEDROCK_VLM_MODEL`). Only gate-promoted pages differ; `docling-kept` pages are
byte-identical. The default model is **the latest Sonnet, `au.anthropic.claude-sonnet-4-6`**
(Sonnet 3.5 is retired). Reproduce with
[scripts/run_model_compare.sh](scripts/run_model_compare.sh); the historical Sonnet-3.5
comparison is preserved in
[docs/escalation-engine-comparison.md](docs/escalation-engine-comparison.md) (marked
superseded).

Latest re-benchmark (2026-06-30, Sonnet 4.6) on the two bundled docs that escalate
(NED ↑; Textract shown as a fixed, model-independent reference):

| escalated doc | Sonnet 4.6 | Haiku 4.5 | Textract |
|---|--:|--:|--:|
| **ato_bench** `1371-6.1997` (scanned form) | 0.648 | **0.706** | 0.322 |
| **dp_bench** `…027` (figure-heavy page) | 0.287 | 0.205 | **0.887** |

Read together:

- **Neither engine dominates.** The VLM wins the scanned form ~2× (grounding a
  hallucination/omission-prone model still beats Textract's flat OCR on a scan); Textract
  wins the figure-heavy page ~3× (its plain OCR matches a verbatim-label gold, while the
  VLM's chart *interpretation* is penalized by sequential edit distance). **Haiku 4.5
  is competitive with — and cheaper/faster than — Sonnet 4.6.**
- **A per-page routing policy is not yet built:** the direction of the win *inverted*
  when the VLM model was updated (Sonnet 3.5 lost the scan, 4.6 wins it), and only **2
  docs escalate** in the bundled set — far too few to learn a per-page-type policy.
  Growing the escalating corpus is the prerequisite before routing per page.

> **Benchmark caveat (doc-bench side):** the bundled DP-Bench / OmniDocBench samples are
> single-page (multi-page concatenation is covered by the test suite), and the gold's
> table rendering is still evolving — see [`DOC_BENCH_GOLD_ISSUE.md`](DOC_BENCH_GOLD_ISSUE.md).

## Development

```bash
uv run pytest -q          # test suite (mocks the escalation engines; no AWS)
uv run ruff check src/    # lint
uv run mypy src/          # type-check
```

The test suite mocks the VLM and Textract and needs no AWS. Only benchmark runs
(`run_benchmark.sh`, `run_model_compare.sh`) contact Bedrock/Textract and the doc-bench
wheel.
