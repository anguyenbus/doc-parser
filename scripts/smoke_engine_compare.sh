#!/usr/bin/env bash
# Smoke comparison of the two escalation engines (vlm vs textract) on a small
# DP-Bench subset. Validates Task Group 6 wiring end-to-end: dump -> parse (each
# engine) -> grade -> compare, WITHOUT the cost/time of the full benchmark.
set -euo pipefail
cd /home/admin/projects/doc-parser

export AWS_REGION=ap-southeast-2
export BEDROCK_VLM_MODEL=anthropic.claude-3-5-sonnet-20241022-v2:0

LIMIT="${1:-5}"
WORK="eval_runs/smoke"
EXPORTED="$WORK/exported"

echo "### 1) dump $LIMIT dp_bench docs"
rm -rf "$WORK"
mkdir -p "$EXPORTED"
doc-bench-dump-dataset --dataset dp_bench --output "$EXPORTED" --config eval_config.yaml --limit "$LIMIT"

for ENGINE in vlm textract; do
  echo "### 2) parse with PARSER_ESCALATION_ENGINE=$ENGINE"
  PRED="$WORK/predictions_$ENGINE"
  mkdir -p "$PRED"
  PARSER_ESCALATION_ENGINE="$ENGINE" uv run python scripts/parse_batch.py \
    --input "$EXPORTED" --output "$PRED" --emit-test-json

  echo "### 3) grade $ENGINE"
  uv run doc-bench --dataset dp_bench --predictions "$PRED" --output-dir "$WORK/results_$ENGINE"
done

echo "### 4) route distribution per engine (route_stats.csv from page_routes)"
for ENGINE in vlm textract; do
  echo "--- $ENGINE route_stats.csv ---"
  find "$WORK/predictions_$ENGINE" -name 'route_stats.csv' -exec cat {} \; 2>/dev/null || echo "(no route_stats.csv found)"
done

echo "### DONE — results in $WORK/results_{vlm,textract}/"
