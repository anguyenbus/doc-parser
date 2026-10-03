# The driver: one pod per PDF of the bundle (an Indexed Job). Each pod splits its
# PDF into the store, sends the pages to the page service, rebuilds the status
# and stitches. It runs inside the cluster on purpose: a port-forward from
# outside would reach a single page-service pod and defeat the scaling.
# Exit 0 = complete; 2 = not complete (status.json says why); 1 = bad input.
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
  labels:
    test-run: ${TEST_RUN_ID}
    app: parse-driver
spec:
  completionMode: Indexed
  completions: ${JOB_COMPLETIONS}
  parallelism: ${JOB_COMPLETIONS}
  backoffLimitPerIndex: 2
  maxFailedIndexes: ${JOB_COMPLETIONS}
  # A crash or a killed pod (any other exit) is retried: the new pod resumes the
  # file, skipping pages already ok. Exit 1 (bad input or settings) and exit 2
  # (the file finished not complete; status.json says why) are final answers.
  podFailurePolicy:
    rules:
      - action: FailIndex
        onExitCodes: { containerName: driver, operator: In, values: [1, 2] }
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels:
        test-run: ${TEST_RUN_ID}
        app: parse-driver
    spec:
      restartPolicy: Never
      nodeSelector:
        role: system
      containers:
        - name: driver
          image: ${IMAGE_URI}
          imagePullPolicy: IfNotPresent
          command: ["python", "scripts/parse_pages.py"]
          args:
            - --input-list=${INPUT_LIST}
            - --executor=kserve
            - --endpoint=http://page-parser-predictor.${NAMESPACE}.svc.cluster.local
            - --concurrency=${DRIVER_CONCURRENCY}
            ${DRIVER_EXTRA_ARGS}
          envFrom:
            - configMapRef:
                name: parser-config
          resources:
            requests: { cpu: 200m, memory: 768Mi }
            limits: { memory: 2Gi }
