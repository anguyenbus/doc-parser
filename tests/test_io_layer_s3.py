"""
test_io_layer_s3.py — ``S3IO`` on the shared URI parser and client factory (``io_layer``).

Offline: every S3 call is answered by botocore's Stubber. The stubbed client is
supplied by replacing the factory (``io_layer.s3_client``); the factory itself is
tested with ``boto3`` replaced in ``sys.modules`` (as ``test_client_retry.py`` does),
so no client is ever built against the real credential chain.

Run ONLY these tests:
    uv run pytest tests/test_io_layer_s3.py
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from parser_service import io_layer
from parser_service.io_layer import S3IO, InputRef, parse_s3_uri

REGION = "ap-southeast-2"


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch) -> Iterator[Stubber]:
    """A Stubber on a client that ``io_layer.s3_client()`` now returns."""
    client = boto3.client(
        "s3", region_name=REGION, aws_access_key_id="test", aws_secret_access_key="test"
    )
    monkeypatch.setattr(io_layer, "s3_client", lambda region=None: client)
    with Stubber(client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def test_parse_uri_on_an_instance_keeps_todays_value() -> None:
    assert S3IO()._parse_uri("s3://bucket/some/prefix/") == ("bucket", "some/prefix/")
    assert S3IO()._parse_uri("s3://bucket") == ("bucket", "")
    assert S3IO()._parse_uri("s3://bucket/a/b.pdf") == parse_s3_uri("s3://bucket/a/b.pdf")
    # Lenient: nothing new is raised for a URI the store opener would refuse.
    assert parse_s3_uri("s3://") == ("", "")
    assert parse_s3_uri("/local/path") == ("", "local/path")


def test_list_input_files_paginates_and_filters_by_extension(stubbed: Stubber) -> None:
    stubbed.add_response(
        "list_objects_v2",
        {
            "Contents": [{"Key": "in/a.pdf"}, {"Key": "in/notes.txt"}],
            "IsTruncated": True,
            "NextContinuationToken": "t1",
        },
        {"Bucket": "bucket", "Prefix": "in/"},
    )
    stubbed.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": "in/sub/b.PNG"}], "IsTruncated": False},
        {"Bucket": "bucket", "Prefix": "in/", "ContinuationToken": "t1"},
    )

    refs = list(S3IO().list_input_files("s3://bucket/in/"))

    assert refs == [
        InputRef(uri="s3://bucket/in/a.pdf", filename="a.pdf", kind="s3"),
        InputRef(uri="s3://bucket/in/sub/b.PNG", filename="b.PNG", kind="s3"),
    ]


def test_read_bytes_sends_bucket_and_key(stubbed: Stubber) -> None:
    stubbed.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(b"%PDF-bytes"), 10)},
        {"Bucket": "bucket", "Key": "in/a.pdf"},
    )
    ref = InputRef(uri="s3://bucket/in/a.pdf", filename="a.pdf", kind="s3")
    assert S3IO().read_bytes(ref) == b"%PDF-bytes"


def test_write_json_and_write_text_send_key_and_content_type(stubbed: Stubber) -> None:
    data = {"k": "v"}
    stubbed.add_response(
        "put_object",
        {},
        {
            "Bucket": "bucket",
            "Key": "out/run/report.json",
            "Body": json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
            "ContentType": "application/json",
        },
    )
    stubbed.add_response(
        "put_object",
        {},
        {
            "Bucket": "bucket",
            "Key": "out/run/report.md",
            "Body": b"# title",
            "ContentType": "text/markdown",
        },
    )
    stubbed.add_response(
        "put_object",
        {},
        {
            "Bucket": "bucket",
            "Key": "out/run/report.txt",
            "Body": b"plain",
            "ContentType": "text/plain",
        },
    )
    ref = InputRef(uri="s3://bucket/in/report.pdf", filename="report.pdf", kind="s3")

    S3IO().write_json(ref, "s3://bucket/out/run/", data)
    S3IO().write_text(ref, "s3://bucket/out/run", "# title", ".md")
    S3IO().write_text(ref, "s3://bucket/out/run", "plain", ".txt")


def _fake_boto3(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock()
    fake.client.side_effect = lambda *args, **kwargs: MagicMock(name=f"client{kwargs}")
    monkeypatch.setitem(sys.modules, "boto3", fake)
    monkeypatch.setattr(io_layer, "_S3_CLIENTS", {})
    return fake


def test_factory_without_a_region_uses_the_default_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_REGION", raising=False)
    fake = _fake_boto3(monkeypatch)

    client = io_layer.s3_client()

    assert client is not None
    fake.client.assert_called_once_with("s3")  # no region argument: boto3 resolves it
    assert io_layer.s3_client() is client  # one client per process
    assert fake.client.call_count == 1


def test_factory_passes_a_given_region_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_boto3(monkeypatch)

    client: Any = io_layer.s3_client(REGION)

    fake.client.assert_called_once_with("s3", region_name=REGION)
    assert io_layer.s3_client(REGION) is client
    assert io_layer.s3_client() is not client  # the default-chain client is a separate one
    assert fake.client.call_count == 2
