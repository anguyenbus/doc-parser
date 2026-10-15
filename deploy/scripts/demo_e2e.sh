#!/usr/bin/env bash
# demo_e2e.sh — the narrated end-to-end run that is recorded for the demo video.
#
# Shows the cluster, runs `30_run_test.sh T5` (html, docx, a 10-page pdf, txt) with a
# live view of the driver pods, the page-service pods and the units they finish, then
# prints the results. Needs the cluster from 20_up.sh. Record it with:
#   asciinema rec -i 2 --cols 120 --rows 34 -c deploy/scripts/demo_e2e.sh demo.cast
source "$(dirname "$0")/lib.sh"
set +e  # a transient kubectl error must not end the recording

OUT=$EVIDENCE/T5${OUT_SUFFIX:-}
ISVC=serving.kserve.io/inferenceservice=page-parser
C=$'\033[1;36m'; D=$'\033[2m'; B=$'\033[1m'; G=$'\033[1;32m'; Y=$'\033[1;33m'; R=$'\033[0m'

say() { printf '\n%s▶ %s%s\n' "$C" "$*" "$R"; sleep 2; }
run() { printf '%s$ %s%s\n' "$D" "$*" "$R"; eval "$@"; sleep 1; }

clear
printf '%sdoc-parser: end-to-end on EKS + KServe%s\n' "$B" "$R"
printf 'One run, four formats: HTML, DOCX, a 10-page PDF and TXT.\n'
printf 'PDFs are split into pages; every other format is parsed as one unit (no split).\n'
printf 'Each unit is sent to the page service, which runs 2-3 pods.\n'
sleep 4

say "1. The cluster: parser nodes, and the page service (KServe): 2 pods, autoscaled up to 3"
run "kubectl get nodes -L role,node.kubernetes.io/instance-type --no-headers | awk '{print \$1, \$2, \$6, \$7}' | column -t"
run "kubectl -n $NAMESPACE get pods -l $ISVC -o wide | awk '{print \$1, \$2, \$3, \$5, \$7}' | column -t"
run "kubectl -n $NAMESPACE get hpa"

say "2. Formats enabled (every format with a tested handler)"
run "kubectl -n $NAMESPACE get configmap parser-config -o jsonpath='{.data.PARSER_ENABLED_KINDS}'; echo"

say "3. Start the run: four input files, one driver pod per file (a Kubernetes Indexed Job)"
log_file=$EVIDENCE/T5${OUT_SUFFIX:-}.log
"$(dirname "$0")/30_run_test.sh" T5 > "$log_file" 2>&1 &
runner=$!
until [[ -s $OUT/inputs.txt ]] || ! kill -0 "$runner" 2>/dev/null; do sleep 1; done
printf '%s%-22s %10s%s\n' "$B" "input file" "bytes" "$R"
awk -F'\t' '{printf "%-22s %10s\n", $1, $2}' "$OUT/inputs.txt"
sleep 3

say "4. Live: drivers split each file into units; the page-service pods parse them"
sleep 1
start=$(date +%s); since=$(date -u +%FT%TZ)
job_done() {
  [[ $(kubectl -n "$NAMESPACE" get job t5-formats -o jsonpath='{range .status.conditions[?(@.status=="True")]}{.type}{" "}{end}' 2>/dev/null) =~ Complete|Failed ]]
}
final=0
while kill -0 "$runner" 2>/dev/null && (( final < 2 )); do
  job_done && final=$((final + 1))  # one last view after the Job finishes, then move on
  frame=$(
  printf '%s▶ live view%s   %s elapsed %ss%s\n\n' "$C" "$R" "$D" "$(( $(date +%s) - start ))" "$R"
  printf '%sDriver pods (one per input file)%s\n' "$B" "$R"
  kubectl -n "$NAMESPACE" get pods -l job-name=t5-formats --no-headers 2>/dev/null \
    | awk '{printf "  %-36s %-10s %s\n", $1, $3, $5}'
  printf '\n%sPage-service pods%s\n' "$B" "$R"
  kubectl -n "$NAMESPACE" get pods -l "$ISVC" --no-headers -o wide 2>/dev/null \
    | awk '{printf "  %-52s %-6s %-10s %s\n", $1, $2, $3, $7}'
  kubectl -n "$NAMESPACE" get hpa --no-headers 2>/dev/null \
    | awk '{t=$3; i=4; if (t == "cpu:") {t=$4; i=5}; printf "  autoscaler: cpu %s (target after the /), %s replicas (min %s, max %s)\n", t, $(i+2), $i, $(i+1)}'
  printf '\n%sLatest units finished (pod, file kind, unit, state, route, seconds)%s\n' "$B" "$R"
  kubectl -n "$NAMESPACE" logs -l "$ISVC" -c kserve-container --prefix --timestamps --since-time="$since" --tail=5000 2>/dev/null \
    | grep 'page_done' | sort -k2,2 | tail -8 \
    | python3 -c '
import json, re, sys
for line in sys.stdin:
    pod = re.search(r"pod/([^ /]+)", line)
    m = re.search(r"\{.*\}", line)
    if not m:
        continue
    try:
        e = json.loads(m.group(0))
    except json.JSONDecodeError:
        continue
    name = (pod.group(1) if pod else "?").replace("page-parser-predictor-", "pod ")
    print(f"  {name:20} {str(e.get("kind", "pdf")):5} unit {e.get("page", "?"):>2}  "
          f"{str(e.get("state")):8} {str(e.get("route")):22} {e.get("elapsed_seconds") or 0:6.1f}s")
'
  )
  clear; printf '%s\n' "$frame"
  sleep 4
done
printf '\n%sAll driver pods finished. Collecting the results and parsing the same inputs on this\nbox for comparison...%s\n' "$C" "$R"
wait "$runner"; status=$?
clear

say "5. Results: every file, its state and its output"
cat "$OUT/summary.txt"
sleep 3

say "Which pod parsed each unit (the 10 PDF pages are spread across the page-service pods)"
cat "$OUT/units.txt"
sleep 5

for f in "$OUT"/outputs/*.md; do
  name=$(basename "$f" .md)
  say "Output: ${name%-*}  (first lines of the stitched markdown)"
  head -n 18 "$f"
  sleep 4
done

say "6. The same inputs parsed on this dev box, compared with the cluster output"
printf '%sHTML, DOCX and TXT must be byte-identical. PDF: pages Docling keeps must match;\n' "$D"
printf 'pages sent to the VLM can differ run to run, and this box (CUDA torch build) may grade\n'
printf 'a page differently from the image (CPU torch build), so routes can differ.%s\n' "$R"
cat "$OUT/compare.txt"
sleep 4

if (( status == 0 )); then
  printf '\n%s✔ end-to-end run finished: HTML, DOCX, PDF (10 pages) and TXT parsed on the cluster%s\n' "$G" "$R"
else
  printf '\n%s! the run ended with exit %s; see %s%s\n' "$Y" "$status" "$log_file" "$R"
fi
printf '%sevidence: %s%s\n' "$D" "$OUT" "$R"
sleep 5
