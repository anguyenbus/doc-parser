"""
store.py — the page pipeline's storage, behind one small interface.

A store is opened from a location string with ``open_store``: a local directory
(``LocalStore``) or ``s3://bucket[/prefix]`` (``S3Store``). Keys are relative,
``/``-separated strings; a local directory holds the same keys as files.

Operations and their guarantees (identical for every store):

    read(key)                 the bytes; a missing key raises ``StoreNotFoundError``
    write(key, data)          atomic replace: a reader sees the old or the new
                              complete object, never a part
    write_if_absent(key, …)   first write wins: True only for the call that created
                              the object; False leaves the stored object untouched
    exists(key)               no error for a missing key
    list_keys(prefix)         keys relative to the store root, sorted; plain string
                              prefix; never a temp file, a directory or a folder marker
    delete(key)               a missing key is not an error
    delete_prefix(prefix)     everything under ``prefix``; refuses a prefix that is
                              empty or does not end in ``/``
    location(key)             where the key lives, for reporting; no I/O
    local_file(key)           a context manager giving a real file for the key
                              (Docling and pypdf need one); read-only for the caller

A store object is safe to use from several threads, and several processes may each
open the same location.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Literal, Protocol

from parser_service.storage import aws

logger = logging.getLogger(__name__)

# Local temp names are ``<final name><marker><pid>.<random>``: the marker lets
# list/exists/read recognise a file left behind by a crash and ignore it.
_TMP_MARKER = ".store-tmp."

_S3_DELETE_BATCH = 1000

# Conditional put (``If-None-Match: *``) outcomes, from the S3 API reference for
# PutObject (https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html,
# request header If-None-Match) and the user guide's "Conditional write behavior"
# (https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html):
#   412 PreconditionFailed          the key already exists          -> False
#   409 ConditionalRequestConflict  a conflicting operation (a delete) ran during
#                                   the upload; "On a 409 failure, retry the upload"
_S3_ALREADY_EXISTS = "PreconditionFailed"
_S3_CONFLICT = "ConditionalRequestConflict"
_S3_CONFLICT_RETRIES = 3
_S3_CONFLICT_BACKOFF_SECONDS = 0.2

_S3_NOT_FOUND_CODES = ("NoSuchKey", "404", "NotFound")

_CONTENT_TYPES = {
    ".json": "application/json",
    ".md": "text/markdown; charset=utf-8",
    ".pdf": "application/pdf",
}
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


class StoreNotFoundError(Exception):
    """The key does not exist. Raised by every store's ``read`` and ``local_file``."""


class StoreDeleteError(RuntimeError):
    """A delete-by-prefix could not remove every key under the prefix."""


def validate_key(key: str) -> str:
    """Return ``key`` if it is a valid store key; raise ``ValueError`` otherwise.

    Rejected, before any I/O: an empty key, a leading ``/``, a backslash, and an
    empty, ``.`` or ``..`` segment.
    """
    if not isinstance(key, str) or not key:
        raise ValueError("store key is empty")
    if key.startswith("/"):
        raise ValueError(f"store key starts with '/': {key!r}")
    if "\\" in key:
        raise ValueError(f"store key contains a backslash: {key!r}")
    if any(segment in ("", ".", "..") for segment in key.split("/")):
        raise ValueError(f"store key has an empty, '.' or '..' segment: {key!r}")
    return key


def _validate_list_prefix(prefix: str) -> str:
    """A list prefix is a plain string prefix: empty, or a key that may stop anywhere."""
    if prefix:
        head, _, _tail = prefix.rpartition("/")
        if head or prefix.startswith("/"):
            validate_key(head)
        if "\\" in prefix:
            raise ValueError(f"store prefix contains a backslash: {prefix!r}")
    return prefix


def _validate_delete_prefix(prefix: str) -> str:
    """Refuse a delete-by-prefix that could reach beyond one named subtree."""
    if not prefix:
        raise ValueError("delete_prefix refuses an empty prefix")
    if not prefix.endswith("/"):
        raise ValueError(f"delete_prefix needs a prefix ending in '/': {prefix!r}")
    validate_key(prefix[:-1])
    return prefix


class Store(Protocol):
    """What the page pipeline needs from storage. See the module docstring."""

    def read(self, key: str) -> bytes: ...

    def write(self, key: str, data: bytes) -> None: ...

    def write_if_absent(self, key: str, data: bytes) -> bool: ...

    def exists(self, key: str) -> bool: ...

    def list_keys(self, prefix: str = "") -> list[str]: ...

    def delete(self, key: str) -> None: ...

    def delete_prefix(self, prefix: str) -> None: ...

    def location(self, key: str) -> str: ...

    def local_file(self, key: str) -> AbstractContextManager[Path]: ...


# ---------------------------------------------------------------------------
# Local disk
# ---------------------------------------------------------------------------


def _is_temp_name(name: str) -> bool:
    return _TMP_MARKER in name


class LocalStore:
    """Keys are files under one root directory; file names equal keys."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        return self.root / validate_key(key)

    def _write_temp(self, path: Path, data: bytes) -> Path:
        """Write ``data`` to a uniquely named temp file beside ``path``."""
        if _is_temp_name(path.name):
            raise ValueError(f"store key looks like a temp file: {path.name!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}{_TMP_MARKER}{os.getpid()}.{secrets.token_hex(8)}")
        try:
            tmp.write_bytes(data)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return tmp

    def read(self, key: str) -> bytes:
        path = self._path(key)
        if _is_temp_name(path.name):
            raise StoreNotFoundError(key)
        try:
            return path.read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise StoreNotFoundError(key) from exc

    def write(self, key: str, data: bytes) -> None:
        path = self._path(key)
        tmp = self._write_temp(path, data)
        try:
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def write_if_absent(self, key: str, data: bytes) -> bool:
        path = self._path(key)
        tmp = self._write_temp(path, data)
        try:
            # A hard link is one atomic create of the final name that fails if it
            # exists, and the file it names is already complete.
            os.link(tmp, path)
        except FileExistsError:
            return False
        finally:
            tmp.unlink(missing_ok=True)
        return True

    def exists(self, key: str) -> bool:
        path = self._path(key)
        return not _is_temp_name(path.name) and path.is_file()

    def list_keys(self, prefix: str = "") -> list[str]:
        _validate_list_prefix(prefix)
        start = self.root / prefix.rpartition("/")[0]
        keys: list[str] = []
        for directory, _subdirs, names in os.walk(start):
            relative = Path(directory).relative_to(self.root).as_posix()
            base = "" if relative == "." else f"{relative}/"
            for name in names:
                key = f"{base}{name}"
                if not _is_temp_name(name) and key.startswith(prefix):
                    keys.append(key)
        return sorted(keys)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> None:
        _validate_delete_prefix(prefix)
        directory = self.root / prefix[:-1]
        if directory.is_dir():
            shutil.rmtree(directory)

    def location(self, key: str) -> str:
        return str(self._path(key))

    @contextmanager
    def local_file(self, key: str) -> Iterator[Path]:
        """The stored file itself, not a copy; the caller must not change it."""
        path = self._path(key)
        if not self.exists(key):
            raise StoreNotFoundError(key)
        yield path


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------


def _s3_error(exc: Exception) -> tuple[str, int]:
    """``(error code, HTTP status)`` of a botocore ``ClientError``."""
    response = getattr(exc, "response", None) or {}
    code = str(response.get("Error", {}).get("Code", ""))
    status = int(response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
    return code, status


class S3Store:
    """Keys are objects under ``s3://bucket/prefix/``. Never creates a bucket."""

    def __init__(
        self, bucket: str, prefix: str = "", region: str | None = None, client: Any = None
    ) -> None:
        if not bucket:
            raise ValueError("the S3 store needs a bucket")
        if client is None and not region:
            raise ValueError(f"the S3 store s3://{bucket}/{prefix} needs a region (set AWS_REGION)")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.region = region
        self._client = client if client is not None else aws.s3_client(region)

    def _key(self, key: str) -> str:
        validate_key(key)
        return f"{self.prefix}/{key}" if self.prefix else key

    def _root(self) -> str:
        return f"{self.prefix}/" if self.prefix else ""

    def read(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        full = self._key(key)
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=full)
        except ClientError as exc:
            if _s3_error(exc)[0] in _S3_NOT_FOUND_CODES:
                raise StoreNotFoundError(key) from exc
            raise
        return response["Body"].read()  # type: ignore[no-any-return]

    def _put(self, key: str, data: bytes, **extra: Any) -> None:
        content_type = _CONTENT_TYPES.get(Path(key).suffix.lower(), _DEFAULT_CONTENT_TYPE)
        self._client.put_object(
            Bucket=self.bucket, Key=self._key(key), Body=data, ContentType=content_type, **extra
        )

    def write(self, key: str, data: bytes) -> None:
        self._put(key, data)

    def write_if_absent(self, key: str, data: bytes) -> bool:
        from botocore.exceptions import ClientError

        for attempt in range(_S3_CONFLICT_RETRIES + 1):
            try:
                self._put(key, data, IfNoneMatch="*")
            except ClientError as exc:
                code, status = _s3_error(exc)
                if code == _S3_ALREADY_EXISTS or status == 412:
                    return False
                if code != _S3_CONFLICT and status != 409:
                    raise
                logger.warning(
                    "conditional write of %s conflicted (try %d)", self.location(key), attempt + 1
                )
                time.sleep(_S3_CONFLICT_BACKOFF_SECONDS * (attempt + 1))
            else:
                return True
        # Still conflicting after every retry: this call created nothing.
        return False

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as exc:
            code, status = _s3_error(exc)
            if code in _S3_NOT_FOUND_CODES or status == 404:
                return False
            raise
        return True

    def _list_full_keys(self, prefix: str) -> list[str]:
        """Every object key under the store root plus ``prefix``, folder markers included."""
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{self._root()}{prefix}"):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return keys

    def list_keys(self, prefix: str = "") -> list[str]:
        _validate_list_prefix(prefix)
        root = self._root()
        return sorted(
            key[len(root) :] for key in self._list_full_keys(prefix) if not key.endswith("/")
        )

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self._key(key))

    def delete_prefix(self, prefix: str) -> None:
        _validate_delete_prefix(prefix)
        keys = self._list_full_keys(prefix)
        for start in range(0, len(keys), _S3_DELETE_BATCH):
            batch = keys[start : start + _S3_DELETE_BATCH]
            response = self._client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
            errors = response.get("Errors") or []
            if errors:
                first = errors[0]
                raise StoreDeleteError(
                    f"{len(errors)} key(s) under s3://{self.bucket}/{self._root()}{prefix} were "
                    f"not deleted; first: {first.get('Key')}: {first.get('Code')}: "
                    f"{first.get('Message')}"
                )

    def location(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._key(key)}"

    @contextmanager
    def local_file(self, key: str) -> Iterator[Path]:
        """Download the object into a fresh temp directory, under the key's base name.

        The directory is removed when the scope ends, whatever happened inside it.
        """
        validate_key(key)
        with tempfile.TemporaryDirectory(prefix="parser-store-") as directory:
            path = Path(directory) / key.rpartition("/")[2]
            path.write_bytes(self.read(key))
            yield path


# ---------------------------------------------------------------------------
# Opening a store from a location string
# ---------------------------------------------------------------------------


def location_kind(location: str) -> Literal["local", "s3"]:
    """Classify a location string, raising ``ValueError`` when it cannot be used.

    ``s3://bucket[/prefix]`` is S3; any other ``scheme://`` is refused; everything
    else is a local directory path. No I/O.
    """
    if not isinstance(location, str) or not location.strip():
        raise ValueError("store location is empty")
    if "://" not in location:
        return "local"
    if not location.startswith("s3://"):
        raise ValueError(f"store location has an unsupported scheme (only s3://): {location!r}")
    bucket, _prefix = aws.parse_s3_uri(location)
    if not bucket:
        raise ValueError(f"store location has no bucket: {location!r}")
    return "s3"


def open_store(location: str | Path, region: str | None = None, client: Any = None) -> Store:
    """Open the store at ``location``: a local directory or ``s3://bucket[/prefix]``.

    ``region`` is required for S3 and is never read from the environment here;
    ``client`` injects an S3 client (tests). A trailing slash makes no difference.
    """
    if isinstance(location, Path):
        return LocalStore(location)
    if location_kind(location) == "local":
        return LocalStore(location)
    bucket, prefix = aws.parse_s3_uri(location)
    return S3Store(bucket, prefix, region=region, client=client)
