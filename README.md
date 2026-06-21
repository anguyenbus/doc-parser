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
export BEDROCK_VLM_MODEL=anthropic.claude-3-5-sonnet-20241022-v2:0
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
`PARSER_RENDER_DPI` (default 144), `PARSER_MAX_PAGES`, `--retry-on-throttle`,
`--timeout-per-file`. (`--emit-test-json` additionally writes the benchmark JSON
wrapper — eval only.)

### Programmatic
```python
from pathlib import Path
from parser_service.markdown_pipeline import parse_to_markdown

result = parse_to_markdown(Path("report.pdf"))
result["markdown"]      # the RAG-ready Markdown string
result["page_routes"]   # [{page_index, route: docling-kept|vlm|vlm-fallback-docling|textract|textract-fallback-docling, reason}]
result["warnings"]      # never raises — failures surface here
```

Two guarantees: a document **never crashes the run** (failures land in
`warnings`/`failures.json`), and **Bedrock is contacted only when a page
escalates** — so HTML/Office and clean digital PDFs run fully offline.

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
doc-bench-setup                                              # NLTK data → METEOR
uv run python scripts/run_eval.py --dataset dp_bench         # dump → parse → grade → compare
```
[run_eval.py](scripts/run_eval.py) parses each doc to markdown, wraps it as a
prediction (`--emit-test-json`), grades via the `doc-bench` CLI, and writes a
`*_vs_baseline.{json,md}` report (per-doc deltas + paired stats). Trust **NID**
(text ↑), **BLEU** (↑), **ARD** (reading order ↓), **METEOR** (↑). TEDS/MHS ~0 by
gold design.

On the representative samples, doc-parser **matches the Docling baseline** (the
hybrid's job on clean corpora is to *not regress* Docling while rescuing degraded
pages). The detailed route/gold analysis and ship-gate record live in
[the spec planning docs](agent-os/specs/2026-05-30-markdown-first-pipeline/planning/).

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

Tests mock the VLM and need no AWS. Benchmark runs (`run_eval.py`) need Bedrock +
the doc-bench wheel.
