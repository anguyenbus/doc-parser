#!/usr/bin/env python
"""End-to-end doc-parser evaluation orchestrator (wheel-based doc-bench).

Pipeline per dataset:  dump (doc-bench) -> parse (parser_service) -> grade
(doc-bench) -> compare (scripts/compare_to_baseline.py).

The parse step runs the markdown-first pipeline: ``parse_batch.py`` produces a
``.md`` per document and, because this orchestrator passes ``--emit-test-json``,
also writes the wrapped schema-valid prediction ``.json`` that doc-bench grades
(md -> wrapped JSON -> grade).

All grading is done by the installed `doc-bench` CLI (the source of truth); this
script only orchestrates and shells out to it, so it stays robust across wheel
versions. Install the grader once with:

    uv tool install --force ./doc_bench-0.1.0-py3-none-any.whl
    doc-bench-setup            # NLTK data for METEOR

Usage:
    uv run python scripts/run_eval.py                         # both datasets, full pipeline
    uv run python scripts/run_eval.py --dataset dp_bench      # one dataset
    uv run python scripts/run_eval.py --skip-parse            # reuse existing predictions
    uv run python scripts/run_eval.py --predictions DIR --dataset dp_bench
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "references/doc-bench/baseline"

DATASETS = {
    "dp_bench": BASELINE / "dp_bench/dpbench_results.json",
    "omnidocbench": BASELINE / "omnidocbench/omnidocbench_results.json",
}

# The grader reads ./eval_config.yaml from its CWD (fixed filename, no --config).
# dump-dataset still accepts --config. We use the one file for both.
CONFIG = ROOT / "eval_config.yaml"

# Bedrock defaults for the parse step (only set if not already in the env).
BEDROCK_DEFAULTS = {
    "AWS_REGION": "ap-southeast-2",
    "BEDROCK_VLM_MODEL": "anthropic.claude-3-5-sonnet-20241022-v2:0",
}


def run(cmd: list[str | Path], **kw: Any) -> subprocess.CompletedProcess[Any]:
    print(f"\n$ {' '.join(str(c) for c in cmd)}", flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def latest_results_csv(results_dir: Path, dataset: str) -> Path:
    matches = sorted(results_dir.glob(f"{dataset}_predictions_results_*.csv"))
    if not matches:
        raise FileNotFoundError(f"No results CSV in {results_dir}")
    return matches[-1]


def evaluate(dataset: str, args: argparse.Namespace) -> dict[str, Any]:
    work = Path(args.workdir) / dataset
    exported = work / "exported"
    predictions = Path(args.predictions) if args.predictions else work / "predictions"
    results = work / "results"
    results.mkdir(parents=True, exist_ok=True)

    doc_bench = shutil.which("doc-bench")
    if not doc_bench:
        sys.exit("ERROR: `doc-bench` not on PATH. Run: uv tool install --force ./doc_bench-*.whl")

    # 1) dump + 2) parse (unless reusing predictions)
    if not args.predictions and not args.skip_parse:
        if exported.exists():
            shutil.rmtree(exported)
        exported.mkdir(parents=True)
        dumper = shutil.which("doc-bench-dump-dataset")
        if not dumper:
            sys.exit("ERROR: `doc-bench-dump-dataset` not on PATH.")
        run(
            [
                dumper,
                "--dataset",
                dataset,
                "--output",
                exported,
                "--config",
                CONFIG,
            ]
        )

        env = {**os.environ, **{k: v for k, v in BEDROCK_DEFAULTS.items() if k not in os.environ}}
        env.setdefault("PARSER_LOG_LEVEL", "WARNING")
        if predictions.exists():
            shutil.rmtree(predictions)
        predictions.mkdir(parents=True)
        # --emit-test-json: parse_batch writes the .md PLUS the wrapped, schema-valid
        # prediction .json doc-bench grades (md -> wrapped JSON -> grade).
        run(
            [
                sys.executable,
                str(ROOT / "scripts/parse_batch.py"),
                "--input",
                exported,
                "--output",
                predictions,
                "--emit-test-json",
                "--concurrency",
                str(args.concurrency),
            ],
            env=env,
        )
    else:
        print(f"[{dataset}] reusing predictions at {predictions}")
        if not predictions.exists():
            sys.exit(f"ERROR: predictions dir not found: {predictions}")

    # 3) grade with the wheel (reads ./eval_config.yaml from CWD → run from ROOT)
    run(
        [doc_bench, "--dataset", dataset, "--predictions", predictions, "--output-dir", results],
        cwd=ROOT,
    )

    csv = latest_results_csv(results, dataset)
    summary = json.loads(csv.with_suffix(".json").read_text())
    avg = summary.get("metrics_avg", summary.get("averages", {}))

    # 4) compare to baseline (writes *_vs_baseline.{json,md})
    route = predictions / "route_stats.csv"
    cmp_cmd: list[str | Path] = [
        sys.executable,
        str(ROOT / "scripts/compare_to_baseline.py"),
        "--results",
        csv,
        "--baseline",
        DATASETS[dataset],
    ]
    if route.exists():
        cmp_cmd += ["--route-stats", route]
    run(cmp_cmd)

    return {
        "dataset": dataset,
        "evaluated": summary.get("evaluated_samples", summary.get("total_processed")),
        "nid": avg.get("nid"),
        "bleu": avg.get("bleu"),
        "ard": avg.get("ard"),
        "meteor": avg.get("meteor"),
        "csv": str(csv),
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dataset", choices=[*DATASETS, "all"], default="all")
    ap.add_argument(
        "--predictions",
        type=Path,
        default=None,
        help="Use an existing predictions dir (skips dump+parse). Single dataset only.",
    )
    ap.add_argument(
        "--skip-parse",
        action="store_true",
        help="Reuse predictions already under --workdir/<dataset>/predictions.",
    )
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--workdir", type=Path, default=ROOT / "docker_eval/run")
    args = ap.parse_args()

    targets = list(DATASETS) if args.dataset == "all" else [args.dataset]
    if args.predictions and len(targets) > 1:
        sys.exit("ERROR: --predictions can only be used with a single --dataset.")

    rows = [evaluate(ds, args) for ds in targets]

    print("\n" + "=" * 64)
    print(f"{'dataset':16}{'n':>4}{'NID':>9}{'BLEU':>9}{'ARD':>9}{'METEOR':>9}")
    print("-" * 64)
    for r in rows:

        def f(x: object) -> str:
            return f"{x:.4f}" if isinstance(x, (int, float)) else "  -  "

        print(
            f"{r['dataset']:16}{str(r['evaluated']):>4}"
            f"{f(r['nid']):>9}{f(r['bleu']):>9}{f(r['ard']):>9}{f(r['meteor']):>9}"
        )
    print("=" * 64)
    print("Per-dataset *_vs_baseline.md written next to each results CSV.")


if __name__ == "__main__":
    main()
