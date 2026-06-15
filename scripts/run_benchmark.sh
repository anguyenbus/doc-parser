#!/usr/bin/env bash
# Full local benchmark: both datasets x both escalation engines, capturing per-file
# grader metrics AND per-file parse latency. Produces a combined report via the
# Python aggregator (scripts/aggregate_benchmark.py).
set -euo pipefail
cd /home/admin/projects/doc-parser

export AWS_REGION=ap-southeast-2
export BEDROCK_VLM_MODEL=anthropic.claude-3-5-sonnet-20241022-v2:0

WORK="eval_runs/bench"
rm -rf "$WORK"; mkdir -p "$WORK"

DATASETS=("dp_bench" "omnidocbench")
ENGINES=("vlm" "textract")

for DS in "${DATASETS[@]}"; do
  EXPORTED="$WORK/$DS/exported"
  mkdir -p "$EXPORTED"
  echo "### DUMP $DS"
  doc-bench-dump-dataset --dataset "$DS" --output "$EXPORTED" --config eval_config.yaml

  for ENGINE in "${ENGINES[@]}"; do
    echo "### PARSE $DS engine=$ENGINE"
    PRED="$WORK/$DS/predictions_$ENGINE"
    mkdir -p "$PRED"
    # parse_batch logs one JSON line per file incl. parse_duration_s -> capture for latency.
    PARSER_ESCALATION_ENGINE="$ENGINE" uv run python scripts/parse_batch.py \
      --input "$EXPORTED" --output "$PRED" --emit-test-json \
      > "$WORK/$DS/parse_${ENGINE}.log" 2>&1

    echo "### GRADE $DS engine=$ENGINE"
    uv run doc-bench --dataset "$DS" --predictions "$PRED" \
      --output-dir "$WORK/$DS/results_$ENGINE" >> "$WORK/$DS/grade_${ENGINE}.log" 2>&1
  done
done

echo "### AGGREGATE"
uv run python scripts/aggregate_benchmark.py "$WORK" > "$WORK/benchmark_report.md"
echo "### DONE -> $WORK/benchmark_report.md"
