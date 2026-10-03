# eksctl config for the KServe scale test. Rendered by deploy/scripts/20_up.sh
# with envsubst from deploy/test.env; do not apply this file directly.
#
# - Existing IAM roles only (the account cannot create roles): the pre-made EKS
#   cluster role, and the dev box's instance role as the node role. No OIDC.
# - Public subnets of the default VPC (the private ones have no NAT). No load
#   balancer or ingress is created; the API endpoint's public side admits only
#   the dev box.
# - IMDSv2 only, reachable from pods: pods use the node role for S3 and Bedrock.
# - Every resource carries the test tags (CloudFormation propagates
#   metadata.tags; propagateASGTags copies the node group tags to the ASGs).
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig

metadata:
  name: ${CLUSTER_NAME}
  region: ${AWS_REGION}
  version: "${K8S_VERSION}"
  tags:
    Project: ${TAG_PROJECT}
    TestRun: ${TEST_RUN_ID}
    Owner: ${TAG_OWNER}
    Purpose: ${TAG_PURPOSE}
    ExpiresAt: "${TAG_EXPIRES_AT}"
    ManagedBy: eksctl

iam:
  serviceRoleARN: ${CLUSTER_ROLE_ARN}
  withOIDC: false

accessConfig:
  authenticationMode: API_AND_CONFIG_MAP
  bootstrapClusterCreatorAdminPermissions: true

vpc:
  id: ${VPC_ID}
  subnets:
    public:
      ap-southeast-2a: { id: ${SUBNET_A} }
      ap-southeast-2b: { id: ${SUBNET_B} }
      ap-southeast-2c: { id: ${SUBNET_C} }
  clusterEndpoints:
    publicAccess: true
    privateAccess: true
  publicAccessCIDRs: ["${API_ALLOWED_CIDR}"]

addons:
  - name: vpc-cni
  - name: coredns
  - name: kube-proxy
  - name: metrics-server

cloudWatch:
  clusterLogging:
    enableTypes: ["audit"]
    logRetentionInDays: 1

managedNodeGroups:
  # KServe controller, cert-manager, cluster autoscaler, metrics-server, drivers.
  - name: system
    instanceType: ${SYSTEM_INSTANCE_TYPE}
    minSize: 2
    maxSize: 2
    desiredCapacity: 2
    volumeSize: 30
    labels: { role: system }
    iam:
      instanceRoleARN: ${NODE_ROLE_ARN}
    disableIMDSv1: true
    disablePodIMDS: false
    propagateASGTags: true
    tags:
      Project: ${TAG_PROJECT}
      TestRun: ${TEST_RUN_ID}
      Owner: ${TAG_OWNER}
      Purpose: ${TAG_PURPOSE}
      ExpiresAt: "${TAG_EXPIRES_AT}"
      ManagedBy: eksctl
      NodeGroup: system

  # The page service only (tainted); the cluster autoscaler scales it.
  - name: parser
    instanceType: ${PARSER_INSTANCE_TYPE}
    minSize: ${PARSER_NODES_MIN}
    maxSize: ${PARSER_NODES_MAX}
    desiredCapacity: ${PARSER_NODES_MIN}
    volumeSize: 50
    labels: { role: parser }
    taints:
      - key: dedicated
        value: parser
        effect: NoSchedule
    iam:
      instanceRoleARN: ${NODE_ROLE_ARN}
    disableIMDSv1: true
    disablePodIMDS: false
    propagateASGTags: true
    tags:
      Project: ${TAG_PROJECT}
      TestRun: ${TEST_RUN_ID}
      Owner: ${TAG_OWNER}
      Purpose: ${TAG_PURPOSE}
      ExpiresAt: "${TAG_EXPIRES_AT}"
      ManagedBy: eksctl
      NodeGroup: parser
      k8s.io/cluster-autoscaler/enabled: "true"
      k8s.io/cluster-autoscaler/${CLUSTER_NAME}: owned
      k8s.io/cluster-autoscaler/node-template/label/role: parser
      k8s.io/cluster-autoscaler/node-template/taint/dedicated: "parser:NoSchedule"
