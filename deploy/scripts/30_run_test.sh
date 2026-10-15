#!/usr/bin/env bash
# 30_run_test.sh <T1|T2|T3|T4> — run one test on the cluster.
#
#   T1 correctness  sample_mixed_10.pdf, escalation on; compared with the local reference.
#   T2 scale        10 renamed copies of the 107-page PDF (1,070 pages).
#   T3 pod loss     5 copies; two page-service pods are deleted mid-run.
#   T4 driver loss  1 copy; its driver pod is deleted mid-file and the Job resumes it.
#   T5 formats      one file per kind (html, docx, pdf with 10 pages, txt), one driver
#                   index each; outputs compared with a local run of the same inputs,
#                   and each unit's parsing pod recorded. Needs PARSER_ENABLED_KINDS to
#                   include txt, docx and html (deploy/test.env).
#
# Inputs are uploaded under $INPUT_PREFIX/<test>/ with a list.txt naming them; the
# driver Job has one index per PDF. The store keys work/ by content
# (work/<sha256>/<config hash>/), so every input is a stamped copy with its own
# /TestCopy metadata entry: the pages are the same, but each copy has its own work
# id and is really parsed. The same name in the same test run gives the same bytes;
# a new name (T3_NAME) or test run gives new ones. ESCALATE=on|off (default on: the 107-page PDF
# has 3 gate-promoted pages, which end degraded, and the file not complete,
# without escalation). Start the sampler first (40_capture_evidence.sh start).
source "$(dirname "$0")/lib.sh"

TEST=${1:?usage: 30_run_test.sh T1|T2|T3|T4|T5}
BIG_PDF=${BIG_PDF:-$REPO_ROOT/247136389-5MB.pdf}  # the 107-page PDF for T2-T4 (not in git)
MIXED_PDF=$REPO_ROOT/tests/fixtures/sample_mixed_10.pdf
REFERENCE_OUTPUT=s3://de-genai-dev/parser/out/sample_mixed_10-50684cab919fc3b1/output.md
OUT=$EVIDENCE/$TEST${OUT_SUFFIX:-}
mkdir -p "$OUT"

# stamp_copy <src.pdf> <dst.pdf> <tag>: the same pages, distinct bytes per tag.
stamp_copy() {
  uv run --project "$REPO_ROOT" python - "$1" "$2" "$3" <<'PY'
import sys
from pypdf import PdfReader, PdfWriter
src, dst, tag = sys.argv[1:]
# An incremental update: the original bytes stay as they are, and only the new
# metadata is appended, so a copy is the same size and splits into the same pages.
writer = PdfWriter(src, incremental=True)
writer.add_metadata({"/TestCopy": tag})
with open(dst, "wb") as f:
    writer.write(f)
PY
}

upload_copies() {  # source-pdf test-dir name-prefix count
  local src=$1 dir=$2 prefix=$3 count=$4 list="" tmp copy
  tmp=$(mktemp -d)
  : > "$OUT/.sha_$prefix"
  for i in $(seq -w 1 "$count"); do
    copy=$tmp/$prefix-$i.pdf
    stamp_copy "$src" "$copy" "$TEST_RUN_ID/$dir/$prefix-$i"
    sha256sum "$copy" | awk '{print $1}' >> "$OUT/.sha_$prefix"
    aws s3 cp --only-show-errors "$copy" "$INPUT_PREFIX/$dir/$prefix-$i.pdf"
    list+="$INPUT_PREFIX/$dir/$prefix-$i.pdf"$'\n'
  done
  rm -rf "$tmp"
  printf '%s' "$list" | aws s3 cp --only-show-errors - "$INPUT_PREFIX/$dir/list.txt"
}

start_job() {  # job-name completions escalate
  export JOB_NAME=$1 JOB_COMPLETIONS=$2 INPUT_LIST=$INPUT_PREFIX/$TEST_DIR/list.txt
  export DRIVER_EXTRA_ARGS=""
  [[ $3 == off ]] && DRIVER_EXTRA_ARGS="- --no-escalate"
  render "$REPO_ROOT/deploy/k8s/driver-job.yaml.tpl" > "$OUT/driver-job.yaml"
  kubectl -n "$NAMESPACE" delete job "$JOB_NAME" --ignore-not-found --wait=true >/dev/null
  kubectl apply -f "$OUT/driver-job.yaml"
  date -u +%FT%TZ > "$OUT/started_at"
}

# Wait until the Job has a Complete or Failed condition; print and record it.
wait_job() {
  local cond
  while :; do
    cond=$(kubectl -n "$NAMESPACE" get job "$JOB_NAME" \
      -o jsonpath='{range .status.conditions[?(@.status=="True")]}{.type}{" "}{end}')
    [[ $cond == *Complete* || $cond == *Failed* ]] && break
    sleep 15
  done
  date -u +%FT%TZ > "$OUT/finished_at"
  kubectl -n "$NAMESPACE" get job "$JOB_NAME" -o wide | tee "$OUT/job.txt"
  log "$JOB_NAME: $cond"
}

result_keys() {  # page record keys of this test's files (by the sha256 of each copy)
  aws s3 ls --recursive "$STORE_LOCATION/work/" | awk '{print $4}' \
    | grep -E "/work/[0-9a-f]{64}/[0-9a-f]{12}/results/[0-9]{4}\.json$" \
    | grep -F -f <(sed 's|^|/work/|; s|$|/|' "$OUT/.sha_$1") || true
}

pages_done() {  # page records written so far for this test's files
  result_keys "$1" | wc -l
}

collect() {  # driver logs and every file's status.json
  kubectl -n "$NAMESPACE" logs -l job-name="$JOB_NAME" --prefix --tail=-1 --max-log-requests=20 \
    > "$OUT/driver.log" 2>&1 || true
  mkdir -p "$OUT/status"
  local only='.'  # with a .sha_<name> list, only this run's files (ids end in -<sha256[:16]>)
  [[ -s $OUT/.sha_$1 ]] && only="-($(cut -c1-16 "$OUT/.sha_$1" | paste -sd'|'))$"
  aws s3 ls "$STORE_LOCATION/out/" | awk '{print $2}' | tr -d / | grep -E "^$1-" | grep -E -- "$only" | while read -r id; do
    aws s3 cp --only-show-errors "$STORE_LOCATION/out/$id/status.json" "$OUT/status/$id.json" || true
  done
  uv run --project "$REPO_ROOT" python "$REPO_ROOT/deploy/scripts/summarize_status.py" "$OUT/status" \
    | tee "$OUT/summary.txt" || true  # a failed file is a result to report, not a reason to stop
}

if [[ $TEST =~ ^T[234]$ && ! -f $BIG_PDF ]]; then
  die "$TEST needs the 107-page PDF at $BIG_PDF (not in git). The 2026-10-03 run uploaded it to
  s3://de-genai-dev/parser/<that run's TEST_RUN_ID>/input/source/247136389-5MB.pdf; copy it there or set BIG_PDF."
fi

case $TEST in
  T1)
    TEST_DIR=t1; ESC=${ESCALATE:-on}
    upload_copies "$MIXED_PDF" t1 sample_mixed_10 1
    start_job t1-correctness 1 "$ESC"; wait_job; collect sample_mixed_10
    id=sample_mixed_10-01-$(head -c 16 "$OUT/.sha_sample_mixed_10")
    aws s3 cp --only-show-errors "$STORE_LOCATION/out/$id/output.md" "$OUT/output.md" || true
    aws s3 cp --only-show-errors "$REFERENCE_OUTPUT" "$OUT/reference.md"
    aws s3 cp --only-show-errors "${REFERENCE_OUTPUT%output.md}status.json" "$OUT/reference-status.json"
    uv run --project "$REPO_ROOT" python "$REPO_ROOT/deploy/scripts/compare_outputs.py" \
      "$OUT/reference.md" "$OUT/output.md" --reference-status "$OUT/reference-status.json" \
      --candidate-status "$OUT/status/$id.json" | tee "$OUT/compare.txt"
    ;;
  T2)
    TEST_DIR=t2; ESC=${ESCALATE:-on}
    upload_copies "$BIG_PDF" t2 scale 10
    start_job t2-scale 10 "$ESC"; wait_job; collect scale
    ;;
  T3)
    TEST_DIR=t3; ESC=${ESCALATE:-on}
    T3_NAME=${T3_NAME:-podloss}  # a fresh name re-runs T3 on new file ids
    upload_copies "$BIG_PDF" t3 "$T3_NAME" 5
    start_job t3-pod-loss 5 "$ESC"
    until (( $(pages_done "$T3_NAME") >= 100 )); do sleep 10; done
    # T3_KILL=graceful (default): delete two pods; the kubelet stops them with
    #   their grace period, so the pages in hand finish (normal scale-in).
    # T3_KILL=crash: SIGKILL every page-service process on one parser node from a
    #   node debug pod, so the pages in hand die with them (like an OOM kill).
    if [[ ${T3_KILL:-graceful} == crash ]]; then
      node=$(kubectl -n "$NAMESPACE" get pods -l serving.kserve.io/inferenceservice=page-parser \
        --field-selector=status.phase=Running -o jsonpath='{.items[0].spec.nodeName}')
      on_node=$(kubectl -n "$NAMESPACE" get pods -l serving.kserve.io/inferenceservice=page-parser \
        --field-selector=spec.nodeName="$node" -o name | tr '\n' ' ')
      log "SIGKILL of every page-service process on $node: $on_node"
      echo "$(date -u +%FT%TZ) SIGKILL on $node: $on_node" | tee "$OUT/killed.txt"
      kubectl debug "node/$node" -q --profile=sysadmin --image=busybox:1.36 -- \
        pkill -9 -f parser_service.service.predictor
    else
      victims=$(kubectl -n "$NAMESPACE" get pods -l serving.kserve.io/inferenceservice=page-parser \
        --field-selector=status.phase=Running -o name | head -2)
      log "deleting page-service pods mid-run: $victims"
      echo "$(date -u +%FT%TZ) deleted $victims" | tee "$OUT/killed.txt"
      # shellcheck disable=SC2086
      kubectl -n "$NAMESPACE" delete --wait=false $victims
    fi
    wait_job; collect "$T3_NAME"
    log "pages that needed more than one attempt (from the driver log and the store):"
    grep -h "request failed" "$OUT/driver.log" | tee "$OUT/failed_requests.txt" || true
    grep -oE "page [0-9]+: request failed" "$OUT/driver.log" | sort -u | while read -r _ n _; do
      n=${n%:}; printf '%04d\n' "$n"
    done | sort -u > "$OUT/.pages"
    result_keys "$T3_NAME" | grep -F -f <(sed 's|$|.json|' "$OUT/.pages") 2>/dev/null | while read -r key; do
        aws s3 cp --only-show-errors "s3://de-genai-dev/$key" - \
          | python3 -c "import json,sys; r=json.load(sys.stdin); print('$key', 'attempt', r['attempt'], r['state'], r.get('worker_host'))"
      done | tee "$OUT/retried_pages.txt"
    ;;
  T5)
    TEST_DIR=t5; ESC=${ESCALATE:-on}
    live=$(kubectl -n "$NAMESPACE" get configmap parser-config -o jsonpath='{.data.PARSER_ENABLED_KINDS}')
    for kind in txt docx html; do
      [[ ,$live, == *,$kind,* ]] || die "T5 needs $kind enabled, but the cluster runs PARSER_ENABLED_KINDS=$live;
  bring it up with: PARSER_ENABLED_KINDS=pdf,txt,docx,html deploy/scripts/20_up.sh"
    done
    inputs=$OUT/inputs; rm -rf "$inputs" "$OUT/local" "$OUT/outputs"
    uv run --project "$REPO_ROOT" --extra dev python "$REPO_ROOT/deploy/scripts/make_demo_inputs.py" \
      "$inputs" "$TEST_RUN_ID/t5${OUT_SUFFIX:-}" | tee "$OUT/inputs.txt"
    : > "$OUT/.sha_demo"; list=""
    for f in "$inputs"/demo-*; do
      sha256sum "$f" | awk '{print $1}' >> "$OUT/.sha_demo"
      aws s3 cp --only-show-errors "$f" "$INPUT_PREFIX/t5/$(basename "$f")"
      list+="$INPUT_PREFIX/t5/$(basename "$f")"$'\n'
    done
    printf '%s' "$list" | aws s3 cp --only-show-errors - "$INPUT_PREFIX/t5/list.txt"
    start_job t5-formats "$(printf '%s' "$list" | wc -l)" "$ESC"; wait_job; collect demo
    mkdir -p "$OUT/outputs" "$OUT/local"
    for s in "$OUT"/status/*.json; do
      id=$(basename "$s" .json)
      aws s3 cp --only-show-errors "$STORE_LOCATION/out/$id/output.md" "$OUT/outputs/$id.md" || true
    done
    log "which pod parsed each unit:"
    fmt_unit=$(cat <<'PY'
import json, sys
r = json.load(sys.stdin)
kind = r.get("kind") or "pdf"
print(f"{kind:5} unit {r['page']:>2}  {r['state']:8} {str(r.get('route')):22} "
      f"{r.get('elapsed_seconds') or 0:6.1f}s  {r.get('worker_host')}")
PY
)
    result_keys demo | while read -r key; do
      aws s3 cp --only-show-errors "s3://de-genai-dev/$key" - | python3 -c "$fmt_unit"
    done | sort | tee "$OUT/units.txt"
    log "local reference run of the same inputs, compared with the cluster output:"
    mkdir -p "$OUT/local"; no_esc=(); [[ $ESC == off ]] && no_esc=(--no-escalate)
    for f in "$inputs"/demo-*; do
      b=$(basename "$f"); id=${b%.*}-$(sha256sum "$f" | head -c 16)
      PARSER_ENABLED_KINDS=$PARSER_ENABLED_KINDS uv run --project "$REPO_ROOT" python "$REPO_ROOT/scripts/parse_pages.py" \
        --input "$f" --work-dir "$OUT/local/store" --output "$OUT/local/$b.md" "${no_esc[@]}" \
        > "$OUT/local/$b.log" 2>&1 || true
      if [[ $b == *.pdf ]]; then
        echo "== $b (escalated pages can differ run to run)"
        uv run --project "$REPO_ROOT" python "$REPO_ROOT/deploy/scripts/compare_outputs.py" \
          "$OUT/local/$b.md" "$OUT/outputs/$id.md" \
          --reference-status "$OUT/local/store/out/$id/status.json" --candidate-status "$OUT/status/$id.json" || true
      elif cmp -s "$OUT/local/$b.md" "$OUT/outputs/$id.md"; then
        echo "== $b: cluster output is byte-identical to the local run"
      else
        echo "== $b: DIFFERS from the local run"; diff "$OUT/local/$b.md" "$OUT/outputs/$id.md" | head -20
      fi
    done | tee "$OUT/compare.txt"
    ;;
  T4)
    TEST_DIR=t4; ESC=${ESCALATE:-on}
    upload_copies "$BIG_PDF" t4 driverloss 1
    start_job t4-driver-loss 1 "$ESC"
    until (( $(pages_done driverloss) >= 30 )); do sleep 5; done
    victim=$(kubectl -n "$NAMESPACE" get pods -l job-name="$JOB_NAME" -o name | head -1)
    log "deleting driver pod mid-file: $victim"
    echo "$(date -u +%FT%TZ) deleted $victim after $(pages_done driverloss) pages" | tee "$OUT/killed.txt"
    kubectl -n "$NAMESPACE" logs "$victim" > "$OUT/driver-before-kill.log" 2>&1 || true
    kubectl -n "$NAMESPACE" delete "$victim" --wait=false
    wait_job; collect driverloss
    grep -h "already done" "$OUT/driver.log" | tee "$OUT/resume.txt" || true
    ;;
  *) die "unknown test $TEST" ;;
esac
log "$TEST done; evidence in $OUT"
