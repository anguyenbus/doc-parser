"""
io_layer.py — I/O abstraction for local filesystem and AWS S3.

InputRef: dataclass describing a file location.
LocalIO / S3IO: implementations of list_input_files, read_bytes, write_json.
Runtime selection: S3IO if URI starts with s3://, else LocalIO.
parse_s3_uri / s3_client: the one S3 URI parser and client factory in the repo,
shared by S3IO and the page store (``store.py``).
"""

from __future__ import annotations

import json
import logging
import threading
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


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split ``s3://bucket/key`` into ``(bucket, key)``.

    Lenient on purpose: nothing is validated and nothing is raised. Strict checks
    (scheme, non-empty bucket) belong to the caller that needs them.
    """
    p = urllib.parse.urlparse(uri)
    return p.netloc, p.path.lstrip("/")


_S3_CLIENTS: dict[str | None, Any] = {}
_S3_CLIENTS_LOCK = threading.Lock()


def s3_client(region: str | None = None) -> Any:
    """The process-wide S3 client: one per region, created on first use.

    ``region`` is passed to boto3 explicitly when given; with None boto3 resolves
    it from its default chain (``AWS_REGION``, the profile, the instance role).
    """
    with _S3_CLIENTS_LOCK:
        client = _S3_CLIENTS.get(region)
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=region) if region else boto3.client("s3")
            _S3_CLIENTS[region] = client
        return client


class S3IO:
    def _parse_uri(self, uri: str) -> tuple[str, str]:
        return parse_s3_uri(uri)

    def list_input_files(self, uri: str) -> Iterator[InputRef]:
        bucket, prefix = self._parse_uri(uri)
        paginator = s3_client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                if Path(key).suffix.lower() in SUPPORTED_EXTENSIONS:
                    yield InputRef(uri=f"s3://{bucket}/{key}", filename=Path(key).name, kind="s3")

    def read_bytes(self, ref: InputRef) -> bytes:
        bucket, key = self._parse_uri(ref.uri)
        return s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()  # type: ignore[no-any-return]

    def write_json(self, ref: InputRef, output_uri: str, data: dict[str, Any]) -> None:
        bucket, prefix = self._parse_uri(output_uri)
        key = f"{prefix.rstrip('/')}/{Path(ref.filename).stem}.json"
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        s3_client().put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")

    def write_text(self, ref: InputRef, output_uri: str, text: str, ext: str) -> None:
        bucket, prefix = self._parse_uri(output_uri)
        key = f"{prefix.rstrip('/')}/{Path(ref.filename).stem}{ext}"
        ctype = "text/markdown" if ext == ".md" else "text/plain"
        s3_client().put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"), ContentType=ctype)


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
