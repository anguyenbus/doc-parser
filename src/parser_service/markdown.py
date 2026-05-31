"""
markdown.py — render parser_output JSON into RAG-friendly Markdown.

The ``parser_output`` JSON is the structured, gradable contract. This module is
the canonical JSON→Markdown renderer **owned by the parser**, so the Markdown we
ship for downstream RAG is the same text we can reason about — not whatever a
grader's converter happens to produce.

Design choices (RAG-oriented, may differ from the benchmark's gold converter):
  - headings keep their level (chunk boundaries);
  - lists render one item per line;
  - tables render as real Markdown tables from their cells;
  - figures contribute their transcribed text / caption;
  - page furniture (page numbers, running headers/footers) is dropped as noise.
"""

from __future__ import annotations

from typing import Any

# Element types treated as page furniture and dropped from the RAG markdown.
_FURNITURE = {"page_number", "header", "footer"}


def _escape_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _table_to_markdown(content: dict[str, Any]) -> str:
    cells = content.get("cells") or []
    if not cells:
        return ""
    rows = content.get("rows") or (max((c.get("row", 0) for c in cells), default=-1) + 1)
    cols = content.get("cols") or (max((c.get("col", 0) for c in cells), default=-1) + 1)
    if rows <= 0 or cols <= 0:
        return ""

    grid: dict[tuple[int, int], str] = {}
    for c in cells:
        if isinstance(c, dict) and "row" in c and "col" in c:
            grid[(c["row"], c["col"])] = _escape_cell(c.get("text", ""))

    lines = []
    for r in range(rows):
        row = [grid.get((r, c), "") for c in range(cols)]
        lines.append("| " + " | ".join(row) + " |")
        if r == 0:  # header separator after the first row
            lines.append("| " + " | ".join(["---"] * cols) + " |")
    return "\n".join(lines)


def render_markdown(parser_output: dict[str, Any]) -> str:
    """Render a parser_output dict to a RAG-ready Markdown string."""
    blocks: list[str] = []

    for el in parser_output.get("elements", []):
        if not isinstance(el, dict):
            continue
        etype = el.get("type", "paragraph")
        if etype in _FURNITURE:
            continue
        text = (el.get("text") or "").strip()
        raw_content = el.get("content")
        content: dict[str, Any] = raw_content if isinstance(raw_content, dict) else {}

        if etype == "table" and content.get("kind") == "table":
            md = _table_to_markdown(content)
            blocks.append(md or text)
        elif etype == "heading":
            level = el.get("level", 1)
            try:
                level = min(max(int(level), 1), 6)
            except (TypeError, ValueError):
                level = 1
            if text:
                blocks.append(f"{'#' * level} {text}")
        elif etype == "list_item":
            if text:
                blocks.append(f"- {text}")
        elif etype == "list":
            continue  # container; items render as their own list_item elements
        elif etype == "code_block":
            if text:
                blocks.append(f"```\n{text}\n```")
        elif etype == "equation":
            if text:
                blocks.append(f"$$\n{text}\n$$")
        elif etype == "figure":
            alt = (content.get("alt_text") or "").strip()
            if text:
                blocks.append(text)
            elif alt:
                blocks.append(f"![{alt}]")
        else:  # paragraph, caption, footnote, and any unknown type
            if text:
                blocks.append(text)

    return "\n\n".join(b for b in blocks if b).strip() + "\n"
