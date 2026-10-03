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
- The page service is `parser_service.kserve_predictor`.
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
| `scripts/40_capture_evidence.sh stop` / `dump` | `timeline.png`; cluster, HPA, autoscaler, ASG and EC2 snapshots |
| `scripts/90_down.sh [--delete-s3]` | Delete the cluster, ECR repo and log group (each checked for the tag) |
| `scripts/99_verify_teardown.sh` | Read-only proof that nothing tagged or named for the test remains |

Evidence lands in `results/kserve_scale_<date>/`.

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
