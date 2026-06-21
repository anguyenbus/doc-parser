#!/usr/bin/env bash
# Benchmark on the doc-bench wheel's bundled stratified set (5 dp_bench /
# 5 omnidocbench / 1 ato_bench), both escalation engines.
#
# Requires doc-bench >= the bundled-loader release: the grader scores against
# bundled gold directly — no --data-dir, and no eval_config.yaml (we removed it
# from the repo; if one reappears in CWD it would override bundled gold). We only
# stage the *source* files for the parser.
set -uo pipefail
cd /home/admin/projects/doc-parser

export AWS_REGION=ap-southeast-2
export BEDROCK_VLM_MODEL=anthropic.claude-3-5-sonnet-20241022-v2:0
export PARSER_LOG_LEVEL=INFO

W=eval_runs/bench2
rm -rf "$W"; mkdir -p "$W"
# Stage source files only (grader uses bundled gold; no gold assembly needed).
.venv-docbench/bin/python scripts/stage_wheel_fixtures.py "$W"

for DS in dp_bench omnidocbench ato_bench; do
  IN="$W/$DS/input"
  for ENGINE in vlm textract; do
    PRED="$W/$DS/predictions_$ENGINE"
    rm -rf "$PRED"; mkdir -p "$PRED"
    echo "### PARSE $DS engine=$ENGINE"
    PARSER_ESCALATION_ENGINE="$ENGINE" uv run --quiet python scripts/parse_batch.py \
      --input "$IN" --output "$PRED" --emit-test-json --concurrency 4 \
      > "$W/$DS/parse_${ENGINE}.log" 2>&1

    RES="$W/$DS/results_$ENGINE"
    rm -rf "$RES"; mkdir -p "$RES"
    echo "### GRADE $DS engine=$ENGINE (bundled gold, no --data-dir)"
    doc-bench --dataset "$DS" --predictions "$PRED" --output-dir "$RES" \
      > "$W/$DS/grade_${ENGINE}.log" 2>&1
    echo "### DONE $DS/$ENGINE -> $(grep -h 'Evaluated:' "$W/$DS/grade_${ENGINE}.log" | tail -1)"
  done
done
echo "### AGGREGATE"
uv run --quiet python scripts/aggregate_benchmark.py "$W" > "$W/benchmark_report.md"
echo "### ALL DONE -> $W/benchmark_report.md"
