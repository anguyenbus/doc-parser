# deploy/ — KServe-on-EKS scale test for the page pipeline

This folder sets up a disposable EKS cluster. On it, the split-first page pipeline
parses pages through a KServe InferenceService, and the page service autoscales.
Every AWS resource is named from `TEST_RUN_ID` in [test.env](test.env) and tagged
`TestRun=<TEST_RUN_ID>`. The teardown removes all of it and proves it is gone.

## How a run works

```
driver (Indexed Job, 1 pod per PDF)                page-parser InferenceService (Standard mode)
  split the PDF into the S3 store   -- POST page ->  1 page per pod (429 when busy)
  bounded retries, status, stitch                    writes the page record to the same store
                                                     HPA on CPU; Cluster Autoscaler adds nodes
```

- The driver is `scripts/parse_pages.py --executor kserve`.
- The page service is `parser_service.service.predictor`. It parses in one
  long-lived worker process per pod (`parser_service.service.worker`); Docling loads
  only there.
  - A page with no result after `PARSER_DEADLINE_PDF_PAGE_S` gets its worker
    killed and one final `failed` record with `reason_code` `timeout` (200).
  - A worker that dies mid-page (for example OOM) writes no record (500). The
    driver sends the page again, or finalizes it at the attempt limit.
  - While a new worker warms up, the pod is not ready and answers 503. The driver
    treats that as busy, at no attempt cost. Three failed worker starts in a row
    make the service exit, and the kubelet restarts the container.
  - `/metrics` adds `parser_worker_restarts_total{cause}` (`timeout`, `died`,
    `start_failed`), `parser_timeouts_total{kind}`, `parser_units_total`,
    `parser_flags_total`, `parser_guard_rejections_total`, and the
    `parser_parse_seconds`, `parser_input_bytes` and `parser_output_bytes`
    histograms (`parser_service/observability.py`). The logs add the JSON events
    `unit_done`, `guard_reject`, `unit_timeout` and `worker_restart`; the driver
    logs `file_intake`, `file_done` and `guard_reject` (`too_large`).
  - [k8s/alerts.yaml.tpl](k8s/alerts.yaml.tpl) holds the `ParserKindFailureRateHigh`
    rule (LogQL over the driver's `file_done` events). Nothing applies or evaluates
    it: the cluster has no Prometheus, Loki or log shipping.
  - The driver's `PARSER_REMOTE_REQUEST_TIMEOUT_S` must be at least the largest
    deadline of an enabled kind plus 120 s; both sides refuse to start otherwise
    (see [Deadline ordering](#deadline-ordering)).
- Both run from the same image, built by [docker/Dockerfile](docker/Dockerfile), and
  read the same ConfigMap. The service refuses (409) any page whose file was split
  under a different pipeline config.

## Steps

| Script | What it does |
|---|---|
| `scripts/00_preflight.sh` | Read-only checks: tools, account, quota, disk, API allow-list |
| `scripts/10_build_push.sh` | Tagged ECR repo; build the image (CPU torch, models baked in); push |
| `scripts/20_up.sh` | eksctl cluster plus node groups; cert-manager, KServe v0.21 (Standard), Cluster Autoscaler; the InferenceService |
| `scripts/40_capture_evidence.sh start` | 15 s sampler writing `timeline.csv` |
| `scripts/30_run_test.sh T1` | Correctness: `sample_mixed_10.pdf`, compared page by page with the reference run |
| `scripts/30_run_test.sh T2` | Scale: 10 × 107 pages |
| `scripts/30_run_test.sh T3` | Two page-service pods deleted mid-run |
| `scripts/30_run_test.sh T4` | The driver pod deleted mid-file; the Job resumes it |
| `scripts/30_run_test.sh T5` | Formats: one file each of `html`, `docx`, a 10-page `pdf` and `txt` (`scripts/make_demo_inputs.py`), compared with a local run of the same files; each unit's pod recorded. Needs those kinds enabled at bring-up. `eml` and `msg` are not in it yet |
| `scripts/demo_e2e.sh` | The narrated T5 run with a live view, recorded for the demo video (`asciinema rec -c scripts/demo_e2e.sh`) |
| `scripts/30_run_test.sh T6` | Planned. The bomb fixtures and a forced timeout per kind, with lowered deadlines in the test namespace: the expected codes, one record each, a working pod afterwards |
| `scripts/30_run_test.sh T7` | Planned. A small golden set per kind from the real corpus (never committed); email table flattening; the `PARSER_IMAGES_MIN_PIXELS` check |
| `scripts/40_capture_evidence.sh stop` / `dump` | `timeline.png`; cluster, HPA, autoscaler, ASG and EC2 snapshots |
| `scripts/90_down.sh [--delete-s3]` | Delete the cluster, ECR repo and log group (each checked for the tag) |
| `scripts/99_verify_teardown.sh` | Read-only proof that nothing tagged or named for the test remains |

Evidence lands in `results/<EVIDENCE_DIR>/` (set in `test.env`).

The `.sh` scripts were recovered on 2026-10-14 from the session that wrote and ran
them on 2026-10-03, with every later edit replayed. `30_run_test.sh` was then
adapted to the content-keyed store: each input is a copy stamped with its own
`/TestCopy` metadata entry, so copies are really parsed instead of sharing one work
id, and progress is found by each copy's sha256. T2 to T4 need the 107-page PDF,
which is not in git: set `BIG_PDF`, or copy it from the 2026-10-03 run's
`input/source/` prefix in S3. T6 and T7 are not written yet.

`test.env` enables every kind with a tested handler (`pdf,docx,html,txt,eml,msg`).
T5 checks the live ConfigMap and refuses to start without `txt`, `docx` and `html`.

## Runbook

### Configuration keys

All keys live in the `parser-config` ConfigMap
([k8s/namespace.yaml.tpl](k8s/namespace.yaml.tpl)). The page service and the
driver read it through `envFrom`, so a change reaches a pod only when it starts.
After any edit, restart the page service and start new drivers. Running drivers
keep the old values.

`load_settings` (`parser_service/settings.py`) checks every key at start, in both
the driver and the service. Numbers must be integers of 1 or more. Kinds must be
known, and each must have a handler and a deadline in the image. An invalid value
stops the start: the driver exits 1, the service container restarts in a loop.

MB means 1,000,000 bytes. Values marked * are provisional (see
[Provisional limits](#provisional-limits-and-the-corpus-inventory)).

| Key | Start | Unit | Read by |
|---|---|---|---|
| `PARSER_ENABLED_KINDS` | `pdf,docx,html,txt,eml,msg` | comma list of `pdf`, `txt`, `docx`, `html`, `eml`, `msg` | driver (intake); service (409 for other kinds) |
| `PARSER_DEADLINE_PDF_PAGE_S` | 600 * | s, one PDF page | service (kills the parse worker); driver (start check, final rule) |
| `PARSER_DEADLINE_TXT_S` | 60 * | s, one document | same |
| `PARSER_DEADLINE_DOCX_S` | 300 * | s, one document | same |
| `PARSER_DEADLINE_HTML_S` | 300 * | s, one document | same |
| `PARSER_DEADLINE_EMAIL_S` | 120 * | s, one document (`eml` and `msg`) | same |
| `PARSER_REMOTE_REQUEST_TIMEOUT_S` | 900 | s, one page request | driver (request timeout); service (start check) |
| `PARSER_WORKER_START_TIMEOUT_S` | 300 | s, one worker start | service |
| `PARSER_MAX_TXT_MB` | 50 * | MB, raw file | driver (intake, `too_large`) |
| `PARSER_MAX_DOCX_MB` | 100 * | MB, raw file | same |
| `PARSER_MAX_HTML_MB` | 50 * | MB, raw file | same |
| `PARSER_MAX_EMAIL_MB` | 50 * | MB, raw file (`eml` and `msg`) | same |
| `PARSER_MAX_OUTPUT_MB` | 20 * | MB, one document's markdown | service (sends it with each request; the worker enforces it, `output_too_large`); driver (final rule). PDF pages have no output cap |
| `PARSER_DOCX_MAX_EXPANDED_MB` | 300 * | MB, sum of expanded ZIP entries | service (DOCX guard, `zip_bomb`); driver (final rule) |
| `PARSER_DOCX_MAX_PART_MB` | 200 * | MB, largest expanded entry | same |
| `PARSER_DOCX_MAX_RATIO` | 100 * | expanded:compressed, overall and per entry of 1 MiB or more | same |
| `PARSER_DOCX_MAX_ENTRIES` | 10000 * | ZIP entries | same |
| `PARSER_DOCX_MAX_PARAGRAPHS` | 50000 * | paragraphs in `word/document.xml` | service (DOCX guard, `too_complex`); driver (final rule) |
| `PARSER_DOCX_MAX_TABLE_CELLS` | 100000 * | table cells in `word/document.xml` | same |
| `PARSER_IMAGES_MIN_PIXELS` | 40000 | pixels (width x height) | parse worker (`images_not_ocrd` flag; never changes a unit's state) |

Every key is validated at start, but a kind's deadline, caps and guards are used
only once the kind is enabled. The start check refuses an enabled kind with no
handler in the image, so a kind cannot be turned on before its code ships.

### Deadline ordering

`PARSER_REMOTE_REQUEST_TIMEOUT_S` must be at least the largest deadline of an
enabled kind plus 120 s. The 120 s cover the source download, the guards, the
kill of a timed-out worker and the record write. Both the driver and the service
refuse to start otherwise, and the error names the key and the minimum.

- With the start values the largest deadline is the PDF page's 600 s, so the
  timeout must be at least 720. 900 holds.
- Raise a deadline and the timeout in the same ConfigMap edit.
- Only enabled kinds count. A disabled kind's deadline can break the rule
  unnoticed; it then stops the start on the day the kind is enabled.
- A deadline above the 180 s termination grace means a scale-in can kill a parse
  in progress. That takes the crash path: no record, the page is sent again.

### Limits and final records

- A unit that fails with `reason_code` `timeout`, `zip_bomb`, `too_complex` or
  `output_too_large` (or `unsupported_type` from the TXT handler on binary input)
  is final. Its record stores the `limits` behind it. A re-run skips it, and
  `status.json` shows `retryable: false`.
- Change a limit (deadline, output cap or DOCX guard), restart the page service
  and start new drivers. On the next run of the file the stored `limits` no longer
  match, so the record is archived and the unit parses again, under the same
  attempt counter. Nothing else is needed. Any change releases it, not only a
  raise.
- If the unit's attempt counter is already at `PARSER_PAGE_MAX_ATTEMPTS` (3), the
  record stays and the status says "attempt limit reached". Run the driver with
  `--restart` for that file. `--restart` deletes all of the file's `work/` and
  `out/`, so every name with the same bytes starts again.
- The TXT handler's `unsupported_type` stores empty `limits`: no limit change
  releases it.
- Raw caps (`too_large`) and the allow-list are decided at intake on every run and
  write no record. A raised cap takes effect on the next run.
- Limits are not in the pipeline config. Changing one never re-parses `ok` work.

### Enabled kinds

Every kind with a tested handler ships enabled: `pdf`, `docx`, `html`, `txt`, `eml`
and `msg`. The list is explicit, not taken from Docling: a format Docling reads but
the parser has no handler for (PPTX, XLSX, images, Markdown, audio, video) cannot be
enabled, because the driver and the service refuse to start with it. The * limits
are provisional for every enabled kind until the corpus inventory replaces them.

To change the list (to switch a kind back on after the kill switch, below):

1. Set the new list in the environment and apply the ConfigMap
   ([k8s/namespace.yaml.tpl](k8s/namespace.yaml.tpl) reads `${PARSER_ENABLED_KINDS}`,
   and `test.env` defaults it to every handled kind), for example:
   `export PARSER_ENABLED_KINDS=pdf,docx,html,txt,eml,msg; source test.env; envsubst < k8s/namespace.yaml.tpl | kubectl apply -f -`.
2. Restart the page service and wait for it:
   `kubectl -n $NAMESPACE rollout restart deployment/page-parser-predictor`, then
   `kubectl -n $NAMESPACE rollout status deployment/page-parser-predictor`.
   KServe Standard mode names the Deployment `<InferenceService>-predictor`;
   check it with `kubectl -n $NAMESPACE get deploy`.
3. Start new drivers.
4. Watch the signals below for the new kind.

The service goes first, so its pods accept the kind before any driver sends it.
A driver started earlier still refuses the kind at intake.

### Kill switch

`PARSER_ENABLED_KINDS` is the kill switch of a kind. To switch one off:

1. Remove the kind from `PARSER_ENABLED_KINDS` and apply the ConfigMap.
2. Restart the page service (as above). Its new pods refuse that kind with 409.
   No attempt is counted. A driver still running on such a file ends with
   `error` (exit 1; the Job does not retry it).
3. Drivers started from then on fail that kind's files at intake with
   `unsupported_type`. They write nothing under `work/`; existing work is left as
   is. `out/<file id>/status.json` says `failed`, `work_id: null`, `retryable:
   false`, and the driver exits 2. With publishing on, a `failed` event is
   written.

No other kind is affected. To undo it, add the kind back, restart the page
service and re-run the job. The files then parse, with no stale records: intake
wrote no work, and a 409 counted no attempt.

Rolling back the code itself means deploying the previous image. There is no flag.

### What to watch

After each step (a deploy, an enabled kind, a changed limit), watch the new kind
for at least an hour of traffic. Nothing scrapes, ships or alerts in this cluster,
so check by hand:

- Metrics, per page-service pod:
  `kubectl -n $NAMESPACE port-forward pod/<pod> 8080:8080`, then
  `curl -s localhost:8080/metrics | grep '^parser_'`.
- Service events:
  `kubectl -n $NAMESPACE logs -l serving.kserve.io/inferenceservice=page-parser -c kserve-container | grep -E '"(unit_done|guard_reject|unit_timeout|worker_restart)"'`.
- Driver events:
  `kubectl -n $NAMESPACE logs -l app=parse-driver -c driver | grep -E '"(file_intake|file_done|guard_reject)"'`.
- Pod memory: `kubectl -n $NAMESPACE top pod`. It must stay under the 6 Gi limit.

| Signal | Where | Look for |
|---|---|---|
| `file_done` event (`kind`, `state`, `reason_code`) | driver log | The failed share of finished files per kind. Over 10% of at least 20 files in an hour is what `ParserKindFailureRateHigh` means |
| `parser_units_total{kind,state,reason_code}` | service | Which codes the failures carry |
| `parser_timeouts_total{kind}`, `unit_timeout` event | service | Units at the deadline. A steady rate means the deadline is too low, or a class of file is pathological |
| `parser_parse_seconds{kind}` | service | Parse times against the kind's deadline |
| `parser_guard_rejections_total{kind,reason}`, `guard_reject` event | service (`zip_bomb`, `too_complex`, `output_too_large`); driver (`too_large`) | Rejections well above the inventory's share mean a limit is too tight |
| `parser_worker_restarts_total{cause}`, `worker_restart` event | service | `timeout` follows each timeout. `died` (for example OOM) and `start_failed` need a look |
| `parser_flags_total{kind,flag}` | service | The `images_not_ocrd` share |
| `parser_input_bytes`, `parser_output_bytes` | service | Sizes against the caps |

Switch the kind off (kill switch) when its failed share stays above 10%, or when
`died` restarts or memory climb with it. The alert rule in
[k8s/alerts.yaml.tpl](k8s/alerts.yaml.tpl) states the same threshold for the day a
log stack evaluates it.

### Releases that change a config hash: drain first

Each file's pipeline config is pinned in its manifest at intake, and its hash is
part of the work id. The page service refuses (409) a page split under another
config, so a driver running across such a deploy ends with `error`. These
releases change a hash:

- sub-step 3 (the PDF config gains `kind` and `handler_version`; the PDF hash
  changes once);
- any `HANDLER_VERSIONS` bump in `parser_service/formats/handlers.py` (that kind's
  hash only);
- a new parser or Docling version;
- for PDF only, a change of `PARSER_ESCALATION_ENGINE`, `BEDROCK_VLM_MODEL`,
  `PARSER_SCAN_FASTPATH` or `PARSER_ESCALATION_ARBITRATION`.

Steps:

1. Stop starting drivers.
2. Wait until the running driver Jobs finish: `kubectl -n $NAMESPACE get jobs`.
3. Deploy the image or the ConfigMap, and restart the page service.
4. Start new drivers.

Files in progress start again under the new hash. A file that was already
complete is parsed again when it is resubmitted.

### Release gate: `schema_version`

Sub-step 3 adds `kind`, `unit`, `flags` and `handler_version` to `document.json`
and every publish event, and `email` to `document.json` for `eml` and `msg`.
`SCHEMA_VERSION` stays 1 (`parser_service/delivery/publish.py`).

Before that release publishes to real consumers, someone must answer the open
question in the spec: do consumers ignore unknown fields? If they do not, the bump
to `SCHEMA_VERSION = 2` lands in the same release. The question is still open.
The test cluster's store is not a real consumer.

### Provisional limits and the corpus inventory

The * values are starting values. The corpus inventory replaces them before each
kind is enabled. Run it from this machine (read-only, `ap-southeast-2` only):

```
AWS_REGION=ap-southeast-2 uv run python scripts/corpus_inventory.py \
    --prefix s3://<corpus prefix> --out-dir <report dir> \
    --time-sample K [--max-files N] --csv <per-file CSV outside the repo>
```

It writes `inventory.json` and `inventory.md`: counts by detected type and
extension, size p50/p99/max per kind, the DOCX index and element-count
percentiles, and with `--time-sample` the parse wall time and peak RSS of K files
per kind. Set from it:

- the raw caps (`PARSER_MAX_*_MB`) from the size per kind;
- the DOCX guards (`PARSER_DOCX_MAX_*`) from the DOCX percentiles;
- the deadlines (`PARSER_DEADLINE_*`) from the timed sample, then the request
  timeout to keep the deadline ordering;
- `PARSER_MAX_OUTPUT_MB`.

Put the values in the ConfigMap and in the spec's Contracts table. The prefix of
the real corpus is not yet known, so every * value is still provisional.

## Constraints of this account, and what production needs instead

- **IAM roles.** IAM roles cannot be created here, so the cluster uses the pre-made
  EKS cluster role. The nodes use the dev box's instance role, and pods reach it
  through IMDS (hop limit 2).
  - Production: dedicated least-privilege roles through EKS Pod Identity.
- **Subnets.** The default VPC's private subnets have no NAT, so the nodes run in the
  public subnets.
  - Inbound traffic is closed: there is no load balancer and no ingress.
  - The API endpoint's public side admits only the dev box.
- **Node scaling.** Cluster Autoscaler runs instead of Karpenter, because Karpenter
  needs its own IAM role.
- **KServe charts.** KServe v0.21.0's charts are installed from the release's own
  tarballs, checksum-pinned in `test.env`. They were never pushed to ghcr.io as
  v0.21.0.
