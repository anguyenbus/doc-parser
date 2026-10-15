# deploy/scripts/lib.sh — sourced by every test script. Not executable on its own.
#
# Loads deploy/test.env, puts the user-space tools first on PATH, and points
# kubectl/helm at this test's own kubeconfig so nothing touches another cluster.

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=../test.env
source "$REPO_ROOT/deploy/test.env"

export PATH=$HOME/.local/bin:$PATH
# eksctl's kubeconfig authenticates as the shared IAM role; once 20_up.sh has
# turned that role into a node identity, kubectl uses the test-operator token.
IAM_KUBECONFIG=$HOME/.kube/$TEST_RUN_ID.yaml
SA_KUBECONFIG=$HOME/.kube/$TEST_RUN_ID-sa.yaml
if [[ -f $SA_KUBECONFIG ]]; then export KUBECONFIG=$SA_KUBECONFIG; else export KUBECONFIG=$IAM_KUBECONFIG; fi
EVIDENCE=$REPO_ROOT/$EVIDENCE_DIR
mkdir -p "$EVIDENCE"
# Deploy the image by the digest 10_build_push.sh recorded, not by its tag: with
# imagePullPolicy IfNotPresent a node would keep running a cached older ":dev".
IMAGE_REF=$IMAGE_URI
if [[ -s $EVIDENCE/image.txt ]]; then IMAGE_REF=$ECR_REGISTRY/$ECR_REPO@$(cut -f1 "$EVIDENCE/image.txt"); fi
export IMAGE_REF

log() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# Render a template with envsubst, substituting ONLY the variables it names, so a
# literal $ elsewhere in a manifest is left alone.
render() {
  local vars
  vars=$(grep -o '\${[A-Z_][A-Z0-9_]*}' "$1" | sort -u | tr '\n' ' ')
  envsubst "$vars" < "$1"
}

# Tags in the formats the CLIs want.
ecr_tags() {
  printf 'Key=Project,Value=%s Key=TestRun,Value=%s Key=Owner,Value=%s Key=Purpose,Value=%s Key=ExpiresAt,Value=%s Key=ManagedBy,Value=scripts' \
    "$TAG_PROJECT" "$TEST_RUN_ID" "$TAG_OWNER" "$TAG_PURPOSE" "$TAG_EXPIRES_AT"
}
logs_tags() {
  printf 'Project=%s,TestRun=%s,Owner=%s,Purpose=%s,ExpiresAt=%s,ManagedBy=eksctl' \
    "$TAG_PROJECT" "$TEST_RUN_ID" "$TAG_OWNER" "$TAG_PURPOSE" "$TAG_EXPIRES_AT"
}

cluster_exists() {
  aws eks describe-cluster --name "$CLUSTER_NAME" --query cluster.status --output text >/dev/null 2>&1
}

# The TestRun tag of an EKS cluster, or empty.
cluster_test_run_tag() {
  aws eks describe-cluster --name "$CLUSTER_NAME" --query 'cluster.tags.TestRun' --output text 2>/dev/null || true
}

# Refuse to act on S3 outside this test's prefix.
require_test_prefix() {
  case "$1" in
    "$S3_PREFIX"/*|"$S3_PREFIX") ;;
    *) die "refusing S3 path outside $S3_PREFIX: $1" ;;
  esac
}
