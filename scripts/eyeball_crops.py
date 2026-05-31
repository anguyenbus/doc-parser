"""
eyeball_crops.py

Visual inspection script for PDF coordinate conversion verification.

Renders full pages and a set of known-good bbox regions from a PDF,
saving PNG images to an output directory for manual inspection.

Usage:
    uv run python scripts/eyeball_crops.py \
        --pdf tests/fixtures/digital_simple.pdf \
        --output-dir /tmp/crops/

Inspect the output PNGs to confirm:
  - Full-page renders look correct
  - Crop regions correspond to the expected page areas
  - No off-by-one flipping (top of image = top of page)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from parser_service.render import render_page, render_region  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render PDF pages and crops for visual coordinate verification."
    )
    parser.add_argument("--pdf", required=True, help="Path to PDF file")
    parser.add_argument("--output-dir", required=True, help="Directory to save PNG images")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        logger.error("PDF not found: %s", pdf_path)
        sys.exit(1)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Render full pages 0 and 1.
    for page_no in [0, 1]:
        try:
            png = render_page(pdf_path, page_no, dpi=144)
            out_path = out_dir / f"page{page_no}_full.png"
            out_path.write_bytes(png)
            logger.info("Wrote %s (%d bytes)", out_path, len(png))
        except Exception as exc:
            logger.warning("Could not render page %d: %s", page_no, exc)

    # Render known-good bbox regions on page 0.
    # A standard Letter page is 612 x 792 pts.
    # We render 3 regions: top-left quadrant, center strip, bottom-right quadrant.
    regions = [
        ("topleft", {"x0": 0, "y0": 396, "x1": 306, "y1": 792}),  # top-left quarter
        ("center_strip", {"x0": 100, "y0": 300, "x1": 512, "y1": 500}),  # center band
        ("bottomright", {"x0": 306, "y0": 0, "x1": 612, "y1": 396}),  # bottom-right quarter
    ]

    for name, bbox in regions:
        try:
            png = render_region(pdf_path, 0, bbox, dpi=144)
            out_path = out_dir / f"page0_crop_{name}.png"
            out_path.write_bytes(png)
            logger.info("Wrote %s (%d bytes)", out_path, len(png))
        except Exception as exc:
            logger.warning("Could not render region %s: %s", name, exc)

    logger.info("Done. Inspect images in %s", out_dir)


if __name__ == "__main__":
    main()
