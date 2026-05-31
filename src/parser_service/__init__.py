"""
parser_service package.

Exposes parse() at the package level for eval-harness and script entry points.
"""

from __future__ import annotations

import logging
import os

from .parser_service import parse

logging.basicConfig(level=os.environ.get("PARSER_LOG_LEVEL", "INFO"))

__all__ = ["parse"]
