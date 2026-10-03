"""
summarize_status.py — one table from a directory of status.json files.

    uv run python deploy/scripts/summarize_status.py <dir of <file id>.json>

Prints each file's state and page counts, then totals: files complete, pages ok /
degraded / failed / pending, and the routes the pages took (docling-kept, vlm,
blank, ...). Exit 0 when every file is complete, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    directory = Path(sys.argv[1])
    files = sorted(directory.glob("*.json"))
    if not files:
        print(f"no status files in {directory}")
        return 1
    totals: Counter[str] = Counter()
    routes: Counter[str] = Counter()
    complete = 0
    print(f"{'file':<40} {'state':<12} {'pages':>5} {'ok':>4} {'degr':>4} {'fail':>4} {'pend':>4}")
    for path in files:
        status = json.loads(path.read_text())
        counts = status.get("counts", {})
        totals.update({k: int(v) for k, v in counts.items()})
        routes.update(str(p.get("route")) for p in status.get("pages", []) if p.get("route"))
        complete += status.get("state") == "complete"
        print(
            f"{path.stem:<40} {status.get('state', '?'):<12} {status.get('page_count', 0):>5} "
            f"{counts.get('ok', 0):>4} {counts.get('degraded', 0):>4} {counts.get('failed', 0):>4} "
            f"{counts.get('pending', 0):>4}"
        )
    print(
        f"\n{complete}/{len(files)} files complete; pages: {dict(totals)}; routes: {dict(routes)}"
    )
    return 0 if complete == len(files) else 1


if __name__ == "__main__":
    sys.exit(main())
