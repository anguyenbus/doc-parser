"""
test_store_live_s3.py — the interface-level live check of the S3 store (real AWS).

LIVE: calls S3 in ``ap-southeast-2`` and nothing else (the pipeline runs use
``--no-escalate``). Never run by the offline suite (``-m "not live and not slow"``).

Every read, write, list and delete stays under
``s3://de-genai-dev/parser/live-check/<run id>/`` with a fresh run id per run. The
run prefix must be empty before the first write, so every key found there at the
end was written by this run; only those keys are deleted. No bucket is created and
the placeholder key ``parser/`` is never touched.

Steps (spec requirement 10):
    1. access            the run prefix is empty; one small write
    2. conditional write the same key again with write-if-absent returns False and
                         the stored bytes are unchanged; plus missing-key read,
                         exists, list-by-prefix, delete and delete-by-prefix
    3. three pages       ``tests/fixtures/three_page_digital.pdf`` run twice (the
                         second run parses nothing), then ``--restart``
    4. 107 pages         ``247136389-5MB.pdf``: wall time and total stored bytes
    5. clean-up          delete every key under the run prefix; confirm it is empty

A JSON report of everything observed is written, step by step, to the path in
``LIVE_CHECK_REPORT`` (default: pytest's temporary directory).

Run:
    AWS_REGION=ap-southeast-2 LIVE_CHECK_REPORT=<file.json> \
        uv run pytest -m live tests/test_store_live_s3.py -s
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from parser_service import io_layer
from parser_service.page_pipeline import file_id_for
from parser_service.store import Store, StoreNotFoundError, open_store

pytestmark = pytest.mark.live

REGION = "ap-southeast-2"
BUCKET = "de-genai-dev"
BASE_PREFIX = "parser/live-check/"
REPO = Path(__file__).parent.parent
SCRIPT = REPO / "scripts" / "parse_pages.py"
THREE_PAGE = REPO / "tests" / "fixtures" / "three_page_digital.pdf"
LARGE = REPO / "247136389-5MB.pdf"
WORKERS = 4

# Settings that would change the pinned config or turn engine calls on.
_CLEARED_ENV = (
    "BEDROCK_VLM_MODEL",
    "PARSER_ESCALATION_ENGINE",
    "PARSER_SCAN_FASTPATH",
    "PARSER_ESCALATION_ARBITRATION",
    "PARSER_PAGE_WORKERS",
    "PARSER_PAGE_MAX_ATTEMPTS",
)


def _listing(client: Any, prefix: str) -> list[dict[str, Any]]:
    """Every object under ``prefix`` (folder markers included): key, size, ETag, time."""
    assert prefix.startswith(BASE_PREFIX) and prefix.endswith("/"), prefix
    objects: list[dict[str, Any]] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            objects.append(
                {
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "etag": obj["ETag"],
                    "last_modified": obj["LastModified"].isoformat(),
                }
            )
    return sorted(objects, key=lambda o: str(o["key"]))


def _run_script(location: str, source: Path, *extra: str) -> dict[str, Any]:
    """Run ``scripts/parse_pages.py`` on the S3 location with engine calls off."""
    env = {k: v for k, v in os.environ.items() if k not in _CLEARED_ENV}
    env["AWS_REGION"] = REGION
    env["PARSER_STORE_LOCATION"] = location
    command = [
        sys.executable,
        str(SCRIPT),
        "--input",
        str(source),
        "--no-escalate",
        "--workers",
        str(WORKERS),
        *extra,
    ]
    started = time.perf_counter()
    done = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
    wall = round(time.perf_counter() - started, 3)
    to_process = re.search(r"(\d+) pages, (\d+) to process, (\d+) already done", done.stderr)
    return {
        "command": f"AWS_REGION={REGION} PARSER_STORE_LOCATION={location} uv run python "
        f"scripts/parse_pages.py --input {source.relative_to(REPO)} --no-escalate "
        f"--workers {WORKERS}" + "".join(f" {arg}" for arg in extra),
        "exit_code": done.returncode,
        "wall_seconds": wall,
        "pages_to_process": int(to_process.group(2)) if to_process else None,
        "pages_already_done": int(to_process.group(3)) if to_process else None,
        "summary_lines": [
            line
            for line in done.stderr.splitlines()
            if re.match(
                r"^(COMPLETE|FAILED|IN_PROGRESS|work dir|status|document|Error|  page )", line
            )
        ],
        "log_line": to_process.group(0) if to_process else None,
        "stderr_tail": done.stderr.splitlines()[-25:],
    }


def _records(store: Store, file_id: str) -> dict[str, dict[str, Any]]:
    """The page records of a file (archives excluded), by key."""
    keys = [
        key
        for key in store.list_keys(f"work/{file_id}/results/")
        if re.fullmatch(r"\d{4}\.json", key.rpartition("/")[2])
    ]
    return {key: json.loads(store.read(key)) for key in keys}


def test_live_s3_store_and_pipeline(tmp_path: Path) -> None:
    assert os.environ.get("AWS_REGION") == REGION, f"set AWS_REGION={REGION}"
    run_id = f"{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    run_prefix = f"{BASE_PREFIX}{run_id}/"
    location = f"s3://{BUCKET}/{run_prefix}"
    report_path = Path(os.environ.get("LIVE_CHECK_REPORT") or tmp_path / "live-check.json")
    report: dict[str, Any] = {"run_id": run_id, "location": location, "region": REGION}

    def save() -> None:
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    client = io_layer.s3_client(REGION)
    store = open_store(location, region=REGION)
    written_by_run = False
    try:
        # ---- Step 1: access -------------------------------------------------
        before = _listing(client, run_prefix)
        report["step1_prefix_listing_before"] = before
        save()
        assert before == [], "the run prefix is not empty; nothing was written"

        written_by_run = True
        first = b"first write " + run_id.encode()
        started = time.perf_counter()
        created = store.write_if_absent("check/conditional.txt", first)
        report["step1_first_write"] = {
            "key": f"{run_prefix}check/conditional.txt",
            "write_if_absent_returned": created,
            "seconds": round(time.perf_counter() - started, 3),
        }
        save()
        assert created is True

        # ---- Step 2: the conditional write, and the rest of the interface ---
        second = store.write_if_absent("check/conditional.txt", b"SECOND WRITE, must not land")
        stored = store.read("check/conditional.txt")
        report["step2_conditional_write"] = {
            "second_write_if_absent_returned": second,
            "stored_bytes_equal_first": stored == first,
        }
        save()
        assert second is False
        assert stored == first

        missing_read = "no error"
        try:
            store.read("check/never-written.txt")
        except StoreNotFoundError:
            missing_read = "StoreNotFoundError"
        except Exception as exc:  # noqa: BLE001 — recorded, then asserted below
            missing_read = f"{type(exc).__name__}: {exc}"
        missing_file = "no error"
        try:
            with store.local_file("check/never-written.pdf"):
                pass
        except StoreNotFoundError:
            missing_file = "StoreNotFoundError"
        except Exception as exc:  # noqa: BLE001
            missing_file = f"{type(exc).__name__}: {exc}"
        report["step2_missing_key"] = {
            "read_raised": missing_read,
            "local_file_raised": missing_file,
            "exists_missing": store.exists("check/never-written.txt"),
            "exists_present": store.exists("check/conditional.txt"),
        }
        save()
        assert missing_read == "StoreNotFoundError"
        assert missing_file == "StoreNotFoundError"
        assert report["step2_missing_key"]["exists_missing"] is False
        assert report["step2_missing_key"]["exists_present"] is True

        store.write("check/plain.json", b'{"v": 1}\n')
        store.write("check/plain.json", b'{"v": 2}\n')
        store.write("check/sub/a.md", b"a\n")
        store.write("check/sub/b.md", b"b\n")
        store.write("check/subway.md", b"not under check/sub/\n")
        report["step2_list"] = {
            "list_keys('check/')": store.list_keys("check/"),
            "list_keys('check/sub')": store.list_keys("check/sub"),
            "list_keys('check/sub/')": store.list_keys("check/sub/"),
            "list_keys('check/nothing/')": store.list_keys("check/nothing/"),
            "overwrite_read_back": store.read("check/plain.json").decode(),
            "content_types": {
                key: client.head_object(Bucket=BUCKET, Key=f"{run_prefix}{key}")["ContentType"]
                for key in ("check/plain.json", "check/sub/a.md", "check/conditional.txt")
            },
        }
        save()
        assert report["step2_list"]["list_keys('check/sub/')"] == [
            "check/sub/a.md",
            "check/sub/b.md",
        ]
        assert report["step2_list"]["list_keys('check/nothing/')"] == []
        assert report["step2_list"]["overwrite_read_back"] == '{"v": 2}\n'

        store.delete("check/plain.json")
        store.delete("check/plain.json")  # a missing key is not an error
        store.delete_prefix("check/sub/")
        report["step2_delete"] = {
            "after_delete_and_delete_prefix('check/sub/')": store.list_keys("check/"),
            "deleted": [
                f"{run_prefix}check/plain.json",
                f"{run_prefix}check/sub/a.md",
                f"{run_prefix}check/sub/b.md",
            ],
        }
        save()
        assert store.list_keys("check/") == ["check/conditional.txt", "check/subway.md"]

        # ---- Step 3: the three-page fixture, twice, then --restart ----------
        file_id = file_id_for(THREE_PAGE)
        step3: dict[str, Any] = {"file_id": file_id}
        report["step3_three_page"] = step3

        # With engine calls off this fixture's pages may be promoted by the gate and
        # recorded ``degraded`` (exit 2). The check is about storage, so both outcomes
        # are accepted: an ``ok`` page must be left untouched by the second run, and a
        # non-ok page must be archived (``NNNN.attempt-1.json``) and tried again.
        step3["run1"] = _run_script(location, THREE_PAGE)
        records1 = _records(store, file_id)
        listing1 = {o["key"]: o for o in _listing(client, f"{run_prefix}work/{file_id}/")}
        step3["run1_keys"] = _listing(client, run_prefix)
        step3["run1_worker_pids"] = sorted({r["worker_pid"] for r in records1.values()})
        step3["run1_states"] = {k.rpartition("/")[2]: r["state"] for k, r in records1.items()}
        save()
        assert step3["run1"]["exit_code"] in (0, 2), step3["run1"]["stderr_tail"]
        assert len(records1) == 3
        ok_keys = sorted(k for k, r in records1.items() if r["state"] == "ok")
        not_ok_keys = sorted(k for k, r in records1.items() if r["state"] != "ok")

        step3["run2"] = _run_script(location, THREE_PAGE)
        records2 = _records(store, file_id)
        listing2 = {o["key"]: o for o in _listing(client, f"{run_prefix}work/{file_id}/")}
        full_ok = [f"{run_prefix}{k}" for k in ok_keys]
        archives = sorted(k for k in listing2 if ".attempt-" in k)
        step3["run2_ok_pages"] = len(ok_keys)
        step3["run2_ok_records_unchanged"] = all(
            records1[k] == records2[k] for k in ok_keys
        ) and all(listing1[k] == listing2[k] for k in full_ok)
        step3["run2_archives"] = archives
        step3["run2_archives_equal_run1_records"] = all(
            json.loads(store.read(k.replace(".json", ".attempt-1.json"))) == records1[k]
            for k in not_ok_keys
        )
        step3["run2_attempts"] = {
            k.rpartition("/")[2]: records2[k]["attempt"] for k in sorted(records2)
        }
        step3["run2_states"] = {k.rpartition("/")[2]: r["state"] for k, r in records2.items()}
        step3["run2_keys"] = [o["key"] for o in _listing(client, run_prefix)]
        save()
        assert step3["run2"]["exit_code"] in (0, 2), step3["run2"]["stderr_tail"]
        assert step3["run2"]["pages_to_process"] == len(not_ok_keys)
        assert step3["run2_ok_records_unchanged"]
        assert len(archives) == len(not_ok_keys)
        assert step3["run2_archives_equal_run1_records"]
        assert all(records2[k]["attempt"] == 2 for k in not_ok_keys)

        # A key the pipeline does not know, to show --restart removes the whole prefix.
        store.write(f"work/{file_id}/stray/marker.txt", b"removed by --restart\n")
        store.write(f"out/{file_id}/stray.txt", b"removed by --restart\n")
        step3["restart"] = _run_script(location, THREE_PAGE, "--restart")
        records3 = _records(store, file_id)
        step3["restart_stray_work_key_exists"] = store.exists(f"work/{file_id}/stray/marker.txt")
        step3["restart_stray_out_key_exists"] = store.exists(f"out/{file_id}/stray.txt")
        step3["restart_attempts"] = {
            k.rpartition("/")[2]: records3[k]["attempt"] for k in sorted(records3)
        }
        step3["restart_keys"] = [o["key"] for o in _listing(client, run_prefix)]
        save()
        assert step3["restart"]["exit_code"] in (0, 2), step3["restart"]["stderr_tail"]
        assert step3["restart"]["pages_to_process"] == 3
        assert not step3["restart_stray_work_key_exists"]
        assert not step3["restart_stray_out_key_exists"]
        assert all(r["attempt"] == 1 for r in records3.values())
        assert not [k for k in step3["restart_keys"] if ".attempt-" in k]

        # ---- Step 4: the 107-page document -----------------------------------
        large_id = file_id_for(LARGE)
        step4: dict[str, Any] = {"file_id": large_id, "source_bytes": LARGE.stat().st_size}
        report["step4_107_pages"] = step4
        step4["run"] = _run_script(location, LARGE)
        objects = _listing(client, f"{run_prefix}work/{large_id}/") + _listing(
            client, f"{run_prefix}out/{large_id}/"
        )
        groups: dict[str, dict[str, int]] = {}
        for obj in objects:
            relative = obj["key"][len(run_prefix) :]
            parts = relative.split("/")
            name = "/".join(parts[:1] + parts[2:3]) if len(parts) > 3 else f"{parts[0]}/{parts[-1]}"
            if parts[2] == "results":
                name = f"work/results/*{Path(parts[-1]).suffix}"
            group = groups.setdefault(name, {"keys": 0, "bytes": 0})
            group["keys"] += 1
            group["bytes"] += obj["size"]
        large_records = _records(store, large_id)
        step4["stored_keys"] = len(objects)
        step4["stored_bytes"] = sum(o["size"] for o in objects)
        step4["by_group"] = groups
        step4["status"] = json.loads(store.read(f"out/{large_id}/status.json"))["counts"]
        step4["state"] = json.loads(store.read(f"out/{large_id}/status.json"))["state"]
        step4["worker_pids"] = sorted({r["worker_pid"] for r in large_records.values()})
        step4["records"] = len(large_records)
        save()
        assert step4["run"]["exit_code"] in (0, 2), step4["run"]["stderr_tail"]
        assert step4["records"] == 107

        # Resume on S3: the second run leaves every ``ok`` page alone and re-drives
        # only the pages that are not ok.
        large_ok = sorted(k for k, r in large_records.items() if r["state"] == "ok")
        etags_before = {
            o["key"]: o["etag"] for o in _listing(client, f"{run_prefix}work/{large_id}/results/")
        }
        step4["run2"] = _run_script(location, LARGE)
        large_records2 = _records(store, large_id)
        etags_after = {
            o["key"]: o["etag"] for o in _listing(client, f"{run_prefix}work/{large_id}/results/")
        }
        step4["run2_ok_pages_before"] = len(large_ok)
        step4["run2_ok_records_unchanged"] = all(
            large_records[k] == large_records2[k]
            and etags_before[f"{run_prefix}{k}"] == etags_after[f"{run_prefix}{k}"]
            for k in large_ok
        )
        step4["run2_archives"] = sorted(k for k in etags_after if ".attempt-" in k)
        step4["run2_status"] = json.loads(store.read(f"out/{large_id}/status.json"))["counts"]
        save()
        assert step4["run2"]["pages_to_process"] == 107 - len(large_ok)
        assert step4["run2_ok_records_unchanged"]
    finally:
        # ---- Step 5: clean-up ------------------------------------------------
        if written_by_run:
            cleanup: dict[str, Any] = {}
            report["step5_cleanup"] = cleanup
            try:
                found = _listing(client, run_prefix)
                cleanup["keys_found"] = len(found)
                cleanup["bytes_found"] = sum(o["size"] for o in found)
                cleanup["keys"] = [{"key": o["key"], "size": o["size"]} for o in found]
                save()
                tops = sorted({o["key"][len(run_prefix) :].split("/")[0] for o in found})
                started = time.perf_counter()
                for top in tops:
                    store.delete_prefix(f"{top}/")
                cleanup["delete_seconds"] = round(time.perf_counter() - started, 3)
                cleanup["deleted_prefixes"] = [f"{run_prefix}{top}/" for top in tops]
                cleanup["listing_after"] = _listing(client, run_prefix)
            except Exception as exc:  # noqa: BLE001 — recorded; the keys are left in place
                cleanup["error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                save()
            assert cleanup["listing_after"] == []
        save()
