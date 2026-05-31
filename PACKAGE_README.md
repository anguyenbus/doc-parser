# doc-bench Package Distribution

Local evaluation framework for document parsing systems — pip-installable package with tiered dataset distribution (bundled fixtures + on-demand downloads).

## Quick Start

```bash
# Install package
pip install doc-bench

# Setup NLTK data (required for METEOR metric)
doc-bench-setup

# Run smoke test against bundled fixtures
doc-bench-smoke-test --predictions ./my_predictions.json

# Download full dataset (when needed)
doc-bench-download --dataset dp_bench --version 1.0.0

# List available datasets
doc-bench-list-datasets
```

## Installation

**Requirements:** Python 3.13+

```bash
# From internal PyPI/index
pip install doc-bench

# With optional dependencies
pip install doc-bench[docling]  # Includes docling parser stubs
pip install doc-bench[bedrock]   # Includes AWS Bedrock support
```

## Four-Step Workflow

### Step 1: Install
```bash
pip install doc-bench
doc-bench-setup  # Download NLTK data
```

### Step 2: Smoke Test (Fast, Offline)
```bash
# Test against bundled stratified fixtures (~22 docs)
doc-bench-smoke-test --predictions ./preds.json
```

**Smoke test output shows:**
- Pass/fail per document type (academic_literature, book, PPT2PDF, etc.)
- Pass/fail per element category (Header, Table, List, etc.)
- Clear exit code (0 = pass, non-zero = fail)

**Pass criteria:** Ran cleanly + schema-valid + <10% rejections (NOT quality thresholds)

### Step 3: Download Full Dataset (When Needed)
```bash
# See available datasets and versions
doc-bench-list-datasets

# Download specific version (required — no "latest")
doc-bench-download --dataset dp_bench --version 1.0.0
doc-bench-download --dataset omnidocbench --version 1.0.0

# Downloads to ~/.cache/doc-bench/<dataset>-<version>/
# Set DOC_BENCH_CACHE to override cache location
```

### Step 4: Evaluate with Full Dataset
```bash
# Evaluate against downloaded dataset
doc-bench --dataset dp_bench \
  --data ~/.cache/doc-bench/dp_bench-1.0.0 \
  --predictions ./preds.json \
  --output ./results.json
```

## Available Commands

| Command | Purpose |
|---------|---------|
| `doc-bench-setup` | Download NLTK data (wordnet, punkt, omw-1.4) |
| `doc-bench-smoke-test` | Quick validation test against bundled fixtures |
| `doc-bench-download` | Download version-pinned full datasets |
| `doc-bench-list-datasets` | Show available datasets and cache status |
| `doc-bench` | Full evaluation run |
| `doc-bench-dump-dataset` | Inspect dataset structure |

## Smoke Test Caveats

**Important:** The bundled fixture set (~22 docs: 10 DP-Bench + 12 OmniDocBench) is a **smoke test**, not a benchmark.

- **Purpose:** Catch obvious breakage and identify which document/element type failed
- **Coverage:** 2 docs per document type, 2 docs per element category (stratified sampling)
- **NOT for:** Quality conclusions or parser comparisons
- **Real numbers come from:** Full dataset run or Docker image in CI

**Why stratified?** Type coverage matters more than count for smoke tests. 2 docs × 6 types catches more failure modes than 20 docs of 1 type.

## Package vs Docker

| Use Case | Tool |
|----------|------|
| Local iteration | Package (pip install) |
| CI gate / reproducible runs | Docker image |

**Package:** Fast local iteration, instant smoke test, on-demand full datasets
**Docker:** Sealed environment, dependency isolation, authoritative CI results

Both use identical loaders, gold construction, and metrics — same scores, different delivery.

## Result Metadata

Every `results.json` includes:
```json
{
  "dataset_version": "1.0.0",
  "doc_bench_version": "0.1.0",
  "document_count": 10,
  "predictions_sha256": "abc123...",
  "timestamp": "2026-05-30T15:00:00Z"
}
```

Smoke test results labeled: `"bundled-smoke-stratified"`

## Dataset Versions

**Version pinning required:**
- `--dataset <name> --version <ver>` — explicit version only
- No "latest" keyword (reproducibility requirement)
- Cache keyed by version: `~/.cache/doc-bench/<dataset>-<version>/`

**Check available versions:**
```bash
doc-bench-list-datasets
```

## Known Issues

**TEDS/MHS metrics:** Not meaningful on OmniDocBench (gold lacks markdown tables/headings). Scores ~0 are expected — this is a gold-builder issue, not package-specific.

**METEOR returning 0:** Missing NLTK data. Run `doc-bench-setup` first.

## Troubleshooting

**"NLTK data missing" error:**
```bash
doc-bench-setup
```

**"Dataset version not found":**
```bash
doc-bench-list-datasets  # Check available versions
```

**Cache location:**
- Default: `~/.cache/doc-bench/`
- Override: `export DOC_BENCH_CACHE=/path/to/cache`

## Requirements

- Python 3.13+
- 500MB disk for cached datasets (per dataset version)
- NLTK data at `~/.cache/nltk_data/` (installed via `doc-bench-setup`)

## Support

For issues, dataset questions, or feature requests:
- Check baseline fixtures in `src/doc_bench/fixtures/`
- Run smoke test first: `doc-bench-smoke-test`
- Use Docker image for CI/reproducible runs
