"""
parser_service package.

Subpackages: ``formats`` (detect a file's kind, guard and parse it), ``parsing`` (Docling,
the quality gate, escalation engines, markdown), ``pipeline`` (intake, units, status,
stitch), ``service`` (the KServe page service and its worker), ``storage`` and
``delivery`` (publish, jobs).
"""

from __future__ import annotations

import logging
import os

from PIL import Image

logging.basicConfig(level=os.environ.get("PARSER_LOG_LEVEL", "INFO"))

# Explicit decompression-bomb ceiling for image inputs. Set deliberately (rather
# than relying on PIL's ~89 MP default) so it is a documented, tunable guard.
# This covers PIL.Image.open() paths (e.g. Docling reading an attacker-controlled
# image); the pdfium render path is bounded separately in render.py. Default
# 128 MP sits above any realistic legitimate scan (600-DPI A3 ~= 70 MP).
Image.MAX_IMAGE_PIXELS = int(os.environ.get("PARSER_MAX_IMAGE_PIXELS", 128_000_000))
