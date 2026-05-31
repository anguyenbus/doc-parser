"""
eval_adapter.py

Thin eval-harness entry point. No business logic.

Invoked by eval-harness via:
    --parser src.parser_service.eval_adapter
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from parser_service.parser_service import parse as _parse

logger = logging.getLogger(__name__)


def parse(file_path: Path) -> dict[str, Any]:
    """Eval-harness entry point — delegates to parser_service.parse."""
    return _parse(Path(file_path))
