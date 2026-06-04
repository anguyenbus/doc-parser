#!/usr/bin/env python
"""Compare doc-parser grade output against the baked Docling baseline.

Joins our per-document grader CSV against doc-bench's
`baseline/docling_baseline.json` (same image set, same order), then reports
per-page deltas, aggregate deltas, and paired statistical tests
(paired t-test, Wilcoxon signed-rank, 95% CI on the mean difference).

Writes `<results-stem>_vs_baseline.json` and `.md` next to the input CSV.

Usage:
    uv run python scripts/compare_to_baseline.py \
        --results eval_runs/omnidocbench/results/omnidocbench_predictions_results_<ts>.csv \
        --baseline references/doc-bench/baseline/docling_baseline.json \
        [--route-stats eval_runs/omnidocbench/predictions/route_stats.csv]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics as st
from pathlib import Path
from typing import Any, cast

from scipy import stats

# Metrics to compare. "higher" = higher is better, "lower" = lower is better.
# METEOR is included: the doc-bench wheel (after `doc-bench-setup`) computes it
# correctly, so it is comparable to the baseline. (The old Docker image lacked
# NLTK omw-1.4 and reported 0 — not the case with the wheel.)
METRICS = {"nid": "higher", "bleu": "higher", "ard": "lower", "meteor": "higher"}


def load_baseline(path: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = json.loads(path.read_text())["results"]
    return results


def load_ours(path: Path) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], list(csv.DictReader(path.open())))


def load_routes(path: Path | None) -> dict[str, str]:
    if not path or not path.exists():
        return {}
    return {r["doc_id"]: r["route"] for r in csv.DictReader(path.open())}


def paired_stats(ours: list[float], base: list[float], higher_better: bool) -> dict[str, Any]:
    diffs = [o - b for o, b in zip(ours, base, strict=True)]
    mean_d = st.mean(diffs)
    sd = st.stdev(diffs) if len(diffs) > 1 else 0.0
    n = len(diffs)
    se = sd / math.sqrt(n) if n else 0.0
    tcrit = stats.t.ppf(0.975, n - 1) if n > 1 else float("nan")

    _t_stat, p_t = stats.ttest_rel(ours, base)
    try:
        _w_stat, p_w = stats.wilcoxon(ours, base)
    except ValueError:
        # all diffs zero -> Wilcoxon undefined
        p_w = 1.0

    wins = sum(1 for d in diffs if abs(d) > 1e-9 and ((d > 0) == higher_better))
    return {
        "n": n,
        "mean_delta": round(mean_d, 4),
        "sd_delta": round(sd, 4),
        "ci95": [round(mean_d - tcrit * se, 4), round(mean_d + tcrit * se, 4)],
        "paired_t_p": round(float(p_t), 4),
        "wilcoxon_p": round(float(p_w), 4),
        "nonzero_diffs": sum(1 for d in diffs if abs(d) > 1e-9),
        "ours_wins": wins,
        "significant_raw": bool(min(p_t, p_w) < 0.05),
        "significant_bonferroni": bool(min(p_t, p_w) < 0.05 / len(METRICS)),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, type=Path)
    ap.add_argument("--baseline", required=True, type=Path)
    ap.add_argument("--route-stats", type=Path, default=None)
    args = ap.parse_args()

    base = load_baseline(args.baseline)
    ours = load_ours(args.results)
    routes = load_routes(args.route_stats)

    if len(base) != len(ours):
        raise SystemExit(
            f"row count mismatch: baseline={len(base)} ours={len(ours)} "
            "(dump/grade must cover the same image set)"
        )

    per_page: list[dict[str, Any]] = []
    series: dict[str, dict[str, list[float]]] = {m: {"ours": [], "base": []} for m in METRICS}
    for o, b in zip(ours, base, strict=True):
        # OmniDocBench baselines key the source as "image"; DP-Bench as "pdf".
        img = b.get("image") or b.get("pdf") or b.get("doc_id", "")
        doc_id = img.rsplit(".", 1)[0]
        row = {"doc": img, "route": routes.get(doc_id, "?")}
        for m in METRICS:
            ov, bv = float(o[m]), float(b[m])
            series[m]["ours"].append(ov)
            series[m]["base"].append(bv)
            row[m] = {"ours": round(ov, 4), "base": round(bv, 4), "delta": round(ov - bv, 4)}
        per_page.append(row)

    aggregate = {}
    stats_out = {}
    for m, direction in METRICS.items():
        o_avg = st.mean(series[m]["ours"])
        b_avg = st.mean(series[m]["base"])
        aggregate[m] = {
            "ours": round(o_avg, 4),
            "base": round(b_avg, 4),
            "delta": round(o_avg - b_avg, 4),
            "better": direction,
        }
        stats_out[m] = paired_stats(
            series[m]["ours"], series[m]["base"], higher_better=(direction == "higher")
        )

    notes = [
        "METEOR included (doc-bench wheel computes it after `doc-bench-setup`).",
        f"Bonferroni alpha = 0.05/{len(METRICS)} = {0.05 / len(METRICS):.4f} across the {len(METRICS)} metrics.",
    ]
    report = {
        "results_csv": str(args.results),
        "baseline": str(args.baseline),
        "n_pages": len(ours),
        "aggregate": aggregate,
        "statistics": stats_out,
        "per_page": per_page,
        "notes": notes,
    }

    out_json = args.results.with_name(args.results.stem + "_vs_baseline.json")
    out_md = args.results.with_name(args.results.stem + "_vs_baseline.md")
    out_json.write_text(json.dumps(report, indent=2))

    # Markdown
    lines = [
        f"# doc-parser vs Docling baseline ({len(ours)} pages)",
        "",
        "## Aggregate",
        "",
        "| Metric | ours | baseline | Δ | better |",
        "|---|---|---|---|---|",
    ]
    for m, a in aggregate.items():
        lines.append(
            f"| {m.upper()} | {a['ours']:.4f} | {a['base']:.4f} | {a['delta']:+.4f} | {a['better']} |"
        )
    lines += [
        "",
        f"## Statistical significance (paired, n={len(ours)})",
        "",
        "| Metric | mean Δ | 95% CI | paired t p | Wilcoxon p | sig (raw) | sig (Bonferroni) |",
        "|---|---|---|---|---|---|---|",
    ]
    for m, s in stats_out.items():
        lines.append(
            f"| {m.upper()} | {s['mean_delta']:+.4f} | "
            f"[{s['ci95'][0]:+.4f}, {s['ci95'][1]:+.4f}] | "
            f"{s['paired_t_p']:.3f} | {s['wilcoxon_p']:.3f} | "
            f"{'yes' if s['significant_raw'] else 'no'} | "
            f"{'yes' if s['significant_bonferroni'] else 'no'} |"
        )
    lines += [
        "",
        "## Per-page",
        "",
        "| # | doc | route | NID Δ | BLEU Δ | ARD Δ |",
        "|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(per_page):
        lines.append(
            f"| {i} | {r['doc']} | {r['route']} | "
            f"{r['nid']['delta']:+.3f} | {r['bleu']['delta']:+.3f} | {r['ard']['delta']:+.3f} |"
        )
    lines += ["", "## Notes", ""] + [f"- {n}" for n in notes] + [""]
    out_md.write_text("\n".join(lines))

    print("\n".join(lines))
    print(f"\nWrote {out_json}")
    print(f"Wrote {out_md}")


if __name__ == "__main__":
    main()
