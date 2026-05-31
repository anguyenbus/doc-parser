"""
make_three_page_digital.py — generate ``three_page_digital.pdf`` (Task Group 5.4).

A genuinely 3-page DIGITAL PDF (embedded text layer, no images) with a UNIQUE
sentinel string per page so the multi-page regression tests can assert page
ORDER and per-page seam INTEGRITY:

  page 0 → "PAGE1_SENTINEL ..."
  page 1 → "PAGE2_SENTINEL ..." (the page the gate is forced to promote → VLM)
  page 2 → "PAGE3_SENTINEL ..."

Under Route B (adopted 2026-05-31) each page's elements are rendered
independently by ``render_markdown`` and joined with ``\\n\\n``, so a real
3-digital-page document gives the seam test three independently-rendered page
sections with distinct content to assert boundary integrity across — exactly
what the per-page-assembly seam test needs. (The same fixture also covered the
older placeholder-split seam, which is route-independent at the fixture level.)

No PDF library is installed in this environment (no reportlab/fpdf2/pymupdf),
so this writes a minimal hand-authored PDF and computes the xref byte offsets
itself — exactly the technique the pre-existing ``digital_simple.pdf`` fixture
used (it is also a hand-authored raw PDF). Run:

    uv run python tests/fixtures/make_three_page_digital.py

The committed ``three_page_digital.pdf`` is the output of this script; the
script is kept alongside the fixture to document how it was produced and to let
anyone regenerate it deterministically.
"""

from __future__ import annotations

from pathlib import Path

# (sentinel, extra body lines) per page. Sentinels are unique and contain no
# substring of one another so "contains its own / not the neighbor's" is sharp.
_PAGES = [
    (
        "PAGE1_SENTINEL",
        [
            "This is the first page of a three page digital PDF document.",
            "Alpha content unique to the opening page only.",
        ],
    ),
    (
        "PAGE2_SENTINEL",
        [
            "This is the middle page that the gate is forced to promote.",
            "Bravo content unique to the middle page only.",
        ],
    ),
    (
        "PAGE3_SENTINEL",
        [
            "This is the final page of the three page digital PDF document.",
            "Charlie content unique to the closing page only.",
        ],
    ),
]


def _content_stream(sentinel: str, lines: list[str]) -> bytes:
    parts = [
        "BT",
        "/F1 16 Tf",
        "72 720 Td",
        f"({sentinel}) Tj",
        "/F1 12 Tf",
    ]
    for line in lines:
        parts.append("0 -28 Td")
        parts.append(f"({line}) Tj")
    parts.append("ET")
    return ("\n".join(parts) + "\n").encode("latin-1")


def build_pdf() -> bytes:
    n_pages = len(_PAGES)
    # Object layout:
    #   1            → Catalog
    #   2            → Pages
    #   3 .. 2+n     → Page objects
    #   3+n .. 2+2n  → Content streams
    page_obj_ids = [3 + i for i in range(n_pages)]
    content_obj_ids = [3 + n_pages + i for i in range(n_pages)]

    objects: dict[int, bytes] = {}

    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"

    kids = " ".join(f"{oid} 0 R" for oid in page_obj_ids)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode("latin-1")

    font_res = (
        "/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >>"
    )
    for i in range(n_pages):
        page_oid = page_obj_ids[i]
        content_oid = content_obj_ids[i]
        objects[page_oid] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_oid} 0 R {font_res} >>"
        ).encode("latin-1")

        (sentinel, lines) = _PAGES[i]
        stream = _content_stream(sentinel, lines)
        objects[content_oid] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"endstream"
        )

    # Serialize, tracking each object's byte offset for the xref table.
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    max_id = max(objects)
    for oid in range(1, max_id + 1):
        offsets[oid] = len(out)
        out += f"{oid} 0 obj\n".encode("latin-1")
        out += objects[oid]
        out += b"\nendobj\n"

    xref_offset = len(out)
    n_entries = max_id + 1
    out += f"xref\n0 {n_entries}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for oid in range(1, max_id + 1):
        out += f"{offsets[oid]:010d} 00000 n \n".encode("latin-1")

    out += f"trailer\n<< /Size {n_entries} /Root 1 0 R >>\n".encode("latin-1")
    out += f"startxref\n{xref_offset}\n%%EOF".encode("latin-1")
    return bytes(out)


def main() -> None:
    target = Path(__file__).parent / "three_page_digital.pdf"
    target.write_bytes(build_pdf())
    print(f"wrote {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
