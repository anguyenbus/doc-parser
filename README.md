# doc-parser

Document parsing service: a **Docling + AWS Bedrock Claude (Sonnet 3.5) hybrid**
pipeline. Docling parses every page on the host; a two-layer quality gate
escalates only low-confidence pages to the VLM. Output is one prediction JSON
per document, conforming to the doc-bench `parser_output` contract.

## Architecture

```
PDF / image ──► Docling parse ──► quality gate ──┬─ pass ─► keep Docling elements
                                                 └─ fail ─► render page ─► VLM (Claude) ─► elements
                                                            (tables: crop region ─► VLM table mode)
```

- **Docling** does the bulk of parsing (layout, text, tables) on CPU.
- **Quality gate** ([quality_gate.py](src/parser_service/quality_gate.py)) decides per page:
  - *Layer 1* — Docling's own confidence (POOR/FAIR → promote).
  - *Layer 2* — text heuristics (garbled-token ratio, **content-token ratio**
    that counts clean numbers, repeated-char runs, word length, printable ratio).
- **VLM** ([vlm_client.py](src/parser_service/vlm_client.py)) — Bedrock Claude,
  used for promoted pages, scanned-page fallback, and per-table extraction.

The VLM client is **Bedrock-only**. Export before running:

```bash
export AWS_REGION=ap-southeast-2
export BEDROCK_VLM_MODEL=anthropic.claude-3-5-sonnet-20241022-v2:0
```

## Running the parser

```bash
# one document
uv run python scripts/parse_one.py --input doc.pdf --output predictions/

# a directory, concurrently (writes route_stats.csv + failures.json)
uv run python scripts/parse_batch.py --input ./inputs --output ./predictions --concurrency 4
```

Supported inputs: PDF, PNG, JPG/JPEG, TIFF, WebP.

## Evaluation

Quality is measured with the **doc-bench** package (a pip-installable wheel that
bundles frozen dataset samples, Docling baselines, fixtures, and the grader).
The wheel reproduces the old Docker grader's scores **and** computes METEOR.

### Install the grader (once)

```bash
uv tool install --force ./doc_bench-0.1.0-py3-none-any.whl   # puts doc-bench* on PATH
doc-bench-setup                                              # NLTK data → METEOR works
```

### Fast offline gate (smoke test)

```bash
doc-bench-smoke-test                          # bundled fixtures (~22 docs); exit 0 = pass
doc-bench-smoke-test --predictions ./preds    # validate OUR predictions (schema + rejection rate)
```
Catches breakage/schema issues in seconds. Not a quality benchmark.

### Full evaluation (one command)

```bash
uv run python scripts/run_eval.py                    # both datasets: dump → parse → grade → compare
uv run python scripts/run_eval.py --dataset dp_bench # one dataset
uv run python scripts/run_eval.py --skip-parse       # reuse existing predictions
```
[run_eval.py](scripts/run_eval.py) orchestrates the four steps and shells out to
the installed `doc-bench` CLI (the grading source of truth); it writes a
`*_vs_baseline.{json,md}` report (per-doc deltas, aggregate, paired stats) per
dataset.

> Trust **NID** (text similarity ↑), **BLEU** (↑), **ARD** (reading order ↓),
> **METEOR** (↑, now functional via the wheel). TEDS/MHS are ~0 by gold design.

### Results vs. the Docling baseline (2026-05-30)

| Dataset | | NID ↑ | BLEU ↑ | ARD ↓ | METEOR ↑ |
|---|---|---|---|---|---|
| **DP-Bench** (12) | doc-parser | 0.9598 | 0.8842 | 0.5874 | 0.9454 |
| | baseline | 0.9593 | 0.8768 | 0.5883 | 0.9475 |
| **OmniDocBench** (10) | doc-parser | 0.8181 | 0.4635 | 0.3175 | 0.6756 |
| | baseline | 0.8230 | 0.4617 | 0.3171 | 0.6501 |

doc-parser **matches the Docling baseline on both** (no statistically significant
differences). On these clean digital corpora Docling already performs well, so
the hybrid's job is to *not regress* it while reserving the VLM for genuinely
degraded pages.

### Timing (10–12 docs, `--concurrency 4`, CPU-only)

Cold start (model load) ~30–60 s; ~10–15 s/page amortized; **~2–3 min total**.
Docling-only pages are cheap (~1–3 s); VLM-escalated pages dominate the tail
(~5–15 s each for the Bedrock round-trip).

## VLM vs. Docling: investigation & fixes

On DP-Bench the hybrid initially trailed Docling (BLEU 0.809 vs 0.877). A
three-way (gold / Docling / VLM) investigation traced the gap to **three causes,
only one a genuine VLM weakness** — all addressed:

1. **Converter dropped list text.** The VLM emitted a `list` container, but the
   grader's markdown converter only renders `list_item`. → `parser_service` now
   expands a VLM `list` into per-item `list_item` elements (`_emit_vlm_elements`).
2. **Number-dense pages falsely escalated.** The gate's `dict_hit_rate` counted
   only alphabetic tokens, so chart/financial pages looked like garble and went
   to the VLM (which scored *worse* than Docling's verbatim OCR). → it's now
   **numeric-aware** (clean numbers/dates/% count as content).
3. **VLM under-transcribes chart axis labels** (genuine, residual). Mitigated by
   (2): such pages now stay on Docling.

Plus prompt hardening (verbatim/completeness rules) and an image `media_type`
sniffing fix. Net DP-Bench BLEU: **0.809 → 0.838 → 0.856 → 0.884** (original →
prompt → list fix → gate fix), closing the gap to baseline.

## Development

```bash
uv run pytest tests/ -q          # test suite
uv run ruff check src/           # lint
```

> 3 tests in `tests/test_image.py` (image-path warning assertions) are
> pre-existing failures unrelated to the parsing pipeline.

## Notes

- `eval_config.yaml` points both datasets at the data shipped in the doc-bench
  repo under `references/doc-bench/baseline/`. The `doc-bench` grader reads this
  fixed filename from the CWD (no `--config` flag; `dump-dataset` still takes one).
- Docker (`references/doc-bench`) remains available for sealed CI runs; the wheel
  is the recommended path for local iteration (identical scores, plus METEOR).
