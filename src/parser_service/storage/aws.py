"""
aws.py — the one S3 URI parser and S3 client factory in the repo.

Shared by the page store (``storage/store.py``) and the scripts. Callers use ``aws.s3_client(...)`` at call time, so tests replace it in one place.
"""

from __future__ import annotations

import threading
import urllib.parse
from typing import Any


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
