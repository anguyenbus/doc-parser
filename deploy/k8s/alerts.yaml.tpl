# Alert rules for the page pipeline (spec D9). NOT a Kubernetes manifest, and not
# applied by any deploy step: the cluster has no Prometheus, Loki or log shipping
# today, so nothing evaluates these rules yet (spec Risks). Load the rendered
# file into a Loki ruler (rule-group format) once the driver's logs are shipped.
#
# ParserKindFailureRateHigh is computed from the driver's `file_done` events (one
# JSON object per log line, after the "<time> <level> <logger>: " prefix), so
# files refused at intake count. `kind` is the routed kind, or `unsupported`.
# Per kind: the failed share of finished files over 1 h is above 10%, with at
# least 20 finished files, for 15 min.
#
# The stream selector assumes the shipper's usual Kubernetes labels (`namespace`,
# `container`); the driver's container is named `driver` (driver-job.yaml.tpl).
# Render like the other templates (envsubst with NAMESPACE set).
groups:
  - name: parser-driver
    rules:
      - alert: ParserKindFailureRateHigh
        expr: |
          (
            sum by (kind) (count_over_time({namespace="${NAMESPACE}", container="driver"} |= "\"file_done\"" | regexp "(?P<event_json>\\{.*\\})" | line_format "{{.event_json}}" | json | event="file_done" | state="failed" [1h]))
            /
            sum by (kind) (count_over_time({namespace="${NAMESPACE}", container="driver"} |= "\"file_done\"" | regexp "(?P<event_json>\\{.*\\})" | line_format "{{.event_json}}" | json | event="file_done" [1h]))
          ) > 0.10
          and
          sum by (kind) (count_over_time({namespace="${NAMESPACE}", container="driver"} |= "\"file_done\"" | regexp "(?P<event_json>\\{.*\\})" | line_format "{{.event_json}}" | json | event="file_done" [1h])) >= 20
        for: 15m
        labels:
          severity: warning
          namespace: ${NAMESPACE}
        annotations:
          summary: "More than 10% of {{ .Labels.kind }} files failed in the last hour"
          description: >-
            Over the last hour more than 10% of the finished {{ .Labels.kind }} files
            (at least 20) ended failed, intake rejections included. Check the
            driver's file_done events for their reason_code. To stop a new kind,
            remove it from PARSER_ENABLED_KINDS (the kill switch in deploy/README.md).
