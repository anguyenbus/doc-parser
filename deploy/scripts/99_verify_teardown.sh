#!/usr/bin/env bash
# 99_verify_teardown.sh — prove nothing of the test is left. Read-only.
#
# Checks each resource type the test could have created, by its TestRun tag or
# its name, and the Resource Groups Tagging API across the region. Writes the
# report to $EVIDENCE_DIR/teardown-verification.txt; exit 1 if anything remains.
source "$(dirname "$0")/lib.sh"

REPORT=$EVIDENCE/teardown-verification.txt
left=0
check() {  # description, command printing one line per remaining resource
  local out
  out=$(eval "$2" 2>/dev/null | sed '/^None$/d;/^$/d' || true)
  if [[ -n $out ]]; then
    printf 'REMAINS  %s\n%s\n' "$1" "$(sed 's/^/         /' <<< "$out")"; left=1
  else
    printf 'none     %s\n' "$1"
  fi
}

{
  echo "Teardown verification for TestRun=$TEST_RUN_ID, account $AWS_ACCOUNT_ID, region $AWS_REGION"
  echo "at $(date -u +%FT%TZ)"
  echo
  check "EKS cluster $CLUSTER_NAME" \
    "aws eks list-clusters --query \"clusters[?@=='$CLUSTER_NAME']\" --output text"
  check "CloudFormation stacks eksctl-$CLUSTER_NAME-* (not deleted)" \
    "aws cloudformation list-stacks --query \"StackSummaries[?starts_with(StackName,'eksctl-$CLUSTER_NAME') && StackStatus!='DELETE_COMPLETE'].[StackName,StackStatus]\" --output text"
  check "EC2 instances tagged TestRun (not terminated)" \
    "aws ec2 describe-instances --filters Name=tag:TestRun,Values=$TEST_RUN_ID Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down --query 'Reservations[].Instances[].[InstanceId,State.Name]' --output text"
  check "EBS volumes tagged TestRun" \
    "aws ec2 describe-volumes --filters Name=tag:TestRun,Values=$TEST_RUN_ID --query 'Volumes[].[VolumeId,State]' --output text"
  check "Network interfaces of the cluster" \
    "aws ec2 describe-network-interfaces --filters Name=tag:cluster.k8s.amazonaws.com/name,Values=$CLUSTER_NAME --query 'NetworkInterfaces[].[NetworkInterfaceId,Status]' --output text; aws ec2 describe-network-interfaces --filters Name=description,Values='Amazon EKS $CLUSTER_NAME' --query 'NetworkInterfaces[].[NetworkInterfaceId,Status]' --output text"
  check "Security groups tagged TestRun" \
    "aws ec2 describe-security-groups --filters Name=tag:TestRun,Values=$TEST_RUN_ID --query 'SecurityGroups[].[GroupId,GroupName]' --output text"
  check "Launch templates tagged TestRun" \
    "aws ec2 describe-launch-templates --filters Name=tag:TestRun,Values=$TEST_RUN_ID --query 'LaunchTemplates[].[LaunchTemplateId,LaunchTemplateName]' --output text"
  check "Auto Scaling groups of the cluster" \
    "aws autoscaling describe-auto-scaling-groups --query \"AutoScalingGroups[?Tags[?Key=='eks:cluster-name' && Value=='$CLUSTER_NAME']].AutoScalingGroupName\" --output text"
  check "ECR repository $ECR_REPO" \
    "aws ecr describe-repositories --repository-names $ECR_REPO --query 'repositories[].repositoryUri' --output text"
  check "CloudWatch log group /aws/eks/$CLUSTER_NAME/cluster" \
    "aws logs describe-log-groups --log-group-name-prefix /aws/eks/$CLUSTER_NAME --query 'logGroups[].logGroupName' --output text"
  echo
  echo "Resource Groups Tagging API, TestRun=$TEST_RUN_ID (can list a deleted resource for a while):"
  aws resourcegroupstaggingapi get-resources --tag-filters "Key=TestRun,Values=$TEST_RUN_ID" \
    --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' '\n' | sed 's/^/  /'
  echo
  echo "S3 test data under $S3_PREFIX/ (kept unless 90_down.sh --delete-s3):"
  echo "  $(aws s3 ls --recursive "$S3_PREFIX/" | wc -l) objects"
  echo
  (( left )) && echo "RESULT: resources remain" || echo "RESULT: no test infrastructure remains"
} | tee "$REPORT"
# The block above ran in a pipeline subshell, so decide from the report itself.
! grep -q '^REMAINS' "$REPORT"
