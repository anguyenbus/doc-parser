apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
  labels:
    test-run: ${TEST_RUN_ID}
---
# Shared by the page service and the driver. The pipeline config pinned in each
# file's manifest at intake is built from these values, and the page service
# refuses a page whose file was split under another config (HTTP 409), so both
# sides read the same ConfigMap.
apiVersion: v1
kind: ConfigMap
metadata:
  name: parser-config
  namespace: ${NAMESPACE}
data:
  AWS_REGION: ${AWS_REGION}
  AWS_DEFAULT_REGION: ${AWS_REGION}
  PARSER_STORE_LOCATION: ${STORE_LOCATION}
  BEDROCK_VLM_MODEL: ${BEDROCK_VLM_MODEL}
  PARSER_ESCALATION_ENGINE: vlm
  PARSER_PAGE_MAX_ATTEMPTS: "3"
  PARSER_LOG_LEVEL: INFO
