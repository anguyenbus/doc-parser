#!/usr/bin/env bash
# Benchmark the production page pipeline (scripts/bench_pages.py) on the doc-bench
# wheel's bundled stratified set (5 dp_bench / 5 omnidocbench / 1 ato_bench), both
# escalation engines. The same datasets, grader and report as run_benchmark.sh,
# which benchmarks the whole-document parse_to_markdown path instead.
#
#   scripts/run_benchmark_pages.sh               # -> eval_runs/bench_pages
#   W=eval_runs/my_run scripts/run_benchmark_pages.sh
set -uo pipefail
cd "$(dirname "$0")/.."

export AWS_REGION=ap-southeast-2
export BEDROCK_VLM_MODEL=${BEDROCK_VLM_MODEL:-au.anthropic.claude-sonnet-4-6}
export PARSER_LOG_LEVEL=INFO

W=${W:-eval_runs/bench_pages}
rm -rf "$W"; mkdir -p "$W"
# The doc-bench CLI is a uv tool; its own interpreter imports doc_bench. The wheel
# ships exactly the manifest's source files next to their .json gold, which
# bench_pages.py skips.
DOCBENCH_PY="$(dirname "$(readlink -f "$(command -v doc-bench)")")/python"
FIX=$("$DOCBENCH_PY" -c "import doc_bench, pathlib; print(pathlib.Path(doc_bench.__file__).parent / 'fixtures')")

for DS in dp_bench omnidocbench ato_bench; do
  for ENGINE in vlm textract; do
    PRED="$W/$DS/predictions_$ENGINE"
    mkdir -p "$W/$DS"
    echo "### PARSE $DS engine=$ENGINE (page pipeline)"
    PARSER_ESCALATION_ENGINE="$ENGINE" uv run --quiet python scripts/bench_pages.py \
      --input "$FIX/$DS" --output "$PRED" > "$W/$DS/parse_${ENGINE}.log" 2>&1 \
      || echo "### PARSE FAILED: see $W/$DS/parse_${ENGINE}.log"

    RES="$W/$DS/results_$ENGINE"
    rm -rf "$RES"; mkdir -p "$RES"
    echo "### GRADE $DS engine=$ENGINE"
    doc-bench --dataset "$DS" --predictions "$PRED" --output-dir "$RES" \
      > "$W/$DS/grade_${ENGINE}.log" 2>&1
    echo "### DONE $DS/$ENGINE -> $(grep -h 'Evaluated:' "$W/$DS/grade_${ENGINE}.log" | tail -1)"
  done
done
echo "### AGGREGATE"
uv run --quiet python scripts/aggregate_benchmark.py "$W" > "$W/benchmark_report.md"
echo "### ALL DONE -> $W/benchmark_report.md"
