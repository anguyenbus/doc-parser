#!/usr/bin/env bash
# Compare VLM escalation across the current Claude models (Sonnet 4.6 /
# Haiku 4.5), plus Textract as a fixed (model-independent) reference.
# (Sonnet 3.5 retired from this comparison — 4.6 is the current baseline.)
#
# Only gate-promoted pages differ between models, and in the bundled set only
# dp_bench (…027) and ato_bench (1371-6.1997) escalate — omnidocbench escalates
# 0/5, so it is model-independent and skipped here (see escalation doc §7).
set -uo pipefail
cd /home/admin/projects/doc-parser

export AWS_REGION=ap-southeast-2
export PARSER_LOG_LEVEL=INFO

W=eval_runs/model_compare
rm -rf "$W"; mkdir -p "$W"
FIX=$(.venv-docbench/bin/python -c "import doc_bench, pathlib; print(pathlib.Path(doc_bench.__file__).parent / 'fixtures')")

# model_key=bedrock_model_id
MODELS=(
  "sonnet-4-6=au.anthropic.claude-sonnet-4-6"
  "haiku-4-5=au.anthropic.claude-haiku-4-5-20251001-v1:0"
)
DATASETS=(dp_bench ato_bench)

run_one() {  # $1=dataset $2=label $3=engine $4=model_id(optional)
  local ds="$1" label="$2" engine="$3" model="${4:-}"
  local pred="$W/$ds/predictions_$label" res="$W/$ds/results_$label"
  rm -rf "$pred" "$res"; mkdir -p "$pred" "$res"
  echo "### PARSE $ds label=$label engine=$engine model=${model:-n/a}"
  env PARSER_ESCALATION_ENGINE="$engine" ${model:+BEDROCK_VLM_MODEL="$model"} \
    uv run --quiet python scripts/parse_batch.py \
      --input "$FIX/$ds" --output "$pred" --emit-test-json --concurrency 4 \
      > "$W/$ds/parse_${label}.log" 2>&1
  echo "### GRADE $ds label=$label"
  doc-bench --dataset "$ds" --predictions "$pred" --output-dir "$res" \
    > "$W/$ds/grade_${label}.log" 2>&1
  echo "### DONE $ds/$label -> $(grep -h 'Evaluated:' "$W/$ds/grade_${label}.log" | tail -1)"
}

for ds in "${DATASETS[@]}"; do
  mkdir -p "$W/$ds"
  for entry in "${MODELS[@]}"; do
    run_one "$ds" "vlm_${entry%%=*}" vlm "${entry#*=}"
  done
  run_one "$ds" "textract" textract
done

echo "### AGGREGATE"
uv run --quiet python scripts/aggregate_model_compare.py "$W" > "$W/model_compare_report.md"
echo "### ALL DONE -> $W/model_compare_report.md"
