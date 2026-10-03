"""
render_timeline.py — draw timeline.csv (from 40_capture_evidence.sh) as one PNG.

    uv run --with matplotlib python deploy/scripts/render_timeline.py timeline.csv timeline.png

Top: page-service pods (HPA desired, ready) and parser nodes over time. Middle:
HPA CPU utilisation against its target. Bottom: page records written (cumulative).
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402


def _num(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def main() -> None:
    source, target = sys.argv[1], sys.argv[2]
    with open(source, newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        sys.exit(f"{source} has no rows")
    t = [datetime.fromisoformat(r["time"].replace("Z", "+00:00")) for r in rows]
    col = {k: [_num(r[k]) for r in rows] for k in rows[0] if k != "time"}

    fig, (pods, cpu, pages) = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    pods.step(t, col["hpa_desired"], where="post", label="HPA desired pods")
    pods.step(t, col["pods_ready"], where="post", label="ready pods")
    pods.step(t, col["pods_pending"], where="post", label="pending pods", alpha=0.6)
    nodes = pods.twinx()
    nodes.step(t, col["parser_nodes_ready"], where="post", color="black", label="parser nodes")
    nodes.set_ylabel("parser nodes")
    pods.set_ylabel("page-service pods")
    handles = pods.get_legend_handles_labels()
    extra = nodes.get_legend_handles_labels()
    pods.legend(handles[0] + extra[0], handles[1] + extra[1], loc="upper left")
    pods.set_title(f"KServe page-parser scale test — {os.environ.get('TEST_RUN_ID', '')}")

    cpu.plot(t, col["hpa_cpu_pct"], label="HPA average CPU % of request")
    cpu.axhline(float(os.environ.get("PREDICTOR_TARGET_CPU", 70)), ls="--", c="grey", label="target")
    cpu.set_ylabel("CPU %")
    cpu.legend(loc="upper left")

    pages.plot(t, col["pages_done"], label="page records written (all tests)")
    pages.set_ylabel("pages")
    pages.legend(loc="upper left")
    pages.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    pages.set_xlabel("time (UTC)")

    fig.tight_layout()
    fig.savefig(target, dpi=120)
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
