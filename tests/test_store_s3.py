"""
test_store_s3.py — the S3 store (``parser_service.storage.store.S3Store``) through botocore's Stubber.

Offline: no network and no AWS call. Stubber replays a scripted queue and holds no
state, so each case scripts the calls it expects, with the request parameters
asserted by the Stubber itself (bucket, full key, ``IfNoneMatch``, prefix, delete
batches), and then checks how the store maps each response.

Run ONLY these tests:
    uv run pytest tests/test_store_s3.py
"""

from __future__ import annotations

import io
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from parser_service import io_layer
from parser_service.storage import store as store_module
from parser_service.storage.store import S3Store, StoreDeleteError, StoreNotFoundError, open_store

REGION = "ap-southeast-2"
BUCKET = "bucket"


def _client() -> Any:
    return boto3.client(
        "s3", region_name=REGION, aws_access_key_id="test", aws_secret_access_key="test"
    )


@pytest.fixture
def s3() -> Iterator[tuple[S3Store, Stubber]]:
    """A store at ``s3://bucket/parser/`` on a stubbed client, and its Stubber."""
    client = _client()
    store = open_store(f"s3://{BUCKET}/parser/", region=REGION, client=client)
    assert isinstance(store, S3Store)
    with Stubber(client) as stubber:
        yield store, stubber
        stubber.assert_no_pending_responses()


def _body(data: bytes) -> dict[str, Any]:
    return {"Body": StreamingBody(io.BytesIO(data), len(data))}


def test_single_call_operations_send_bucket_and_full_key(s3: tuple[S3Store, Stubber]) -> None:
    store, stubber = s3
    key = {"Bucket": BUCKET, "Key": "parser/work/x/manifest.json"}
    stubber.add_response("get_object", _body(b"{}"), key)
    stubber.add_response(
        "put_object", {}, {**key, "Body": b"{}", "ContentType": "application/json"}
    )
    stubber.add_response("head_object", {}, key)
    stubber.add_response("delete_object", {}, key)

    assert store.read("work/x/manifest.json") == b"{}"
    store.write("work/x/manifest.json", b"{}")
    assert store.exists("work/x/manifest.json") is True
    store.delete("work/x/manifest.json")
    assert store.location("work/x/manifest.json") == "s3://bucket/parser/work/x/manifest.json"
    with pytest.raises(ValueError):  # the shared key validation, before any call
        store.read("work/../manifest.json")


def test_not_found_mapping_and_other_errors_propagate(s3: tuple[S3Store, Stubber]) -> None:
    store, stubber = s3
    stubber.add_client_error("get_object", "NoSuchKey", "no such key", 404)
    stubber.add_client_error("head_object", "404", "Not Found", 404)
    stubber.add_client_error("get_object", "AccessDenied", "denied", 403)
    stubber.add_client_error("head_object", "403", "Forbidden", 403)

    with pytest.raises(StoreNotFoundError):
        store.read("out/x/output.md")
    assert store.exists("out/x/output.md") is False
    with pytest.raises(ClientError, match="AccessDenied"):
        store.read("out/x/output.md")
    with pytest.raises(ClientError):
        store.exists("out/x/output.md")


def test_write_if_absent_maps_exists_conflict_and_other_errors(
    s3: tuple[S3Store, Stubber], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, stubber = s3
    monkeypatch.setattr("parser_service.storage.store.time.sleep", lambda seconds: None)
    params = {
        "Bucket": BUCKET,
        "Key": "parser/work/x/results/0001.json",
        "Body": b"{}",
        "ContentType": "application/json",
        "IfNoneMatch": "*",
    }
    conflict = ("put_object", "ConditionalRequestConflict", "conflict", 409)
    retries = store_module._S3_CONFLICT_RETRIES

    stubber.add_response("put_object", {}, params)
    assert store.write_if_absent("work/x/results/0001.json", b"{}") is True

    stubber.add_client_error("put_object", "PreconditionFailed", "exists", 412, None, params)
    assert store.write_if_absent("work/x/results/0001.json", b"{}") is False

    stubber.add_client_error(*conflict, None, params)
    stubber.add_response("put_object", {}, params)
    assert store.write_if_absent("work/x/results/0001.json", b"{}") is True

    stubber.add_client_error(*conflict, None, params)
    stubber.add_client_error("put_object", "PreconditionFailed", "exists", 412, None, params)
    assert store.write_if_absent("work/x/results/0001.json", b"{}") is False

    for _ in range(retries + 1):  # bounded: it gives up and reports "not created"
        stubber.add_client_error(*conflict, None, params)
    assert store.write_if_absent("work/x/results/0001.json", b"{}") is False

    stubber.add_client_error("put_object", "AccessDenied", "denied", 403, None, params)
    with pytest.raises(ClientError, match="AccessDenied"):
        store.write_if_absent("work/x/results/0001.json", b"{}")


def test_list_paginates_and_returns_sorted_relative_keys(s3: tuple[S3Store, Stubber]) -> None:
    store, stubber = s3
    stubber.add_response(
        "list_objects_v2",
        {
            "Contents": [
                {"Key": "parser/work/x/pages/"},  # a console "folder": never returned
                {"Key": "parser/work/x/pages/0002.pdf"},
            ],
            "IsTruncated": True,
            "NextContinuationToken": "t1",
        },
        {"Bucket": BUCKET, "Prefix": "parser/work/x/pages/"},
    )
    stubber.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": "parser/work/x/pages/0001.pdf"}], "IsTruncated": False},
        {"Bucket": BUCKET, "Prefix": "parser/work/x/pages/", "ContinuationToken": "t1"},
    )
    stubber.add_response(
        "list_objects_v2", {"IsTruncated": False}, {"Bucket": BUCKET, "Prefix": "parser/"}
    )

    assert store.list_keys("work/x/pages/") == ["work/x/pages/0001.pdf", "work/x/pages/0002.pdf"]
    assert store.list_keys() == []


def test_delete_prefix_batches_raises_per_key_errors_and_refuses_empty(
    s3: tuple[S3Store, Stubber],
) -> None:
    store, stubber = s3
    keys = [f"parser/work/x/pages/{n:04d}.pdf" for n in range(1, 2501)]
    listing = {"Bucket": BUCKET, "Prefix": "parser/work/x/"}
    stubber.add_response(
        "list_objects_v2", {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}, listing
    )
    for start in (0, 1000, 2000):
        batch = keys[start : start + 1000]
        assert len(batch) <= 1000
        stubber.add_response(
            "delete_objects",
            {},
            {"Bucket": BUCKET, "Delete": {"Objects": [{"Key": k} for k in batch], "Quiet": True}},
        )
    store.delete_prefix("work/x/")

    stubber.add_response(
        "list_objects_v2", {"Contents": [{"Key": keys[0]}], "IsTruncated": False}, listing
    )
    stubber.add_response(
        "delete_objects",
        {"Errors": [{"Key": keys[0], "Code": "AccessDenied", "Message": "denied"}]},
        {"Bucket": BUCKET, "Delete": {"Objects": [{"Key": keys[0]}], "Quiet": True}},
    )
    with pytest.raises(StoreDeleteError, match="AccessDenied"):
        store.delete_prefix("work/x/")

    # Refused before any call: nothing is scripted, so a call would fail differently.
    for refused in ("", "work/x"):
        with pytest.raises(ValueError):
            store.delete_prefix(refused)


def test_content_type_comes_from_the_key_suffix(s3: tuple[S3Store, Stubber]) -> None:
    store, stubber = s3
    for key, content_type in (
        ("work/x/results/0001.json", "application/json"),
        ("work/x/results/0001.md", "text/markdown; charset=utf-8"),
        ("work/x/pages/0001.pdf", "application/pdf"),
        ("work/x/attempts/0001", "application/octet-stream"),
    ):
        stubber.add_response(
            "put_object",
            {},
            {"Bucket": BUCKET, "Key": f"parser/{key}", "Body": b"x", "ContentType": content_type},
        )
        store.write(key, b"x")


def test_opening_needs_a_region_and_normalises_the_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    regions: list[str | None] = []
    client = _client()

    def factory(region: str | None = None) -> Any:
        regions.append(region)
        return client

    monkeypatch.setattr(io_layer, "s3_client", factory)

    with pytest.raises(ValueError, match="region"):
        open_store("s3://bucket/parser")
    assert regions == []  # refused before a client is asked for

    with_slash = open_store("s3://bucket/parser/", region=REGION)
    without = open_store("s3://bucket/parser", region=REGION)
    bare = open_store("s3://bucket", region=REGION)
    assert regions == [REGION, REGION, REGION]  # the region is passed explicitly
    assert with_slash.location("out/a") == without.location("out/a") == "s3://bucket/parser/out/a"
    assert bare.location("out/a") == open_store("s3://bucket/", region=REGION).location("out/a")
    assert bare.location("out/a") == "s3://bucket/out/a"  # an empty prefix is valid

    with Stubber(client) as stubber:
        stubber.add_response("get_object", _body(b"x"), {"Bucket": "bucket", "Key": "out/a"})
        assert bare.read("out/a") == b"x"


def test_local_file_scope_removes_its_directory_in_every_outcome(
    s3: tuple[S3Store, Stubber],
) -> None:
    store, stubber = s3
    request = {"Bucket": BUCKET, "Key": "parser/work/x/pages/0001.pdf"}
    seen: list[Path] = []

    stubber.add_response("get_object", _body(b"%PDF-page"), request)
    with store.local_file("work/x/pages/0001.pdf") as path:
        seen.append(path.parent)
        assert path.name == "0001.pdf"
        assert path.read_bytes() == b"%PDF-page"
        assert [p.name for p in path.parent.iterdir()] == ["0001.pdf"]
    assert not seen[0].exists()

    for raised in (RuntimeError("parse blew up"), KeyboardInterrupt()):
        stubber.add_response("get_object", _body(b"%PDF-page"), request)
        with pytest.raises(type(raised)), store.local_file("work/x/pages/0001.pdf") as path:
            seen.append(path.parent)
            raise raised
        assert not seen[-1].exists()

    # The download itself fails: the directory made for it is gone too.
    made: list[str] = []
    real = tempfile.TemporaryDirectory

    def recording(*args: Any, **kwargs: Any) -> Any:
        directory = real(*args, **kwargs)
        made.append(directory.name)
        return directory

    stubber.add_client_error("get_object", "NoSuchKey", "no such key", 404)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("parser_service.storage.store.tempfile.TemporaryDirectory", recording)
        with pytest.raises(StoreNotFoundError), store.local_file("work/x/pages/0001.pdf"):
            pytest.fail("the scope must not be entered")
    assert len(made) == 1 and not Path(made[0]).exists()
