#!/usr/bin/env bash
# 90_down.sh [--delete-s3] — remove everything the test created. Safe to re-run.
#
# Order: stop the sampler; delete the Jobs and the InferenceService (no load
# balancers or volumes exist, but nothing is left running while the cluster
# goes); eksctl deletes the node groups and the cluster (their CloudFormation
# stacks, so every stack resource goes with them); then the ECR repo and the
# control-plane log group, which eksctl does not own. Each AWS resource is
# checked for TestRun=$TEST_RUN_ID before it is deleted.
#
# --delete-s3 also removes $S3_PREFIX (inputs, page records, outputs). Without
# it the test data stays as evidence; it is under parser/ only.
source "$(dirname "$0")/lib.sh"

"$(dirname "$0")/40_capture_evidence.sh" stop >/dev/null 2>&1 || true

if cluster_exists; then
  tag=$(cluster_test_run_tag)
  [[ $tag == "$TEST_RUN_ID" ]] || die "cluster $CLUSTER_NAME has TestRun=$tag, not $TEST_RUN_ID; not deleting"
  if [[ -f $KUBECONFIG ]]; then
    kubectl -n "$NAMESPACE" delete jobs --all --ignore-not-found --wait=false || true
    kubectl -n "$NAMESPACE" delete isvc page-parser --ignore-not-found --wait=true --timeout=5m || true
  fi
  # eksctl deletes as the IAM role. 20_up.sh made that role a node identity
  # (EC2_LINUX), which cannot list the cluster's Ingresses as eksctl requires, so
  # give the role cluster-admin back first; the nodes are going anyway.
  if [[ $(aws eks describe-access-entry --cluster-name "$CLUSTER_NAME" --principal-arn "$NODE_ROLE_ARN" \
      --query accessEntry.type --output text 2>/dev/null) == EC2_LINUX ]]; then
    log "restoring the role's admin access entry for eksctl"
    aws eks delete-access-entry --cluster-name "$CLUSTER_NAME" --principal-arn "$NODE_ROLE_ARN"
    aws eks create-access-entry --cluster-name "$CLUSTER_NAME" --principal-arn "$NODE_ROLE_ARN" \
      --type STANDARD --tags "TestRun=$TEST_RUN_ID,Project=$TAG_PROJECT" >/dev/null
    aws eks associate-access-policy --cluster-name "$CLUSTER_NAME" --principal-arn "$NODE_ROLE_ARN" \
      --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
      --access-scope type=cluster >/dev/null
    until KUBECONFIG=$IAM_KUBECONFIG kubectl auth can-i list ingresses --all-namespaces 2>/dev/null | grep -qx yes; do sleep 5; done
  fi
  log "deleting cluster $CLUSTER_NAME (about 10-15 min)"
  KUBECONFIG=$IAM_KUBECONFIG eksctl delete cluster --name "$CLUSTER_NAME" --region "$AWS_REGION" --wait --disable-nodegroup-eviction \
    2>&1 | tee "$EVIDENCE/eksctl-delete.log"
else
  log "cluster $CLUSTER_NAME does not exist"
fi

if arn=$(aws ecr describe-repositories --repository-names "$ECR_REPO" \
    --query 'repositories[0].repositoryArn' --output text 2>/dev/null); then
  tag=$(aws ecr list-tags-for-resource --resource-arn "$arn" --query "tags[?Key=='TestRun'].Value|[0]" --output text)
  [[ $tag == "$TEST_RUN_ID" ]] || die "ECR repo $ECR_REPO has TestRun=$tag; not deleting"
  log "deleting ECR repo $ECR_REPO"
  aws ecr delete-repository --repository-name "$ECR_REPO" --force >/dev/null
fi

LOG_GROUP=/aws/eks/$CLUSTER_NAME/cluster
if aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --query 'logGroups[0].arn' --output text | grep -q arn; then
  arn=$(aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --query 'logGroups[0].arn' --output text)
  tag=$(aws logs list-tags-for-resource --resource-arn "${arn%:\*}" --query 'tags.TestRun' --output text 2>/dev/null || true)
  # The group is named after this test's cluster; it may predate the tag call.
  [[ $tag == "$TEST_RUN_ID" || $tag == None || -z $tag ]] || die "log group $LOG_GROUP has TestRun=$tag; not deleting"
  log "deleting log group $LOG_GROUP"
  aws logs delete-log-group --log-group-name "$LOG_GROUP"
fi

if [[ ${1:-} == --delete-s3 ]]; then
  require_test_prefix "$S3_PREFIX"
  log "deleting $S3_PREFIX/"
  aws s3 rm --recursive --only-show-errors "$S3_PREFIX/"
fi

rm -f "$IAM_KUBECONFIG" "$SA_KUBECONFIG"
log "teardown finished; run 99_verify_teardown.sh"
