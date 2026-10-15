#!/usr/bin/env bash
# 40_capture_evidence.sh start|stop|dump — record what the cluster did.
#
#   start  background sampler: every 15 s one row in timeline.csv (HPA desired /
#          current replicas, ready and pending page-service pods, HPA CPU %,
#          parser nodes, page records written).
#   stop   stop the sampler and render timeline.png from timeline.csv.
#   dump   snapshot the cluster: objects, HPA and node events, autoscaler log,
#          page-service logs, the parser ASG's scaling activity, tagged EC2.
source "$(dirname "$0")/lib.sh"

PIDFILE=$EVIDENCE/.sampler.pid
TIMELINE=$EVIDENCE/timeline.csv
PS_LABEL=serving.kserve.io/inferenceservice=page-parser

sample() {
  [[ -f $TIMELINE ]] || echo "time,hpa_desired,hpa_current,pods_ready,pods_pending,hpa_cpu_pct,parser_nodes,parser_nodes_ready,pages_done" > "$TIMELINE"
  while :; do
    local hpa pods nodes pages
    hpa=$(kubectl -n "$NAMESPACE" get hpa page-parser-predictor \
      -o jsonpath='{.status.desiredReplicas},{.status.currentReplicas},{.status.currentMetrics[0].resource.current.averageUtilization}' 2>/dev/null || echo ",,")
    pods=$(kubectl -n "$NAMESPACE" get pods -l "$PS_LABEL" \
      -o jsonpath='{range .items[*]}{.status.phase}{":"}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null || true)
    nodes=$(kubectl get nodes -l role=parser \
      -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null || true)
    pages=$(aws s3 ls --recursive "$STORE_LOCATION/work/" 2>/dev/null | grep -cE '/results/[0-9]{4}\.json$' || true)
    IFS=, read -r desired current cpu <<< "$hpa"
    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s\n' "$(date -u +%FT%TZ)" "$desired" "$current" \
      "$(grep -c 'Running:True' <<< "$pods" || true)" "$(grep -c '^Pending' <<< "$pods" || true)" \
      "$cpu" "$(grep -c . <<< "$nodes" || true)" "$(grep -c True <<< "$nodes" || true)" "$pages" >> "$TIMELINE"
    sleep 15
  done
}

case ${1:-} in
  start)
    if [[ -f $PIDFILE ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      log "sampler already running (pid $(cat "$PIDFILE"))"; exit 0
    fi
    sample > "$EVIDENCE/.sampler.log" 2>&1 &
    echo $! > "$PIDFILE"
    log "sampler started (pid $!), writing $TIMELINE"
    ;;
  stop)
    [[ -f $PIDFILE ]] && kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE"
    uv run --project "$REPO_ROOT" --with matplotlib python "$REPO_ROOT/deploy/scripts/render_timeline.py" \
      "$TIMELINE" "$EVIDENCE/timeline.png"
    ;;
  dump)
    D=$EVIDENCE/snapshot-$(date -u +%H%M%S)
    mkdir -p "$D"
    kubectl get nodes -L role,node.kubernetes.io/instance-type,topology.kubernetes.io/zone -o wide > "$D/nodes.txt"
    kubectl -n "$NAMESPACE" get isvc,deploy,rs,hpa,svc,pods,jobs -o wide > "$D/doc-parser.txt"
    kubectl -n "$NAMESPACE" describe hpa page-parser-predictor > "$D/hpa-describe.txt" 2>&1 || true
    kubectl -n "$NAMESPACE" get isvc page-parser -o yaml > "$D/isvc.yaml"
    kubectl get events -A --sort-by=.lastTimestamp > "$D/events.txt"
    kubectl -n kube-system logs -l app.kubernetes.io/name=aws-cluster-autoscaler --tail=-1 > "$D/cluster-autoscaler.log" 2>&1 || true
    kubectl -n "$NAMESPACE" logs -l "$PS_LABEL" --prefix --tail=-1 --max-log-requests=40 > "$D/page-service.log" 2>&1 || true
    grep -h "page_done" "$D/page-service.log" | sed 's/.*page_done //' > "$D/page-service-pages.jsonl" || true
    helm list -A > "$D/helm.txt"
    asg=$(aws eks describe-nodegroup --cluster-name "$CLUSTER_NAME" --nodegroup-name parser \
      --query 'nodegroup.resources.autoScalingGroups[0].name' --output text)
    aws autoscaling describe-scaling-activities --auto-scaling-group-name "$asg" \
      --query 'Activities[].[StartTime,StatusCode,Description]' --output table > "$D/parser-asg-activity.txt"
    aws ec2 describe-instances --filters "Name=tag:TestRun,Values=$TEST_RUN_ID" \
      --query 'Reservations[].Instances[].[InstanceId,InstanceType,State.Name,LaunchTime,Tags[?Key==`eks:nodegroup-name`].Value|[0]]' \
      --output table > "$D/ec2-instances.txt"
    log "snapshot in $D"
    ;;
  *) die "usage: 40_capture_evidence.sh start|stop|dump" ;;
esac
