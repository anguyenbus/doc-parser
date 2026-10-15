"""The parser package version, as installed (``0.0.0+dev`` from a source tree)."""

from __future__ import annotations

import importlib.metadata

try:
    PARSER_VERSION: str = importlib.metadata.version("parser-service")
except importlib.metadata.PackageNotFoundError:
    PARSER_VERSION = "0.0.0+dev"
