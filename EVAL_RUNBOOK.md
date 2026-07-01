# Evaluating doc-parser with doc-bench — Runbook

How to measure doc-parser against the **doc-bench** bundled stratified set
(**5 dp_bench / 5 omnidocbench / 1 ato_bench**) using the pip-installable wheel,
and against your **own** documents via `--data-dir`. Metrics are **NED + TEDS**.

> **As of the bundled-loader release (`doc_bench-0.1.0.tar.gz`, 2026-06-16):** the grader
> reads its own bundled gold directly — **no `eval_config.yaml`, no `--data-dir`, no
> staging/assembly** for the bundled set. `eval_config.yaml` has been **removed** from the repo;
> the legacy `references/` flow (`run_eval.py`, `dump-dataset`, `compare_to_baseline.py`) has
> been removed. For results and analysis see [docs/escalation-engine-comparison.md](docs/escalation-engine-comparison.md).

## Mental model

- **doc-bench (wheel) = the measuring instrument.** It bundles the stratified dataset
  (sources + gold + `manifest.json`) and the NED/TEDS grader. We never run our parser inside it.
- **Host = the parser.** `parser_service` runs on the host (Docling + the escalation engine,
  `vlm` or `textract`) and emits one prediction `<doc_id>.json` per document.
- **They meet via file-based grading.** `doc-bench --dataset X --predictions DIR` grades our
  predictions against the bundled gold, keyed by real `doc_id`.

```mermaid
flowchart LR
    subgraph wheel["doc-bench wheel (instrument)"]
        gold[(bundled gold<br/>5 dp / 5 omni / 1 ato)]
        G[doc-bench grade<br/>NED + TEDS]
    end
    subgraph host["host (parser)"]
        S[stage source files] --> P[parse_batch.py<br/>Docling + vlm|textract]
    end
    P -->|predictions/&lt;doc_id&gt;.json| G
    gold --> G
    G -->|results/*.csv| A[aggregate_benchmark.py]
    A --> R([NED/TEDS + latency report])
```

---

## Step 0 — Install (once, and after each new wheel)

```bash
cd /home/admin/projects/doc-parser
uv tool install --force ./doc_bench-0.1.0.tar.gz                                   # doc-bench* onto PATH
uv pip install --python .venv-docbench/bin/python --reinstall --no-deps ./doc_bench-0.1.0.tar.gz
```

The CLI install (`uv tool`) is isolated and on PATH; the `.venv-docbench` copy is used by the
harness scripts. Confirm + export Bedrock/Textract env (same region for both):

```bash
doc-bench-list-datasets        # dp_bench / omnidocbench / ato_bench / …
export AWS_REGION=ap-southeast-2
export BEDROCK_VLM_MODEL=au.anthropic.claude-sonnet-4-6
```

---

## Step 1 — One-command benchmark (recommended)

```bash
scripts/run_benchmark.sh
```

Does, for all three bundled datasets × both engines (`vlm`, `textract`):
**stage source files → parse → grade (bundled gold) → aggregate**, writing
`eval_runs/bench2/benchmark_report.md` (per-doc NED/TEDS + route + latency, both engines).
The manual steps below are only for running a stage by hand.

---

## Manual steps (what run_benchmark.sh does)

### 1. Locate the bundled source files (no staging needed)
```bash
FIX=$(.venv-docbench/bin/python -c "import doc_bench, pathlib; print(pathlib.Path(doc_bench.__file__).parent / 'fixtures')")
# $FIX/<dataset> holds the source files (the installed wheel ships exactly the
# manifest's 5/5/1 docs; the sibling .json gold is ignored by parse_batch).
```
The grader uses **bundled gold**, so the parser reads sources straight from `$FIX/<dataset>`.

### 2. Parse on the host (pick the engine)
```bash
PARSER_ESCALATION_ENGINE=textract uv run python scripts/parse_batch.py \
  --input "$FIX/dp_bench" \
  --output eval_runs/bench2/dp_bench/predictions_textract \
  --emit-test-json --concurrency 4
# engine ∈ vlm (default) | textract ; writes <doc_id>.json + route_stats.csv + failures.json
```

### 3. Grade against bundled gold (no config, no --data-dir)
```bash
doc-bench --dataset dp_bench \
  --predictions eval_runs/bench2/dp_bench/predictions_textract \
  --output-dir   eval_runs/bench2/dp_bench/results_textract
```
Prints `Evaluated: N`, `Rejected: 0`, and `ned_similarity` / `teds` averages.

> ⚠️ **CWD gotcha:** if an `eval_config.yaml` is present in the grader's CWD, it *overrides*
> bundled gold with whatever paths it lists. We deleted ours; if one reappears, run the grader
> from a directory without it (the harness runs it from `eval_runs/bench2/`).

### 4. Aggregate (NED/TEDS + route + latency, both engines)
```bash
uv run python scripts/aggregate_benchmark.py eval_runs/bench2   # → benchmark_report.md
```

---

## Custom datasets (`--data-dir`)

Evaluate on **your own** documents — any set with ground truth. `--data-dir` is the *only* case
that needs a gold file you provide (and it always wins over any config).

DP-Bench shape:
```
my_data/
  reference.json            # { "<file>.pdf": { "elements": [ {category, page, content:{text}}, ... ] } }
  pdfs/<file>.pdf
```
OmniDocBench shape:
```
my_data/
  OmniDocBench.json         # list of pages with layout_dets + page_info.image_path
  images/<file>.png|jpg
```

```bash
PARSER_ESCALATION_ENGINE=textract uv run python scripts/parse_batch.py \
  --input my_data/pdfs --output my_preds --emit-test-json --concurrency 4
doc-bench --dataset dp_bench --data-dir my_data --predictions my_preds --output-dir my_results
# OmniDocBench: --dataset omnidocbench --data-dir my_data
```
`doc_id` is the source-file stem; predictions must be `<doc_id>.json`. (This is exactly how the
§7 20-page OmniDocBench probe in the comparison doc was run.)

---

## Escalation engine & latency

- **Engine switch:** `PARSER_ESCALATION_ENGINE=vlm` (default, Bedrock Claude) or `textract`
  (AWS `AnalyzeDocument`, `LAYOUT+TABLES`). Only gate-promoted pages differ between engines;
  `docling-kept` pages are byte-identical.
- **Latency:** run the parser with `PARSER_LOG_LEVEL=INFO`; each `file_parsed` JSON log line
  carries `parse_duration_s` (and `vlm_routed_pages`). The aggregator joins these per doc.
  Measured at `--concurrency 4`, so Docling times include CPU contention.

## How to read the scorecard

- **NED** (`ned_similarity`, ↑) = normalized edit-distance text similarity. **TEDS** (↑) =
  table-structure similarity; **0 when the gold has no scored table** — expected, not a failure.
- NID/BLEU/METEOR/ARD are **retired** — do not compare to older reports that quoted them.
- The bundled `*_results.json` Docling baselines are **stale** (disagree with the current
  grader); treat them as rough context only. See [docs/doc-bench-feedback.md](docs/doc-bench-feedback.md).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `doc-bench: command not found` | `uv tool install --force ./doc_bench-0.1.0.tar.gz` (ensure `~/.local/bin` on PATH). |
| Wrong dataset size / all `MISSING_PREDICTION` | A stale `eval_config.yaml` in CWD is overriding bundled gold (points at `references/`). Remove it or grade from a clean dir. |
| `Evaluated: 0` | Predictions not named `<doc_id>.json`, or (for `--data-dir`) gold missing for those docs. |
| CSV has no `ned` column | It's `ned_similarity` now (the aggregator reads both). |
| All pages `vlm-failed` / textract errors | Bedrock/Textract unreachable — check the IAM instance role + `AWS_REGION=ap-southeast-2`. |
