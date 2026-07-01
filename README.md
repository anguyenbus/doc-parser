# doc-parser

Turn any document — **PDF, image, DOCX, XLSX, or HTML — into one clean Markdown file**
a RAG pipeline can consume.

doc-parser is **Docling-first with an escalation fallback**: Docling parses every page
on CPU, and a per-page quality gate sends only the pages it struggles with (scanned,
garbled, under-extracted) to a stronger engine — **AWS Bedrock Claude (VLM)** or **AWS
Textract**. Most pages stay on Docling (fast, cheap); the engine is the safety net.

- **Input:** `.pdf`, `.png/.jpg/.jpeg/.tif/.tiff`, `.docx`, `.xlsx/.xlsm`, `.html/.htm`
- **Output:** one `.md` per document. (A schema-JSON exists only as a benchmark wrapper.)
- A run **never crashes** — failures land in `warnings` / `failures.json`.
- An engine is contacted **only when a page escalates** — so HTML/Office and clean
  digital PDFs run **fully offline**.

Module map & data flow: [docs/architecture.md](docs/architecture.md). Guided notebook:
[notebooks/walkthrough.ipynb](notebooks/walkthrough.ipynb).

## Quickstart

```bash
uv sync                                                   # install deps

# Only needed if a page may escalate (promoted PDFs, images, scans).
export AWS_REGION=ap-southeast-2
export BEDROCK_VLM_MODEL=au.anthropic.claude-sonnet-4-6   # latest Sonnet
```

## Usage

```bash
# one document → markdown (stdout, or --output out/)
uv run python scripts/parse_one.py --input report.pdf --format md

# a folder or S3 prefix → one .md each
uv run python scripts/parse_batch.py --input ./inbox --output ./out --concurrency 4
uv run python scripts/parse_batch.py --input s3://bucket/in --output s3://bucket/out --budget-usd 5.00
```

`--format`: `md` · `json` (legacy element JSON, current default) · `both`. Batch also
writes `route_stats.csv` + `failures.json` and prints a cost summary. Knobs:
`PARSER_CONCURRENCY`, `PARSER_RENDER_DPI` (144), `PARSER_MAX_PAGES`, `--timeout-per-file`,
`--budget-usd`.

```python
from parser_service.markdown_pipeline import parse_to_markdown

result = parse_to_markdown(Path("report.pdf"))
result["markdown"]      # the RAG-ready Markdown string (the product)
result["page_routes"]   # {page_index, route, reason, n_chars} per page — provenance
result["confidence"]    # {"document": float, "pages": [...]} — advisory only
result["call_counts"]   # {"vlm": int, "textract": int} — escalation calls this run
result["warnings"]      # never raises — failures surface here
```

## How it works

```
doc ─► Docling parse ─► per page ─► quality gate ─┬─ confident ─► Docling page markdown
                                                  └─ promote ──► render → engine (VLM│Textract)
                                                                  (empty/garbage → keep Docling)
      ─► concatenate pages in order ─► document.md  (+ page_routes, confidence, call_counts)
```

- **Quality gate** ([quality_gate.py](src/parser_service/quality_gate.py)), per page:
  Docling confidence (POOR/FAIR → escalate), coverage (under-extraction → escalate),
  and text heuristics (garble/repeat). Numeric-aware so financial pages aren't flagged.
- **Format routing:** PDF → per-page gate; image → gated one-page path; DOCX/XLSX/HTML →
  whole-doc export, gate skipped.

### Route vocabulary (`page_routes[].route`)

| route | meaning |
| --- | --- |
| `docling-kept` | gate kept Docling's page (no engine call) |
| `vlm` / `textract` | engine markdown replaced the page |
| `vlm-fallback-docling` / `textract-fallback-docling` | engine errored/emptied → Docling shipped |
| `vlm-rejected-kept-docling` / `textract-rejected-kept-docling` | arbitration kept clean Docling over a garbled engine page |

`reason` carries the gate's promote reason, or a marker: `scan_fastpath` / `throttled`.

## Escalation engine

`PARSER_ESCALATION_ENGINE` (default `vlm`) picks which engine parses promoted pages;
both emit the same element-JSON, so Docling handling and fallback are identical.

```bash
export PARSER_ESCALATION_ENGINE=vlm       # (default) Bedrock Claude Sonnet
export PARSER_ESCALATION_ENGINE=textract  # Textract synchronous AnalyzeDocument (LAYOUT+TABLES)
```

`textract` sends rendered page bytes directly (no S3), one call per promoted page,
reusing `AWS_REGION` and the instance-role credentials.

## Optional toggles (default OFF; byte-identical to baseline when off)

| toggle | effect |
| --- | --- |
| `PARSER_ESCALATION_ARBITRATION=1` | *Keep-the-better-of:* after a page escalates, if the engine output fails the garble proxy but a clean Docling render exists (and it wasn't a coverage promotion), keep Docling. Records a `*-rejected-kept-docling` route. |
| `PARSER_SCAN_FASTPATH=1` | Skip Docling on documents that are **all** image-only scans (no text layer + embedded image, probed via `pypdf`). Docling would extract nothing there and every page escalates anyway, so `convert()` is skipped and pages route straight to the engine (`reason="scan_fastpath"`). **Forfeits Docling's OCR** — only for batches known to be hopeless-for-Docling scans. All-or-nothing: any text-bearing page keeps the normal path. |

## Confidence score (advisory, non-gating)

`parse_to_markdown` returns a `confidence` block — a 0–1 `document` score plus a per-page
map, derived read-only from routing outcomes. It **never** gates parsing or output; it's
a transparent routing heuristic, **not** a calibrated correctness probability.

Per-page base score, keyed on `route`:

| route | score |
| --- | --- |
| `docling-kept` | 0.95 |
| `vlm`/`textract`, quality signal passes | 0.85 |
| `*-rejected-kept-docling` | 0.70 |
| `vlm`/`textract`, signal fails but kept | 0.60 |
| `*-fallback-docling` | 0.50 |
| error / empty page | 0.15 |

Document score = content-weighted mean `Σ(score × n_chars) / Σ(n_chars)` (a convex
combination — one tiny bad page can't tank a long clean doc). Pages below
`LOW_CONFIDENCE_THRESHOLD` (0.65) surface as additive `low_confidence_page` warnings.

## Cost & budget

Batch sums per-file `call_counts` into an **estimated** cost (`cost_is_estimate: true`):
Bedrock via an average-token model; Textract via `TEXTRACT_PRICE_PER_PAGE` (`$0.019`,
**provisional** — pending live `ap-southeast-2` price verification). `--budget-usd <n>`
caps spend at the **document boundary**: once exceeded, remaining files parse
Docling-only (no engine calls) and the run reports `budget_exceeded`. The cap is coarse
(can overshoot by ~one in-flight document).

**Retry:** both engine clients wrap their AWS call in bounded exponential backoff.
Transient errors (throttling / timeout / 5xx) retry with jitter; permanent errors
(auth / validation / 4xx) fail fast. When retries exhaust on a throttle, the page falls
back to Docling with `reason="throttled"` (visible in `page_routes`; `route_stats`
derives a `throttled_pages` count).

## Evaluation

Quality is measured with the **doc-bench** wheel (frozen datasets, Docling baselines,
grader):

```bash
uv tool install --force ./doc_bench-0.1.0-py3-none-any.whl
scripts/run_benchmark.sh          # parse + grade, both engines
```

It grades markdown against bundled gold via **NED** (text similarity ↑) + **TEDS** (table
structure ↑). On clean corpora doc-parser **matches the Docling baseline** — the hybrid's
job is to not regress Docling while rescuing degraded pages. See
[EVAL_RUNBOOK.md](EVAL_RUNBOOK.md).

Re-benchmark (2026-06-30, Sonnet 4.6) on the two bundled docs that escalate (NED ↑):

| escalated doc | Sonnet 4.6 | Haiku 4.5 | Textract |
|---|--:|--:|--:|
| **ato_bench** (scanned form) | 0.648 | **0.706** | 0.322 |
| **dp_bench** `…027` (figure-heavy) | 0.287 | 0.205 | **0.887** |

**Neither engine dominates** — the VLM wins the scan, Textract wins the figure page — and
only 2 docs escalate in the bundled set, so a per-page routing policy is gated on a
larger escalating corpus. Reproduce with
[scripts/run_model_compare.sh](scripts/run_model_compare.sh).

## Development

```bash
uv run pytest -q          # tests (engines mocked; no AWS)
uv run ruff check src/    # lint
uv run mypy src/          # type-check
```
