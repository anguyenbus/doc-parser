#!/usr/bin/env python
"""Aggregate run_model_compare.sh outputs into a per-model scorecard (Markdown).

Compares VLM escalation across Claude models (plus Textract as a fixed reference)
on the bundled datasets that actually escalate. Reuses the same NED/TEDS CSVs and
file_parsed latency log lines as aggregate_benchmark.py, but pivots on the model
label (predictions_<label> / results_<label> / parse_<label>.log) instead of engine.
"""
from __future__ import annotations

import csv
import glob
import json
import sys
from pathlib import Path
from statistics import mean

METRICS = ["ned", "teds"]
COLUMNS = {"ned": ("ned_similarity", "ned"), "teds": ("teds",)}
# Display order; labels not present in a dataset are skipped.
LABELS = ["vlm_sonnet-3-5", "vlm_sonnet-4-6", "vlm_haiku-4-5", "textract"]
PRETTY = {
    "vlm_sonnet-3-5": "VLM Sonnet 3.5",
    "vlm_sonnet-4-6": "VLM Sonnet 4.6",
    "vlm_haiku-4-5": "VLM Haiku 4.5",
    "textract": "Textract (ref)",
}


def _cell(row: dict, *names: str) -> float | None:
    for n in names:
        v = row.get(n)
        if v not in (None, "", "nan"):
            return float(v)
    return None


def load_results(results_dir: Path) -> dict[str, dict]:
    files = sorted(glob.glob(str(results_dir / "*results*.csv")))
    if not files:
        return {}
    out: dict[str, dict] = {}
    with open(files[-1]) as f:
        for row in csv.DictReader(f):
            out[row["query_id"]] = {m: _cell(row, *COLUMNS[m]) for m in METRICS}
    return out


def load_routes(pred_dir: Path) -> dict[str, str]:
    p = pred_dir / "route_stats.csv"
    if not p.exists():
        return {}
    with open(p) as f:
        return {row["doc_id"]: row["route"] for row in csv.DictReader(f)}


def load_latency(log_path: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    if not log_path.exists():
        return out
    for line in log_path.read_text().splitlines():
        i = line.find('{"event"')
        if i == -1:
            continue
        try:
            rec = json.loads(line[i:])
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "file_parsed":
            out[Path(rec["filename"]).stem] = rec.get("parse_duration_s")
    return out


def fmt(v, nd=4):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "—"


def main() -> None:
    work = Path(sys.argv[1])
    datasets = sorted(d.name for d in work.iterdir() if d.is_dir())

    print("# doc-parser — VLM model comparison (escalation engine)\n")
    print("NED + TEDS + parse latency for the VLM escalation engine across Claude "
          "models, with Textract as a fixed (model-independent) reference. Only "
          "gate-promoted pages differ between models.\n")
    print("- Grader: doc-bench wheel (NED/TEDS). Region: ap-southeast-2, temperature 0.")
    print("- **NED** = normalized edit-distance text similarity (↑). **TEDS** = table "
          "structure similarity (↑; 0 when the gold has no scored table).")
    print("- Latency = full end-to-end parse (Docling + escalation), concurrency 4.")
    print("- omnidocbench is omitted: 0/5 pages escalate, so all models are identical there.\n")

    for ds in datasets:
        dsd = work / ds
        present = [
            lab for lab in LABELS
            if sorted(glob.glob(str(dsd / f"results_{lab}" / "*results*.csv")))
        ]
        if not present:
            continue
        data = {
            lab: {
                "res": load_results(dsd / f"results_{lab}"),
                "route": load_routes(dsd / f"predictions_{lab}"),
                "lat": load_latency(dsd / f"parse_{lab}.log"),
            }
            for lab in present
        }
        doc_ids = sorted({d for lab in present for d in data[lab]["res"]})

        print(f"\n## {ds}\n")

        # Per-doc NED (the docs that escalate are where models diverge).
        print("**Per-document NED** (escalated rows are where models differ):\n")
        header = "| doc_id | route | " + " | ".join(PRETTY[lab] for lab in present) + " |"
        print(header)
        print("|---|---|" + "--:|" * len(present))
        for qid in doc_ids:
            # route taken from the first VLM label that has it
            route = "—"
            for lab in present:
                if qid in data[lab]["route"]:
                    route = data[lab]["route"][qid]
                    break
            label = qid if len(qid) <= 22 else qid[:19] + "…"
            cells = " | ".join(
                fmt(data[lab]["res"].get(qid, {}).get("ned")) for lab in present
            )
            print(f"| {label} | {route} | {cells} |")

        # Aggregate per label.
        print("\n**Aggregate (mean over graded docs):**\n")
        print("| model | NED | TEDS | mean lat (s) | promoted docs |")
        print("|---|--:|--:|--:|--:|")
        for lab in present:
            res = data[lab]["res"]
            row = []
            for m in METRICS:
                vals = [r[m] for r in res.values() if isinstance(r.get(m), (int, float))]
                row.append(fmt(mean(vals)) if vals else "—")
            lats = [x for x in data[lab]["lat"].values() if isinstance(x, (int, float))]
            promoted = sum(1 for rt in data[lab]["route"].values()
                           if rt not in ("docling-kept", "error"))
            print(f"| {PRETTY[lab]} | " + " | ".join(row)
                  + f" | {fmt(mean(lats), 2) if lats else '—'} | {promoted} |")


if __name__ == "__main__":
    main()
