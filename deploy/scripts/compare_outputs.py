"""
compare_outputs.py — compare two stitched documents page by page.

    uv run python deploy/scripts/compare_outputs.py <reference.md> <candidate.md> \
        [--reference-status status.json] [--candidate-status status.json]

Splits both on the ``==== page N ===`` markers and reports, per page, whether the
markdown is byte-identical and, if not, a similarity ratio (difflib). With the
status files it also shows each page's route, so a difference can be read
against it: Docling pages are deterministic and must match exactly; VLM pages are
model output and are expected to differ slightly between runs.

Exit 0 when every Docling page matches (or, without status files, every page);
exit 1 otherwise.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

_MARKER = re.compile(r"^==== page (\d+)(?: \([^)\n]*\))? ===$", re.MULTILINE)


def split_pages(text: str) -> dict[int, str]:
    pages: dict[int, str] = {}
    matches = list(_MARKER.finditer(text))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        pages[int(match.group(1))] = text[match.end() : end].strip("\n")
    return pages


def routes(path: str | None) -> dict[int, str]:
    if not path:
        return {}
    return {p["page"]: str(p.get("route")) for p in json.loads(Path(path).read_text())["pages"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("reference")
    parser.add_argument("candidate")
    parser.add_argument("--reference-status")
    parser.add_argument("--candidate-status")
    args = parser.parse_args()

    if not Path(args.candidate).is_file():
        print(f"FAIL: {args.candidate} does not exist (the run did not stitch)")
        return 1
    ref = split_pages(Path(args.reference).read_text(encoding="utf-8"))
    cand = split_pages(Path(args.candidate).read_text(encoding="utf-8"))
    ref_routes, cand_routes = routes(args.reference_status), routes(args.candidate_status)

    failed = sorted(set(ref) ^ set(cand))
    if failed:
        print(f"FAIL: page sets differ: only in one document: {failed}")
    print(f"{'page':>4}  {'route (ref / run)':<28} {'result':<10} similarity")
    for page in sorted(set(ref) & set(cand)):
        route = f"{ref_routes.get(page, '?')} / {cand_routes.get(page, '?')}"
        same = ref[page] == cand[page]
        ratio = 1.0 if same else difflib.SequenceMatcher(None, ref[page], cand[page]).ratio()
        print(f"{page:>4}  {route:<28} {'identical' if same else 'differs':<10} {ratio:.3f}")
        deterministic = cand_routes.get(page, "docling-kept") != "vlm"
        if not same and deterministic:
            failed.append(page)
    identical = sum(ref[p] == cand[p] for p in set(ref) & set(cand))
    print(f"\n{identical}/{len(ref)} pages byte-identical")
    if failed:
        print(f"FAIL: deterministic pages differ: {failed}")
        return 1
    print("PASS: every deterministic (non-VLM) page is byte-identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
