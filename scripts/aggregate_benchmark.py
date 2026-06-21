#!/usr/bin/env python
"""Aggregate run_benchmark2.sh outputs into a per-file + summary report (Markdown).

Metrics are NED + TEDS (doc-bench >= the NED/TEDS wheel; NID/BLEU/METEOR are retired;
the bundled-loader wheel renamed the CSV column ``ned`` -> ``ned_similarity``). Joins,
per dataset and engine: grader per-file metrics (results CSV, keyed by ``query_id`` —
now the real ``doc_id``), the page route (``route_stats.csv``), and per-file parse
latency (the ``file_parsed`` JSON log lines). All three key on the same doc_id stem,
so the join is direct. Prints Markdown to stdout.
"""
from __future__ import annotations

import csv
import glob
import json
import sys
from pathlib import Path
from statistics import mean

ENGINES = ["vlm", "textract"]
METRICS = ["ned", "teds"]
# CSV column aliases per logical metric (the bundled-loader wheel renamed ned -> ned_similarity).
COLUMNS = {"ned": ("ned_similarity", "ned"), "teds": ("teds",)}


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
        # query_id is the real doc_id (matches route_stats / latency stems directly).
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

    print("# doc-parser Benchmark — vlm vs textract escalation engine\n")
    print("Per-file **NED + TEDS** + parse latency, both escalation engines, across the "
          "local doc-bench datasets.\n")
    print("- Grader: doc-bench wheel with NED/TEDS (NID/BLEU/METEOR retired). "
          "Region: ap-southeast-2.")
    print("- **NED** = normalized edit-distance similarity (higher is better). "
          "**TEDS** = table structure similarity (higher is better; 0 when a doc has no table).")
    print("- Latency = full end-to-end parse (Docling + escalation), not just the engine call.")
    print("- Only escalated (promoted) pages differ between engines; `docling-kept` pages are identical.\n")

    for ds in datasets:
        dsd = work / ds
        data = {}
        for eng in ENGINES:
            data[eng] = {
                "res": load_results(dsd / f"results_{eng}"),
                "route": load_routes(dsd / f"predictions_{eng}"),
                "lat": load_latency(dsd / f"parse_{eng}.log"),
            }
        doc_ids = sorted(set(data["vlm"]["res"]) | set(data["textract"]["res"]))
        if not doc_ids:
            continue

        print(f"\n## {ds} ({len(doc_ids)} docs)\n")
        print("| doc_id | route vlm | route tex | NED vlm | NED tex | TEDS vlm | TEDS tex "
              "| lat vlm (s) | lat tex (s) |")
        print("|---|---|---|--:|--:|--:|--:|--:|--:|")
        for qid in doc_ids:
            rid = qid  # query_id is the real doc_id; route_stats/latency use the same stem
            v, t = data["vlm"], data["textract"]
            vr, tr = v["res"].get(qid, {}), t["res"].get(qid, {})
            label = rid if len(rid) <= 28 else rid[:25] + "…"
            print(f"| {label} | {v['route'].get(rid,'—')} | {t['route'].get(rid,'—')} "
                  f"| {fmt(vr.get('ned'))} | {fmt(tr.get('ned'))} "
                  f"| {fmt(vr.get('teds'))} | {fmt(tr.get('teds'))} "
                  f"| {fmt(v['lat'].get(rid),2)} | {fmt(t['lat'].get(rid),2)} |")

        print("\n**Aggregate (mean over graded docs):**\n")
        print("| engine | NED | TEDS | mean lat (s) | total lat (s) | promoted docs |")
        print("|---|--:|--:|--:|--:|--:|")
        for eng in ENGINES:
            res = data[eng]["res"]
            row = []
            for m in METRICS:
                vals = [r[m] for r in res.values() if isinstance(r.get(m), (int, float))]
                row.append(fmt(mean(vals)) if vals else "—")
            lats = [x for x in data[eng]["lat"].values() if isinstance(x, (int, float))]
            promoted = sum(1 for rt in data[eng]["route"].values()
                           if rt not in ("docling-kept", "error"))
            print(f"| {eng} | " + " | ".join(row)
                  + f" | {fmt(mean(lats),2) if lats else '—'} "
                  f"| {fmt(sum(lats),1) if lats else '—'} | {promoted} |")


if __name__ == "__main__":
    main()
