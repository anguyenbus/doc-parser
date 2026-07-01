# doc-parser

Turn any document — **PDF, image, DOCX, XLSX, or HTML — into one clean Markdown
file a RAG pipeline can consume.** doc-parser is a **Docling-first, VLM-fallback**
hybrid: Docling parses every page; a per-page quality gate sends only the pages
Docling struggles with (scanned, garbled, under-extracted) to a vision model
(**AWS Bedrock Claude**). Most pages stay on Docling (fast, cheap); the VLM is the
safety net.

- **Input:** `.pdf`, `.png/.jpg/.jpeg/.tif/.tiff`, `.docx`, `.xlsx/.xlsm`, `.html/.htm`
- **Output:** a `.md` file per document (the product). A schema JSON also exists,
  but it's only a test wrapper for benchmarking — not the shipped artifact.

A guided notebook for data scientists is at
[`notebooks/walkthrough.ipynb`](notebooks/walkthrough.ipynb).

## Quickstart

```bash
cd doc-parser
uv sync                          # install deps (Docling, pypdfium2, boto3, …)

# Only needed if the VLM may run (PDFs that escalate, images, scans).
# HTML / DOCX / XLSX are Docling-only and need no AWS.
export AWS_REGION=ap-southeast-2
export BEDROCK_VLM_MODEL=au.anthropic.claude-sonnet-4-6
```

**Escalation engine** (`PARSER_ESCALATION_ENGINE`, default `vlm`): chooses which
engine parses pages the quality gate promotes to escalation. Docling still handles
confident pages either way; this only swaps the escalation engine.

```bash
# vlm (default): Bedrock Claude Sonnet on promoted pages.
export PARSER_ESCALATION_ENGINE=vlm

# textract: AWS Textract synchronous AnalyzeDocument (FeatureTypes LAYOUT+TABLES),
# sending the rendered page bytes directly — no S3, no async StartDocumentAnalysis,
# one call per promoted page (<=10 MB, ~5 MB recommended). Reuses AWS_REGION and the
# same instance-role credentials as the VLM (verified in ap-southeast-2); adds no new
# heavy local dependency (boto3 is already installed). Emits the same element-JSON
# the VLM does, so Docling fallback on empty/garbage output is identical.
export PARSER_ESCALATION_ENGINE=textract
```

The default stays `vlm` (zero behavior change). Flipping the default to `textract`
is gated on a head-to-head eval, not this switch.

**Escalation output arbitration** (`PARSER_ESCALATION_ARBITRATION`, default **OFF**):
a post-escalation *keep-the-better-of* chooser. After a page is escalated and the
engine returns a successful, non-empty rendering, arbitration compares that engine
markdown against the Docling markdown already rendered for the same page and keeps
Docling when **all** hold: (1) the engine output fails the `_measure_text_quality`
garble proxy, (2) a real Docling rendering exists and its text passes the same proxy,
and (3) the promotion was **not** a coverage promotion (`reason` prefix
`low_coverage:` — never reverted, since Docling there is clean but *incomplete* and
reverting would drop the content escalation recovered). The gate's promote decision
is untouched; this only chooses between two already-produced renderings.

```bash
# off (default): engine output ships unconditionally on the success path (today's
# behavior — byte-identical markdown and route_stats).
# on: keep a clean Docling page over a garbled/hallucinated engine page.
export PARSER_ESCALATION_ARBITRATION=1
```

When it fires, the page is recorded under a new per-page route —
`vlm-rejected-kept-docling` or `textract-rejected-kept-docling` — which counts as a
**Docling-output** page (excluded from the `vlm`/`textract` document roll-up and from
`vlm_pages`; still counted in the route sum invariant). The `page_routes` entry adds
telemetry: `engine_quality_passes` / `engine_quality_failing_signals` (the engine's
signals, also mirrored under the back-compat `vlm_quality_*` keys),
`docling_quality_passes` / `docling_quality_failing_signals` (the Docling signals,
populated only when arbitration is on and a Docling rendering exists), and an
`arbitration` marker (`kept-engine` vs `kept-docling`).

Default stays **OFF**. A bench A/B (NED / TEDS) must be recorded and show
non-regression **before** any proposal to flip the default to ON.

**Scan fast-path** (`PARSER_SCAN_FASTPATH`, default **OFF**): skip the dominant
60–115 s/page Docling CPU pass on documents that are *all* image-only scans. Before
`DocumentConverter.convert()`, a cheap probe classifies each page with the
text-layer/has-images signals we already own: a page qualifies for the fast-path iff
`text_layer_tokens[page] == 0` **AND** the page has an embedded image (`pypdf`
resource-level `/XObject /Image` walk, recursing into `/Form` XObjects — **not**
PyMuPDF, which is AGPL, and **not** pypdfium2, whose content-walk misses
resource-declared images). When **every** page qualifies, Docling would extract
nothing on every page and each page would escalate anyway, so `convert()` is skipped
entirely and every page is routed straight to the escalation engine with
`reason="scan_fastpath"`. Page count/indices and the additive route keys
(`n_chars`) match the normal path exactly, so eval grading is unaffected — only
`reason` differs.

```bash
# off (default): all-scanned docs still run Docling convert (byte-identical to today).
# on: all-scanned docs skip convert and escalate every page directly.
export PARSER_SCAN_FASTPATH=1
```

**OCR-skip risk (why it ships OPT-IN, default OFF).** The fast-path forfeits
Docling's OCR pass on scanned pages. On a real scan where Docling OCR would have
*succeeded* and the quality gate would have *kept* it, going straight to escalation
changes the output. With the flag OFF the pipeline is byte-identical to today
(regression-tested). Flip it ON only for batches you know are hopeless-for-Docling
scans, or after a bench A/B shows non-regression.

**Mixed-document limitation (honest scope).** `DocumentConverter.convert()` is
whole-document; Docling has **no** per-page convert. So a mixed document (any
text-bearing page) **cannot** skip convert — only *all-scanned* documents do. The
fast-path never skips Docling on a page with a usable text layer, so that page keeps
its Docling pass and its fallback. Recorded latency on the 2-page all-scanned fixture:
~11.6 s (convert) → ~0.17 s (fast-path).

**Escalation client retry + throttle observability.** Both escalation clients
(`vlm_client.call_vlm` → Bedrock `invoke_model`, `textract_client.analyze_page` →
Textract `analyze_document`) wrap **only** their inner AWS call in a bounded
exponential-backoff retry (`parser_service.retry`). Transient failures —
throttling / provisioned-throughput / timeout / connection / 5xx (incl. 429 / 408 /
425) and the botocore `ThrottlingException` / `TooManyRequestsException` /
`ProvisionedThroughputExceededException` / `ServiceUnavailable` error codes — are
retried with 50–100% jitter, honoring a `Retry-After` header when present; permanent
errors (auth / `AccessDenied*` / `ValidationException` / 404 / 4xx) **fail fast** and
are never retried. The retry lives **inside** the client's existing `try`, so the
public never-raises `{"error": ...}` contract is unchanged when retries exhaust.

When retries are exhausted on a **throttle**, the client returns
`{"error": ..., "error_kind": "throttled"}`. The escalation seam then records that
page's `*-fallback-docling` route with a distinct `reason="throttled"` (instead of
the gate reason), so a throttle storm is **visible** in `page_routes` — the route
**vocabulary is unchanged**; the throttle fact rides only in `reason`. `route_stats`
derives a `throttled_pages` count from that reason (and a `route_stats.csv` column of
the same name); the per-page route counts still sum to the page count. Every
non-throttle path keeps today's gate reason (byte-for-byte).

### One document → Markdown
```bash
# print markdown to stdout
uv run python scripts/parse_one.py --input report.pdf --format md

# write report.md (or into a directory)
uv run python scripts/parse_one.py --input page.html --format md --output out/
```
`--format`: `md` (the markdown product), `json` (legacy element JSON — current
default, kept during the markdown-first transition), `both`.

### A folder (or S3 prefix) → one `.md` each
```bash
uv run python scripts/parse_batch.py --input ./inbox --output ./out --concurrency 4
uv run python scripts/parse_batch.py --input s3://bucket/in --output s3://bucket/out
```
Writes `out/<name>.md` per document, plus `route_stats.csv` (which pages went
Docling vs VLM) and `failures.json`. Knobs: `PARSER_CONCURRENCY`,
`PARSER_RENDER_DPI` (default 144), `PARSER_MAX_PAGES`, `--timeout-per-file`,
`--budget-usd` (see [Cost accounting & budget cap](#cost-accounting--budget-cap)).
(`--retry-on-throttle` is **deprecated / a no-op** — retry now lives inside the
escalation clients, above.)
(`--emit-test-json` additionally writes the benchmark JSON wrapper — eval only.)

### Programmatic
```python
from pathlib import Path
from parser_service.markdown_pipeline import parse_to_markdown

result = parse_to_markdown(Path("report.pdf"))
result["markdown"]      # the RAG-ready Markdown string
result["page_routes"]   # [{page_index, route: docling-kept|vlm|vlm-fallback-docling|textract|textract-fallback-docling|vlm-rejected-kept-docling|textract-rejected-kept-docling, reason, n_chars}]
result["warnings"]      # never raises — failures surface here
result["confidence"]    # {document: float, pages: [{page_index, confidence}, ...]} — advisory only
result["call_counts"]   # {vlm: int, textract: int} — this invocation's successful escalation calls
```

Each `page_routes` record also carries an additive `n_chars` key — the length of
the Markdown string shipped for that page — which is the sole weight used by the
document confidence.

Two guarantees: a document **never crashes the run** (failures land in
`warnings`/`failures.json`), and **Bedrock is contacted only when a page
escalates** — so HTML/Office and clean digital PDFs run fully offline.

### Confidence score (advisory, non-gating)

`parse_to_markdown` returns a `confidence` block — a 0-1 `document` score plus a
per-page map `pages: [{page_index, confidence}]` — derived entirely from the
routing outcomes doc-parser already computes. It is meant for a downstream RAG
consumer to threshold low-confidence documents for human review without
reverse-engineering the route vocabulary.

**It is advisory only.** The score **NEVER** gates parsing, routing, the quality
gate, the promote/keep decision, or the shipped Markdown — it is computed
read-only over `page_routes` after the fact. It is a transparent,
**routing-derived heuristic**, **NOT** a gold-NED/TEDS-calibrated probability of
correctness.

Per-page base score (`src/parser_service/confidence.py`), keyed on the page
`route` and disambiguated by the quality-gate signal booleans already on the
record:

| route                                   | score | meaning                                             |
| --------------------------------------- | ----- | --------------------------------------------------- |
| `docling-kept`                          | 0.95  | gate kept a clean digital Docling page              |
| `vlm` / `textract`, quality signal passes | 0.85  | engine shipped and its text-quality signal passed   |
| `*-rejected-kept-docling`               | 0.70  | promoted, but the clean Docling render shipped      |
| `vlm` / `textract`, signal fails but kept | 0.60  | engine shipped but its text-quality signal failed   |
| `*-fallback-docling`                    | 0.50  | engine errored/emptied; gate-flagged Docling shipped |
| error / empty / unparseable page        | 0.15  | no Docling and no engine output for the page        |

The **document score** is the content-weighted mean
`Σ(page_score × n_chars) / Σ(n_chars)` — one bad half-page image can't tank a long
clean doc. Because the weighting is a convex combination, the document score
stays within `[min_page, max_page]` (it never escapes the tier band its pages
occupy). If `Σ(n_chars) == 0`, or there are no `page_routes` at all, the document
score is `0.0`.

Pages scoring **below the advisory review threshold** surface as additive
`low_confidence_page` warnings (`scope="page"`, the page index, an advisory
message) so low-confidence pages are visible for re-review — again without
changing what shipped. The threshold (`confidence.LOW_CONFIDENCE_THRESHOLD`, `0.65`)
is derived as the midpoint between the highest **untrusted** tier
(engine-failing-kept, `0.60`) and the lowest **trusted** tier
(rejected-kept-docling, `0.70`), so it flags exactly the tiers whose shipped output
the pipeline does not trust — engine-failing-kept (`0.60`), `*-fallback-docling`
(`0.50`), and error/empty (`0.15`) — and never coincides with a tier value.

The score is **not** written to `route_stats.csv`: the CSV schema
(`route_stats.FIELDNAMES`) is a fixed positional contract, so no column is added.
Confidence lives only in the `parse_to_markdown` return dict; a CSV column may be
a follow-up.

## Cost accounting & budget cap

### Per-invocation call counts

`parse_to_markdown` returns an additive `call_counts` key
`{"vlm": int, "textract": int}` — the number of successful escalation-engine
calls **this invocation** made. The two counters (`vlm_client` / `textract_client`)
are backed by `threading.local()`, so under the batch's default concurrency each
file's counts are **race-free per worker thread**: a concurrent neighbor's reset
at the start of its own parse cannot corrupt another thread's count. The public
`get_*_call_count` / `reset_*_call_count` API is unchanged; single-threaded callers
are unaffected.

### Both-engine cost estimate

`scripts/parse_batch.py` sums the per-file `call_counts` and reports a both-engine
cost in the run summary (previously a Textract-only batch reported `$0`):

- **Bedrock (VLM) leg:** `total_vlm_calls × _AVG_COST_PER_CALL`, a per-call token
  model using **average** input/output tokens.
- **Textract leg:** `total_textract_calls × TEXTRACT_PRICE_PER_PAGE` (one
  `AnalyzeDocument` LAYOUT+TABLES call per promoted page).

The run summary carries `estimated_cost_usd` (the combined total) plus the
per-engine breakdown `bedrock_cost_usd`, `textract_cost_usd`, and
`total_textract_calls` (alongside `total_vlm_calls`). A **pre-flight**
`preflight_worst_case_usd` = `Σ page_count × per-page cost` is also emitted: because
the engine that fires per page is unknown until the gate runs (most pages stay on
Docling), it is a **worst-case bound** over page counts, not a per-page prediction.

**The number is an ESTIMATE, not billed cost** (`cost_is_estimate: true` in the
summary): the Bedrock leg uses average tokens rather than metered usage. The
Textract per-page price is **PROVISIONAL** — `TEXTRACT_PRICE_PER_PAGE = $0.019/page`
is the starting figure from `docs/escalation-engine-comparison.md`. A live check of
the AWS Textract pricing page surfaced only US West (Oregon) list rates (Tables
$0.015/page; Layout free when used with Tables → LAYOUT+TABLES ≈ $0.015/page in
us-west-2) and **did not display an `ap-southeast-2` (Sydney) breakdown**; Textract
pricing is region-specific. Verifying the live `ap-southeast-2` LAYOUT+TABLES
per-page price and replacing the constant is a pending human/ops step (see the code
comment on the constant).

### `--budget-usd` document-level cap

`--budget-usd <float>` (default: disabled) is an optional spend ceiling enforced
**at the document boundary**. Running spend is accumulated from each completed
file's estimated cost; once it would exceed the ceiling, escalation is halted and
**every subsequently-dispatched file parses via Docling only (no engine calls)** —
the batch never crashes mid-run. The stop is reflected in the run summary via
`budget_exceeded` (bool), `budget_exceeded_at_file_index`, and `running_spend_usd`.

The cap is **coarse**: because it is checked at the document boundary and files run
concurrently, actual spend can overshoot the ceiling by roughly one document's
worth of in-flight escalation (files already dispatched before the trip finish
escalating). Per-page hard capping is **out of scope** — a mid-page shared spend
counter under concurrency would re-introduce the cross-thread race the thread-local
counters removed.

The cost estimate is **not** written to `route_stats.csv`: the CSV schema
(`route_stats.FIELDNAMES`) is a fixed positional contract, so no cost column is
added; the estimate lives only in the run-summary dict / logs (a CSV column may be
a follow-up).

## Architecture

```
doc ─► Docling parse ─► per page ─► quality gate ─┬─ confident ─► Docling page markdown
                                                  └─ not / scanned ─► render page ─► VLM ─► markdown
                                                                       (VLM empty? → keep Docling md)
                                          ─► concatenate pages in order ─► document.md
```

- **Docling** does the bulk of parsing on CPU; each page is serialized with the
  parser-owned `markdown.render_markdown` (chosen over Docling's native
  `export_to_markdown` — it matches the benchmark gold more closely; see
  [the route decision](agent-os/specs/2026-05-30-markdown-first-pipeline/planning/spike-route-decision.md)).
- **Quality gate** ([quality_gate.py](src/parser_service/quality_gate.py)), per page:
  *Layer 1* Docling confidence (POOR/FAIR → escalate); *coverage* (extracted ≪
  text-layer tokens → escalate, catches silent under-extraction); *Layer 2* text
  heuristics (garbled / repeated-char). Numeric-aware so chart/financial pages
  aren't mistaken for garble.
- **VLM** ([vlm_client.py](src/parser_service/vlm_client.py)) — Bedrock Claude,
  for escalated pages, scanned-page fallback, and per-table extraction; an empty
  VLM result falls back to Docling for that page.
- **Format routing:** PDF → per-page gate; images → gated one-page path; DOCX/
  XLSX/HTML → whole-doc Docling export, gate skipped (one logical page).

JSON is produced only by `wrap_md_as_prediction` (a 1-element envelope around the
markdown) and only on the eval path — it's how the markdown is scored, not what
ships.

## Evaluation

Quality is measured with the **doc-bench** wheel (bundles frozen dataset samples,
Docling baselines, fixtures, schema, and the grader).

```bash
uv tool install --force ./doc_bench-0.1.0-py3-none-any.whl   # puts doc-bench* on PATH
scripts/run_benchmark.sh                                     # parse + grade, both engines
```
[run_benchmark.sh](scripts/run_benchmark.sh) parses each bundled doc to markdown,
wraps it as a prediction (`--emit-test-json`), grades it against the wheel's
**bundled gold** via the `doc-bench` CLI (no `--data-dir`), and aggregates per-doc
**NED** (text similarity ↑) + **TEDS** (table structure ↑) with parse latency. See
[EVAL_RUNBOOK.md](EVAL_RUNBOOK.md) and
[docs/escalation-engine-comparison.md](docs/escalation-engine-comparison.md).

On the representative samples, doc-parser **matches the Docling baseline** (the
hybrid's job on clean corpora is to *not regress* Docling while rescuing degraded
pages). The detailed route/gold analysis and ship-gate record live in
[the spec planning docs](agent-os/specs/2026-05-30-markdown-first-pipeline/planning/).

### Escalation engine & VLM model

Two knobs decide how escalated pages are parsed: the **engine**
(`PARSER_ESCALATION_ENGINE=vlm|textract`) and, for the VLM, the **model**
(`BEDROCK_VLM_MODEL`). Only gate-promoted pages differ; `docling-kept` pages are
byte-identical. We benchmarked both — see
[docs/escalation-engine-comparison.md](docs/escalation-engine-comparison.md)
(engine head-to-head + §10 VLM model comparison).

VLM escalation across Claude models, on the two bundled docs that escalate
(NED ↑; Textract shown as a fixed, model-independent reference):

| escalated doc | Sonnet 3.5 | Sonnet 4.6 | Haiku 4.5 | Textract |
|---|--:|--:|--:|--:|
| **ato_bench** `1371-6.1997` (scanned form — target workload) | 0.662 | 0.700 | **0.706** | 0.322 |
| **dp_bench** `…027` (figure-heavy digital page) | 0.360 | 0.286 | 0.205 | **0.887** |

Read together (full analysis in §10):

- **On scanned text/forms — the workload doc-parser targets — the newer VLMs win on
  quality *and* speed.** Sonnet 4.6 (+5.8%) and Haiku 4.5 (+6.7%) beat the current
  default Sonnet 3.5, and **Haiku 4.5 is the fastest VLM** (~47 s/doc quicker
  end-to-end). All three VLMs beat Textract ~2× here.
- **On the figure-heavy page, the newer VLMs score *lower* — but that's a metric
  artifact, not worse extraction.** The gold is verbatim chart labels; the VLM prompt
  asks models to transcribe *and describe* every chart value, and stronger models obey
  more thoroughly (output grows to 151%→290%→407% of gold length) while **word recall
  actually rises** (54%→61%→72%). Sequential edit distance penalizes the extra prose.
  Textract wins there only because plain OCR matches a verbatim-label gold.
- **Default unchanged** (`vlm` + Sonnet 3.5): switching the production model is gated on a
  larger escalating corpus (only 2 docs escalate here). Reproduce with
  [scripts/run_model_compare.sh](scripts/run_model_compare.sh).

> **Benchmark caveat (open, on the doc-bench side):** the current DP-Bench/
> OmniDocBench samples are **single-page**, so they don't exercise multi-page
> concatenation (covered by the test suite instead); and the gold's table
> rendering is still evolving — see
> [`DOC_BENCH_GOLD_ISSUE.md`](DOC_BENCH_GOLD_ISSUE.md).

## Development

```bash
uv run pytest -q          # test suite (mocked VLM; no Bedrock)
uv run ruff check src/    # lint
```

Tests mock the VLM and need no AWS. Benchmark runs (`run_benchmark.sh`) need Bedrock +
the doc-bench wheel.
