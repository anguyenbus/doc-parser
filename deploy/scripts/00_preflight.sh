#!/usr/bin/env bash
# 00_preflight.sh — read-only checks before anything is created.
#
# Tools, identity, region, quota, disk, the API allow-list address, and whether
# this test's cluster or ECR repo already exist. Creates nothing.
source "$(dirname "$0")/lib.sh"

ok=1
check() { if eval "$2" >/dev/null 2>&1; then log "ok    $1"; else log "FAIL  $1"; ok=0; fi; }

check "aws cli"  "aws --version"
check "eksctl"   "eksctl version"
check "kubectl"  "kubectl version --client"
check "helm"     "helm version --short"
check "envsubst" "command -v envsubst"
check "docker (sudo)" "sudo -n docker info"

account=$(aws sts get-caller-identity --query Account --output text)
[[ $account == "$AWS_ACCOUNT_ID" ]] && log "ok    account $account" || { log "FAIL  account $account, expected $AWS_ACCOUNT_ID"; ok=0; }

quota=$(aws service-quotas get-service-quota --service-code ec2 --quota-code L-1216C47A \
  --query 'Quota.Value' --output text 2>/dev/null || echo 0)
log "info  on-demand standard vCPU quota: $quota (test peak: 2x2 + ${PARSER_NODES_MAX}x16 = $((4 + PARSER_NODES_MAX * 16)))"

free_gb=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
(( free_gb >= 10 )) && log "ok    disk: ${free_gb} GB free" || { log "FAIL  disk: ${free_gb} GB free, need 10"; ok=0; }

my_ip=$(curl -s -m 5 https://checkip.amazonaws.com || true)
[[ "$my_ip/32" == "$API_ALLOWED_CIDR" ]] && log "ok    API allow-list matches this box ($my_ip)" \
  || { log "FAIL  this box is $my_ip but API_ALLOWED_CIDR is $API_ALLOWED_CIDR"; ok=0; }

if cluster_exists; then
  log "info  cluster $CLUSTER_NAME already exists (TestRun=$(cluster_test_run_tag))"
else
  log "info  cluster $CLUSTER_NAME does not exist yet"
fi
if aws ecr describe-repositories --repository-names "$ECR_REPO" >/dev/null 2>&1; then
  log "info  ECR repo $ECR_REPO already exists"
fi

(( ok )) || die "preflight failed"
log "preflight passed"
