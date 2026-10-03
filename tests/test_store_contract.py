"""
test_store_contract.py — the store interface (``parser_service.store``) and the local store.

``CONTRACT_CASES`` is one store-agnostic list of cases, each taking an empty store.
It runs here against ``LocalStore`` on a real temporary directory, and is reused by
``tests/test_page_pipeline_store.py`` for the in-memory test store. The S3 store
scripts the same behaviour call by call in ``tests/test_store_s3.py`` (Stubber
holds no state).

Run ONLY these tests:
    uv run pytest tests/test_store_contract.py
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from parser_service.store import (
    _TMP_MARKER,
    LocalStore,
    Store,
    StoreNotFoundError,
    open_store,
)

BAD_KEYS = ["", "/a", "a\\b", "a//b", "a/", "./a", "a/./b", "..", "a/../b"]


def case_read_and_atomic_write(store: Store) -> None:
    with pytest.raises(StoreNotFoundError):
        store.read("missing.json")
    store.write("deep/er/still/a.json", b"one")  # parents are created
    assert store.read("deep/er/still/a.json") == b"one"
    store.write("deep/er/still/a.json", b"two")  # replace
    assert store.read("deep/er/still/a.json") == b"two"
    with pytest.raises(StoreNotFoundError):
        store.read("deep/er")  # a "directory" is not a key


def case_write_if_absent_first_write_wins(store: Store) -> None:
    assert store.write_if_absent("results/0001.json", b"first") is True
    assert store.write_if_absent("results/0001.json", b"second") is False
    assert store.read("results/0001.json") == b"first"


def case_exists_and_delete(store: Store) -> None:
    assert store.exists("a/b.md") is False
    store.write("a/b.md", b"x")
    assert store.exists("a/b.md") is True
    assert store.exists("a") is False
    store.delete("a/b.md")
    assert store.exists("a/b.md") is False
    store.delete("a/b.md")  # a missing key is not an error
    store.delete("never/was.md")


def case_list_is_sorted_and_uses_a_plain_string_prefix(store: Store) -> None:
    assert store.list_keys() == []
    for key in ("w/x/results/0002.json", "w/x/results/0001.json", "w/x/manifest.json", "w/xy/a"):
        store.write(key, b"-")
    assert store.list_keys() == [
        "w/x/manifest.json",
        "w/x/results/0001.json",
        "w/x/results/0002.json",
        "w/xy/a",
    ]
    assert store.list_keys("w/x/results/") == ["w/x/results/0001.json", "w/x/results/0002.json"]
    # A plain string prefix, not a directory: "w/x" also matches "w/xy/…".
    assert store.list_keys("w/x") == store.list_keys()
    assert store.list_keys("w/x/results/0002") == ["w/x/results/0002.json"]
    assert store.list_keys("nothing/") == []


def case_delete_prefix_and_its_refusals(store: Store) -> None:
    for key in ("work/a/1", "work/a/sub/2", "work/ab/3", "out/a/4"):
        store.write(key, b"-")
    for refused in ("", "work/a", "/", "work//", "../"):
        with pytest.raises(ValueError):
            store.delete_prefix(refused)
    assert len(store.list_keys()) == 4  # a refusal deletes nothing
    store.delete_prefix("work/a/")
    assert store.list_keys() == ["out/a/4", "work/ab/3"]
    store.delete_prefix("work/a/")  # nothing left under it: not an error


def case_key_validation(store: Store) -> None:
    store.write("keep", b"kept")
    operations: list[Callable[[str], object]] = [
        lambda k: store.read(k),
        lambda k: store.write(k, b"x"),
        lambda k: store.write_if_absent(k, b"x"),
        lambda k: store.exists(k),
        lambda k: store.delete(k),
        lambda k: store.location(k),
        lambda k: store.local_file(k).__enter__(),
    ]
    for key in BAD_KEYS:
        for operation in operations:
            with pytest.raises(ValueError):
                operation(key)
    assert store.list_keys() == ["keep"]


def case_location_and_local_file(store: Store) -> None:
    assert store.location("pages/0001.pdf").endswith("pages/0001.pdf")  # no I/O, no such key
    store.write("pages/0001.pdf", b"%PDF-page")
    with store.local_file("pages/0001.pdf") as path:
        assert path.name == "0001.pdf"  # callers read the page number from the name
        assert path.read_bytes() == b"%PDF-page"
    with pytest.raises(StoreNotFoundError), store.local_file("pages/0002.pdf"):
        pass


CONTRACT_CASES: list[Callable[[Store], None]] = [
    case_read_and_atomic_write,
    case_write_if_absent_first_write_wins,
    case_exists_and_delete,
    case_list_is_sorted_and_uses_a_plain_string_prefix,
    case_delete_prefix_and_its_refusals,
    case_key_validation,
    case_location_and_local_file,
]


@pytest.mark.parametrize("case", CONTRACT_CASES, ids=lambda case: str(case.__name__))
def test_local_store_contract(case: Callable[[Store], None], tmp_path: Path) -> None:
    case(LocalStore(tmp_path / "root"))


def test_local_write_if_absent_race_has_exactly_one_winner(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    writers = 16
    payloads = [bytes([65 + n]) * 200_000 for n in range(writers)]
    barrier = threading.Barrier(writers)
    created: list[bool] = [False] * writers

    def write(index: int) -> None:
        barrier.wait()
        created[index] = store.write_if_absent("results/0001.json", payloads[index])

    threads = [threading.Thread(target=write, args=(n,)) for n in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert created.count(True) == 1
    assert store.read("results/0001.json") == payloads[created.index(True)]
    assert os.listdir(tmp_path / "results") == ["0001.json"]  # every temp file is gone


def test_local_leftover_temp_file_is_never_listed_reported_or_read(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    store.write("work/x/results/0001.json", b"{}")
    leftover = f"work/x/results/0002.json{_TMP_MARKER}4242.deadbeef"
    (tmp_path / leftover).write_bytes(b"half a rec")

    assert store.list_keys() == ["work/x/results/0001.json"]
    assert store.list_keys("work/x/results/0002") == []
    assert store.exists(leftover) is False
    with pytest.raises(StoreNotFoundError):
        store.read(leftover)
    # The key it was meant for is simply absent, and can still be written first.
    assert store.exists("work/x/results/0002.json") is False
    assert store.write_if_absent("work/x/results/0002.json", b"whole") is True

    store.delete_prefix("work/x/")  # leftover temp files go with the tree
    assert not (tmp_path / "work" / "x").exists()


def test_open_store_from_a_location_string(tmp_path: Path) -> None:
    store = open_store(str(tmp_path / "loc"))
    assert isinstance(store, LocalStore)
    assert store.location("out/x/output.md") == str(tmp_path / "loc" / "out" / "x" / "output.md")
    assert Path(store.location("a")).is_absolute()
    assert isinstance(open_store(tmp_path), LocalStore)

    for bad in ("", "  ", "gs://bucket/x", "s3://", "s3:///prefix-only"):
        with pytest.raises(ValueError):
            open_store(bad, region="ap-southeast-2")
