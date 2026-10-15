#!/usr/bin/env bash
# 20_up.sh — create the test cluster and install everything the test needs.
#
# 1. eksctl creates the cluster and both node groups (skipped if it exists, and
#    refused if an existing cluster of that name lacks this test's TestRun tag).
# 2. Tags the control-plane log group; checks pods can reach the node role.
# 3. cert-manager, KServe (CRDs + controller, Standard mode), Cluster Autoscaler.
# 4. Namespace, shared ConfigMap, the page-parser InferenceService; waits Ready.
# Safe to re-run: every step is create-or-update.
source "$(dirname "$0")/lib.sh"

CONFIG_DIR=$EVIDENCE/config
mkdir -p "$CONFIG_DIR"
CHART_CACHE=$HOME/.cache/doc-parser-deploy
mkdir -p "$CHART_CACHE"

# --- 1. cluster ---------------------------------------------------------------
render "$REPO_ROOT/deploy/eks/cluster.yaml.tpl" > "$CONFIG_DIR/cluster.yaml"
if cluster_exists; then
  [[ $(cluster_test_run_tag) == "$TEST_RUN_ID" ]] \
    || die "cluster $CLUSTER_NAME exists without TestRun=$TEST_RUN_ID; not touching it"
  log "cluster $CLUSTER_NAME exists; skipping create"
else
  log "creating cluster $CLUSTER_NAME (about 15-20 min)"
  # eksctl's own node check fails here (see below), so its exit status is not
  # trusted; the cluster's resources are checked by the steps that follow.
  eksctl create cluster -f "$CONFIG_DIR/cluster.yaml" 2>&1 | tee "$EVIDENCE/eksctl-create.log" || true
fi

# With private endpoint access on, the API name resolves to private IPs inside
# the VPC, and the EKS-created cluster security group does not admit this dev
# box. Allow 443 from the dev box's security group (the rule and the group are
# deleted with the cluster) and tag the group, which EKS creates untagged.
CLUSTER_SG=$(aws eks describe-cluster --name "$CLUSTER_NAME" \
  --query cluster.resourcesVpcConfig.clusterSecurityGroupId --output text)
MY_SG=$(curl -s -m 2 -H "X-aws-ec2-metadata-token: $(curl -s -m 2 -X PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" http://169.254.169.254/latest/meta-data/security-groups \
  | head -1 | xargs -I{} aws ec2 describe-security-groups --filters Name=group-name,Values={} \
  --query 'SecurityGroups[0].GroupId' --output text)
aws ec2 authorize-security-group-ingress --group-id "$CLUSTER_SG" \
  --ip-permissions "IpProtocol=tcp,FromPort=443,ToPort=443,UserIdGroupPairs=[{GroupId=$MY_SG,Description=doc-parser test dev box kubectl}]" \
  >/dev/null 2>&1 || log "API rule from $MY_SG already present"
aws ec2 create-tags --resources "$CLUSTER_SG" --tags Key=TestRun,Value="$TEST_RUN_ID" \
  Key=Project,Value="$TAG_PROJECT" Key=Owner,Value="$TAG_OWNER" Key=Purpose,Value="$TAG_PURPOSE" \
  Key=ExpiresAt,Value="$TAG_EXPIRES_AT"

eksctl utils write-kubeconfig --cluster "$CLUSTER_NAME" --region "$AWS_REGION" --kubeconfig "$IAM_KUBECONFIG"

# --- node identity ----------------------------------------------------------------
# The nodes use the dev box's IAM role, which also created the cluster, so the
# role's access entry is the creator's admin (STANDARD) entry: every node would be
# a cluster admin, and EKS issues no kubelet serving certificates to it (no
# metrics-server, so no HPA; no kubectl logs/exec). Fix: give the operator (this
# box) a cluster-admin ServiceAccount token first, then make the role's entry an
# EC2_LINUX node entry, so nodes are system:node:<name> with node permissions.
ROLE_ENTRY_TYPE=$(aws eks describe-access-entry --cluster-name "$CLUSTER_NAME" --principal-arn "$NODE_ROLE_ARN" \
  --query accessEntry.type --output text 2>/dev/null || echo none)
if [[ $ROLE_ENTRY_TYPE != EC2_LINUX ]]; then
  KUBECONFIG=$IAM_KUBECONFIG kubectl apply -f "$REPO_ROOT/deploy/k8s/test-operator.yaml"
  until [[ -n $(KUBECONFIG=$IAM_KUBECONFIG kubectl -n kube-system get secret test-operator-token -o jsonpath='{.data.token}') ]]; do sleep 1; done
  token=$(KUBECONFIG=$IAM_KUBECONFIG kubectl -n kube-system get secret test-operator-token -o jsonpath='{.data.token}' | base64 -d)
  install -m 600 "$IAM_KUBECONFIG" "$SA_KUBECONFIG"
  ctx=$(KUBECONFIG=$SA_KUBECONFIG kubectl config current-context)
  KUBECONFIG=$SA_KUBECONFIG kubectl config set-credentials test-operator --token="$token" >/dev/null
  KUBECONFIG=$SA_KUBECONFIG kubectl config set-context "$ctx" --user=test-operator >/dev/null
  KUBECONFIG=$SA_KUBECONFIG kubectl auth can-i '*' '*' --all-namespaces | grep -qx yes \
    || die "the test-operator token is not cluster-admin; access entry left unchanged"
  log "switching the node role's access entry ($ROLE_ENTRY_TYPE) to EC2_LINUX"
  [[ $ROLE_ENTRY_TYPE == none ]] || aws eks delete-access-entry --cluster-name "$CLUSTER_NAME" --principal-arn "$NODE_ROLE_ARN"
  aws eks create-access-entry --cluster-name "$CLUSTER_NAME" --principal-arn "$NODE_ROLE_ARN" --type EC2_LINUX \
    --tags "TestRun=$TEST_RUN_ID,Project=$TAG_PROJECT" >/dev/null
fi
export KUBECONFIG=$SA_KUBECONFIG
log "waiting for every node to have an issued kubelet serving certificate"
nodes=$(kubectl get nodes --no-headers | wc -l)
until (( $(kubectl get csr -o jsonpath='{range .items[*]}{.spec.username} {.status.certificate}{"\n"}{end}' \
    | awk '$1 ~ /^system:node:/ && length($2) > 0 {print $1}' | sort -u | wc -l) >= nodes )); do sleep 15; done
kubectl get nodes -L role,node.kubernetes.io/instance-type

# metrics-server feeds the HPA. eksctl installs it only after its own node check,
# which fails here, so make sure the add-on exists.
if ! aws eks describe-addon --cluster-name "$CLUSTER_NAME" --addon-name metrics-server >/dev/null 2>&1; then
  aws eks create-addon --cluster-name "$CLUSTER_NAME" --addon-name metrics-server \
    --configuration-values '{"nodeSelector":{"role":"system"}}' \
    --tags "TestRun=$TEST_RUN_ID,Project=$TAG_PROJECT" >/dev/null
fi
aws eks wait addon-active --cluster-name "$CLUSTER_NAME" --addon-name metrics-server
until kubectl top nodes >/dev/null 2>&1; do sleep 5; done

# --- 2. log group tags, pod access to the node role ------------------------------
LOG_GROUP=/aws/eks/$CLUSTER_NAME/cluster
if aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --query 'logGroups[0].arn' --output text | grep -q arn; then
  aws logs tag-log-group --log-group-name "$LOG_GROUP" --tags "$(logs_tags)"
fi

log "IMDS hop limit on the node launch templates (pods need 2):"
for ng in system parser; do
  lt=$(aws eks describe-nodegroup --cluster-name "$CLUSTER_NAME" --nodegroup-name "$ng" \
    --query 'nodegroup.launchTemplate.id' --output text)
  hops=$(aws ec2 describe-launch-template-versions --launch-template-id "$lt" --versions '$Latest' \
    --query 'LaunchTemplateVersions[0].LaunchTemplateData.MetadataOptions.HttpPutResponseHopLimit' --output text)
  log "  $ng: $hops"
  [[ $hops == 2 ]] || die "node group $ng has IMDS hop limit $hops; pods cannot use the node role"
done

# --- 3. add-ons -------------------------------------------------------------------
log "cert-manager $CERT_MANAGER_VERSION"
helm upgrade --install cert-manager oci://quay.io/jetstack/charts/cert-manager \
  --version "$CERT_MANAGER_VERSION" -n cert-manager --create-namespace \
  --set crds.enabled=true \
  --set nodeSelector.role=system --set webhook.nodeSelector.role=system \
  --set cainjector.nodeSelector.role=system --set startupapicheck.nodeSelector.role=system \
  --wait --timeout 10m

fetch_chart() {  # name sha256
  local file=$CHART_CACHE/$1
  [[ -f $file ]] || curl -fsSL -o "$file" "$KSERVE_CHART_URL/$1"
  echo "$2  $file" | sha256sum -c --quiet - || die "checksum mismatch for $1"
  echo "$file"
}
log "KServe $KSERVE_VERSION (Standard mode)"
helm upgrade --install kserve-crd "$(fetch_chart "$KSERVE_CRD_CHART" "$KSERVE_CRD_CHART_SHA256")" \
  -n kserve --create-namespace --wait --timeout 10m
helm upgrade --install kserve "$(fetch_chart "$KSERVE_RESOURCES_CHART" "$KSERVE_RESOURCES_CHART_SHA256")" \
  -n kserve --set kserve.version="$KSERVE_VERSION" \
  -f "$REPO_ROOT/deploy/k8s/kserve-values.yaml" --wait --timeout 10m

log "Cluster Autoscaler $CLUSTER_AUTOSCALER_TAG"
helm repo add autoscaler https://kubernetes.github.io/autoscaler >/dev/null 2>&1 || true
helm repo update autoscaler >/dev/null
render "$REPO_ROOT/deploy/k8s/cluster-autoscaler-values.yaml.tpl" > "$CONFIG_DIR/cluster-autoscaler-values.yaml"
helm upgrade --install cluster-autoscaler autoscaler/cluster-autoscaler \
  --version "$CLUSTER_AUTOSCALER_CHART_VERSION" -n kube-system \
  -f "$CONFIG_DIR/cluster-autoscaler-values.yaml" --wait --timeout 10m

# --- 4. the page service ----------------------------------------------------------
render "$REPO_ROOT/deploy/k8s/namespace.yaml.tpl" > "$CONFIG_DIR/namespace.yaml"
render "$REPO_ROOT/deploy/k8s/inferenceservice.yaml.tpl" > "$CONFIG_DIR/inferenceservice.yaml"
kubectl apply -f "$CONFIG_DIR/namespace.yaml"

log "checking a pod can use the node role (S3 + identity)"
kubectl -n "$NAMESPACE" delete pod aws-access-check --ignore-not-found >/dev/null
kubectl -n "$NAMESPACE" run aws-access-check --image="$IMAGE_URI" --restart=Never \
  --overrides='{"spec":{"nodeSelector":{"role":"system"}}}' \
  --env="AWS_REGION=$AWS_REGION" --command -- python -c "
import boto3
print(boto3.client('sts').get_caller_identity()['Arn'])
b, p = '${S3_PREFIX#s3://}'.split('/', 1)
boto3.client('s3').list_objects_v2(Bucket=b, Prefix=p, MaxKeys=1)
print('s3 ok')"
kubectl -n "$NAMESPACE" wait --for=jsonpath='{.status.phase}'=Succeeded pod/aws-access-check --timeout=300s \
  || { kubectl -n "$NAMESPACE" logs aws-access-check; die "pod cannot use the node role"; }
kubectl -n "$NAMESPACE" logs aws-access-check | tee "$EVIDENCE/aws-access-check.txt"
kubectl -n "$NAMESPACE" delete pod aws-access-check >/dev/null

kubectl apply -f "$CONFIG_DIR/inferenceservice.yaml"
log "waiting for the page-parser InferenceService (image pull + model load)"
kubectl -n "$NAMESPACE" wait --for=condition=Ready isvc/page-parser --timeout=15m
kubectl -n "$NAMESPACE" get isvc,deploy,hpa,svc,pods -o wide | tee "$EVIDENCE/up-state.txt"
log "cluster ready"
