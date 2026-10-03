# The page service: one page per request, one page at a time per pod (429 when
# busy), HPA on CPU. Standard (raw Deployment) mode: no Knative, no Istio, no
# scale-to-zero (a cold start pulls a ~4 GB image and loads the models).
apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  name: page-parser
  namespace: ${NAMESPACE}
  labels:
    test-run: ${TEST_RUN_ID}
  annotations:
    serving.kserve.io/deploymentMode: Standard
    serving.kserve.io/autoscalerClass: hpa
spec:
  predictor:
    minReplicas: ${PREDICTOR_MIN_REPLICAS}
    maxReplicas: ${PREDICTOR_MAX_REPLICAS}
    autoScaling:
      metrics:
        - type: Resource
          resource:
            name: cpu
            target:
              type: Utilization
              averageUtilization: ${PREDICTOR_TARGET_CPU}
      behavior:
        scaleUp:
          stabilizationWindowSeconds: 0
          policies:
            - { type: Percent, value: 100, periodSeconds: 30 }
            - { type: Pods, value: 6, periodSeconds: 30 }
          selectPolicy: Max
        scaleDown:
          stabilizationWindowSeconds: 180
          policies:
            - { type: Percent, value: 50, periodSeconds: 60 }
    nodeSelector:
      role: parser
    tolerations:
      - key: dedicated
        operator: Equal
        value: parser
        effect: NoSchedule
    # Long enough to finish the page in hand on scale-in (the server's own
    # graceful-shutdown limit is PARSER_GRACE_PERIOD, 170 s).
    terminationGracePeriodSeconds: 180
    containers:
      - name: kserve-container
        image: ${IMAGE_URI}
        imagePullPolicy: IfNotPresent
        envFrom:
          - configMapRef:
              name: parser-config
        env:
          - { name: OMP_NUM_THREADS, value: "2" }
          - { name: PARSER_GRACE_PERIOD, value: "170" }
        ports:
          - containerPort: 8080
            protocol: TCP
        resources:
          requests: { cpu: "2", memory: 4Gi }
          limits: { cpu: "2", memory: 6Gi }
        # The server only starts listening after load() has parsed the warm-up
        # page, so ready means the models are loaded.
        readinessProbe:
          httpGet: { path: /v1/models/page-parser, port: 8080 }
          periodSeconds: 5
          failureThreshold: 2
        startupProbe:
          httpGet: { path: /v1/models/page-parser, port: 8080 }
          periodSeconds: 5
          failureThreshold: 60
        livenessProbe:
          httpGet: { path: /, port: 8080 }
          periodSeconds: 20
          timeoutSeconds: 5
          failureThreshold: 6
