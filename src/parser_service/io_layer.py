"""
io_layer.py — I/O abstraction for local filesystem and AWS S3. ≤100 lines.

InputRef: dataclass describing a file location.
LocalIO / S3IO: implementations of list_input_files, read_bytes, write_json.
Runtime selection: S3IO if URI starts with s3://, else LocalIO.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".docx",
    ".xlsx",
    ".xlsm",
    ".html",
    ".htm",
}


@dataclass
class InputRef:
    uri: str
    filename: str
    kind: Literal["local", "s3"]


class LocalIO:
    def list_input_files(self, uri: str) -> Iterator[InputRef]:
        for p in sorted(Path(uri).iterdir()):
            if p.suffix.lower() in SUPPORTED_EXTENSIONS:
                yield InputRef(uri=str(p), filename=p.name, kind="local")

    def read_bytes(self, ref: InputRef) -> bytes:
        return Path(ref.uri).read_bytes()

    def write_json(self, ref: InputRef, output_uri: str, data: dict[str, Any]) -> None:
        out = Path(output_uri) / (Path(ref.filename).stem + ".json")
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_text(self, ref: InputRef, output_uri: str, text: str, ext: str) -> None:
        out = Path(output_uri) / (Path(ref.filename).stem + ext)
        out.write_text(text, encoding="utf-8")


class S3IO:
    def _parse_uri(self, uri: str) -> tuple[str, str]:
        p = urllib.parse.urlparse(uri)
        return p.netloc, p.path.lstrip("/")

    def list_input_files(self, uri: str) -> Iterator[InputRef]:
        import boto3

        bucket, prefix = self._parse_uri(uri)
        s3 = boto3.client("s3")
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                if Path(key).suffix.lower() in SUPPORTED_EXTENSIONS:
                    yield InputRef(uri=f"s3://{bucket}/{key}", filename=Path(key).name, kind="s3")

    def read_bytes(self, ref: InputRef) -> bytes:
        import boto3

        bucket, key = self._parse_uri(ref.uri)
        return boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()  # type: ignore[no-any-return]

    def write_json(self, ref: InputRef, output_uri: str, data: dict[str, Any]) -> None:
        import boto3

        bucket, prefix = self._parse_uri(output_uri)
        key = f"{prefix.rstrip('/')}/{Path(ref.filename).stem}.json"
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        boto3.client("s3").put_object(
            Bucket=bucket, Key=key, Body=body, ContentType="application/json"
        )

    def write_text(self, ref: InputRef, output_uri: str, text: str, ext: str) -> None:
        import boto3

        bucket, prefix = self._parse_uri(output_uri)
        key = f"{prefix.rstrip('/')}/{Path(ref.filename).stem}{ext}"
        ctype = "text/markdown" if ext == ".md" else "text/plain"
        boto3.client("s3").put_object(
            Bucket=bucket, Key=key, Body=text.encode("utf-8"), ContentType=ctype
        )


def _select_io(uri: str) -> LocalIO | S3IO:
    return S3IO() if uri.startswith("s3://") else LocalIO()


def list_input_files(uri: str) -> Iterator[InputRef]:
    """Yield InputRef for each supported document in uri (local dir or s3:// prefix)."""
    yield from _select_io(uri).list_input_files(uri)


def read_bytes(ref: InputRef) -> bytes:
    """Read raw file bytes from the location described by ref."""
    return _select_io(ref.uri).read_bytes(ref)


def write_json(ref: InputRef, output_uri: str, data: dict[str, Any]) -> None:
    """Serialize data as JSON and write to output_uri/<ref.filename>.json."""
    _select_io(output_uri).write_json(ref, output_uri, data)


def write_text(ref: InputRef, output_uri: str, text: str, ext: str = ".md") -> None:
    """Write text to output_uri/<ref.filename><ext> (default .md)."""
    _select_io(output_uri).write_text(ref, output_uri, text, ext)
