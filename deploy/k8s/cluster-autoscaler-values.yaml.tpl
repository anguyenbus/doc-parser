# Cluster Autoscaler (helm chart autoscaler/cluster-autoscaler). It uses the node
# role's credentials (no IRSA in this account) and discovers the parser node
# group's ASG by its k8s.io/cluster-autoscaler tags. Scale-in timers are shorter
# than the defaults so the test shows nodes leaving within its window.
autoDiscovery:
  clusterName: ${CLUSTER_NAME}
awsRegion: ${AWS_REGION}
image:
  repository: registry.k8s.io/autoscaling/cluster-autoscaler
  tag: ${CLUSTER_AUTOSCALER_TAG}
nodeSelector:
  role: system
extraArgs:
  expander: least-waste
  balance-similar-node-groups: "true"
  skip-nodes-with-system-pods: "false"
  scale-down-unneeded-time: 3m
  scale-down-delay-after-add: 3m
  max-node-provision-time: 10m
