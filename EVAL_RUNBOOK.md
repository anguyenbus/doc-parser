# Evaluating doc-parser with doc-bench (wheel) — Runbook

How to measure doc-parser against the **OmniDocBench** (10 pages) and
**DP-Bench** (12 PDFs) samples using the pip-installable **doc-bench wheel**.
The wheel reproduces the old Docker grader's scores, ships the datasets +
baselines + fixtures + schema, and computes METEOR. Docker remains an option for
sealed CI runs (see "Docker fallback").

## Mental model

- **doc-bench (wheel) = the measuring instrument.** It bundles hash-pinned
  dataset samples, precomputed **Docling baselines**, smoke-test fixtures, and
  the grader. We never run our parser inside it.
- **Host = the parser.** `parser_service` runs on the host (Docling + Bedrock)
  and emits one prediction JSON per document.
- **They meet via file-based grading.** `doc-bench` grades our `<doc_id>.json`
  predictions against the frozen gold.

```
doc-bench-dump-dataset ─► exported/<doc_id>.{png,jpg,pdf}
parser_service (host)  ─► predictions/<doc_id>.json
doc-bench (grade)      ─► results/*.csv
compare_to_baseline    ─► *_vs_baseline.{json,md}
```

Dataset data ships in this repo under `references/doc-bench/baseline/{omnidocbench,dp_bench}`
and `eval_config.yaml` points at it (the grader reads this fixed filename from
the CWD — no `--config` flag).

---

## Step 0 — Install (once)

```bash
cd /home/admin/projects/doc-parser
uv tool install --force ./doc_bench-0.1.0-py3-none-any.whl   # doc-bench* onto PATH
doc-bench-setup                                             # NLTK data (wordnet/punkt/omw-1.4) → METEOR
```

`uv tool install` is the right way to install a CLI tool — isolated env, on PATH,
and unaffected by `uv run` syncing the project venv. Update with the same command
(`--force`) when the team ships a new wheel.

Confirm:
```bash
doc-bench-list-datasets        # lists dp_bench / omnidocbench / …
doc-bench-smoke-test           # ~22 bundled fixtures → PASS, exit 0
```

Export Bedrock env for the parse step:
```bash
export AWS_REGION=ap-southeast-2
export BEDROCK_VLM_MODEL=anthropic.claude-3-5-sonnet-20241022-v2:0
```

---

## Step 1 — One-command evaluation (recommended)

```bash
uv run python scripts/run_eval.py                     # both datasets
uv run python scripts/run_eval.py --dataset dp_bench  # one dataset
uv run python scripts/run_eval.py --skip-parse        # reuse existing predictions
```

`run_eval.py` does **dump → parse → grade → compare** per dataset (work goes to
`docker_eval/run/<dataset>/`), prints an aggregate summary, and writes a
`*_vs_baseline.{json,md}` report next to each results CSV. Done — the manual
steps below are only if you want to run a stage by hand.

---

## Manual steps (equivalent to what run_eval does)

### 1. Export source files
```bash
doc-bench-dump-dataset --dataset dp_bench \
  --output docker_eval/dpb/exported --config eval_config.yaml
# --dataset omnidocbench  (and --limit N for a subset)
```
Each filename stem **is** the `doc_id` the grader joins on.

### 2. Parse on the host
```bash
uv run python scripts/parse_batch.py \
  --input docker_eval/dpb/exported --output docker_eval/dpb/predictions --concurrency 4
```
Writes `<doc_id>.json` per doc, plus `route_stats.csv` + `failures.json`.

### 3. Grade with the wheel
```bash
# Run from the repo root: the grader reads ./eval_config.yaml (no --config flag).
doc-bench --dataset dp_bench \
  --predictions docker_eval/dpb/predictions \
  --output-dir docker_eval/dpb/results
```
Prints `Evaluated: N`, `Rejected: 0`, and metric averages. (No CWD `contracts/`
needed — the wheel resolves its bundled schema. OmniDocBench data may be flat or
under `images/`; the wheel handles both. The grader is predictions-only — the
in-process `--parser {stub,fast,docling}` was removed from the wheel.)

### 4. Compare to baseline (with significance)
```bash
uv run python scripts/compare_to_baseline.py \
  --results docker_eval/dpb/results/dp_bench_predictions_results_<ts>.csv \
  --baseline references/doc-bench/baseline/dp_bench/dpbench_results.json \
  --route-stats docker_eval/dpb/predictions/route_stats.csv
# OmniDocBench baseline: references/doc-bench/baseline/omnidocbench/omnidocbench_results.json
```

---

## Custom datasets (`--data-dir`)

Evaluate on **your own** documents — any set with ground truth, no code changes.
`--data-dir` overrides `eval_config.yaml` and points the grader at your data.

### 1. Lay out your data

DP-Bench shape:
```
my_data/
  reference.json            # { "<file>.pdf": { "elements": [ ... ] } }
  pdfs/<file>.pdf
```
OmniDocBench shape:
```
my_data/
  OmniDocBench.json         # list of pages with layout_dets + page_info.image_path
  images/<file>.png|jpg
```

`reference.json` ground-truth element (DP-Bench):
```json
{
  "my_report.pdf": {
    "elements": [
      {"category": "Header",    "page": 1, "content": {"text": "Quarterly Report"},
       "coordinates": [{"x": 0, "y": 0}, ...]},
      {"category": "Paragraph", "page": 1, "content": {"text": "Revenue grew ..."}}
    ]
  }
}
```
`category` ∈ Header / Paragraph / Table / List / Figure / … ; `content.text` is the
gold text used for scoring. The `doc_id` is the filename stem (`my_report`).

### 2. Parse with doc-parser
```bash
uv run python scripts/parse_batch.py \
  --input my_data/pdfs --output my_preds --concurrency 4   # → my_preds/<doc_id>.json
```

### 3. Grade against your ground truth
```bash
doc-bench --dataset dp_bench \
  --data-dir my_data \
  --predictions my_preds \
  --output-dir my_results
# OmniDocBench: --dataset omnidocbench --data-dir my_data
```
Prints `Evaluated: N`. To compare against a baseline of your own, pass a
`*_results.json` in the same shape to `scripts/compare_to_baseline.py --baseline`.

> **Verified:** a fabricated 2-doc DP-Bench set (`mydoc_a.pdf`, `mydoc_b.pdf`)
> graded cleanly via `--data-dir` — Evaluated 2, 0 rejected — and reproduced the
> per-doc scores of the originals it was seeded from.

**Requirements:** every doc must have ground truth in `reference.json` /
`OmniDocBench.json`, and predictions must be named `<doc_id>.json` (doc_id =
source-file stem).

---

## How to read the scorecard

Trust **NID** (text similarity ↑), **BLEU** (↑), **ARD** (reading order ↓), and
**METEOR** (↑ — now functional via the wheel + `doc-bench-setup`). **TEDS/MHS**
are ~0 (the gold has no markdown tables/headings) — expected, not a failure.

> The gold is a verbatim element-text dump, which rewards literal OCR (Docling's
> strength). Most pages stay on Docling, so the sample largely measures Docling;
> the VLM only changes the escalated pages.

### Reference numbers (doc-parser vs Docling baseline, 2026-05-30)

| Dataset | | NID ↑ | BLEU ↑ | ARD ↓ | METEOR ↑ |
|---|---|---|---|---|---|
| **DP-Bench** (12) | doc-parser | 0.9598 | 0.8842 | 0.5874 | 0.9454 |
| | baseline | 0.9593 | 0.8768 | 0.5883 | 0.9475 |
| **OmniDocBench** (10) | doc-parser | 0.8181 | 0.4635 | 0.3175 | 0.6756 |
| | baseline | 0.8230 | 0.4617 | 0.3171 | 0.6501 |

doc-parser **matches the Docling baseline on both** (differences within noise).
Validation: forcing Docling-only on all docs reproduces each baseline to ~3
decimals.

---

## Docker fallback (sealed CI)

The Docker image still works for reproducible CI. Build from
`references/doc-bench` (`sudo docker build -t doc-bench:latest .`) and grade via
`docker run … doc-bench --dataset X --predictions /work/predictions …`. The wheel
is preferred for local iteration (same scores, plus METEOR, no container).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `doc-bench: command not found` | `uv tool install --force ./doc_bench-*.whl` (ensure `~/.local/bin` on PATH). |
| `Evaluated: 0` | Predictions don't match dataset `doc_id`s (must be `<stem>.json`), or `eval_config.yaml`/`--data-dir` points at the wrong data. Re-dump + re-parse. |
| METEOR = 0 | Run `doc-bench-setup`. |
| All pages `vlm-failed` | Bedrock unreachable — check instance role + the two env vars. |
