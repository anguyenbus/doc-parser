"""
test_parse_pages_script.py — ``scripts/parse_pages.py`` on the configured store location.

Offline: ``parse_to_markdown`` is the fake from ``test_page_pipeline.py`` and pages
run in-process (``--workers 0 --no-escalate``). S3 is botocore's Stubber on a
client supplied by replacing the factory (``aws.s3_client``). A complete run
against an S3 location is not scripted through Stubber (that is the live check):
the S3-location cases replace the pipeline call and check the wiring.

Run ONLY these tests:
    uv run pytest tests/test_parse_pages_script.py
"""

from __future__ import annotations

import io
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from test_page_pipeline import THREE_PAGE, _install_fake, _result
from test_scripts_rewiring import _load_script

from parser_service.page_pipeline import file_id_for, work_id_for
from parser_service.storage import aws
from parser_service.storage.store import S3Store

REGION = "ap-southeast-2"
FILE_ID = file_id_for(THREE_PAGE)
WORK_ID = work_id_for(THREE_PAGE)
_ENV = (
    "PARSER_STORE_LOCATION",
    "AWS_REGION",
    "BEDROCK_VLM_MODEL",
    "PARSER_ESCALATION_ENGINE",
    "PARSER_PAGE_WORKERS",
    "PARSER_PAGE_MAX_ATTEMPTS",
    "PARSER_SCAN_FASTPATH",
    "PARSER_ESCALATION_ARBITRATION",
)


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The script as a module, with none of its settings in the environment."""
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    return _load_script("parse_pages")


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Stubber, list[str | None]]]:
    """A Stubber on the client the factory now returns, and the regions it was asked for."""
    client = boto3.client(
        "s3", region_name=REGION, aws_access_key_id="test", aws_secret_access_key="test"
    )
    regions: list[str | None] = []

    def factory(region: str | None = None) -> Any:
        regions.append(region)
        return client

    monkeypatch.setattr(aws, "s3_client", factory)
    with Stubber(client) as stubber:
        yield stubber, regions
        stubber.assert_no_pending_responses()


def _main(script: Any, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    """Run the script's ``main`` with ``argv``; return its exit code (0 when it returns)."""
    monkeypatch.setattr(sys, "argv", ["parse_pages.py", *argv])
    try:
        script.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def _pdf_body() -> dict[str, Any]:
    data = THREE_PAGE.read_bytes()
    return {"Body": StreamingBody(io.BytesIO(data), len(data))}


def test_location_from_the_environment_runs_end_to_end(
    script: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    location = tmp_path / "location"
    monkeypatch.setenv("PARSER_STORE_LOCATION", str(location))
    copy = tmp_path / "copies" / "doc.md"

    code = _main(
        script,
        monkeypatch,
        *("--input", str(THREE_PAGE), "--workers", "0", "--no-escalate", "--output", str(copy)),
    )

    assert code == 0
    err = capsys.readouterr().err
    assert "COMPLETE: 3 pages — 3 ok, 0 degraded, 0 failed, 0 pending" in err
    assert f"work dir: {location / 'work' / WORK_ID}\n" in err
    assert f"status: {location / 'out' / FILE_ID / 'status.json'}\n" in err
    assert f"document: {location / 'out' / FILE_ID / 'output.md'}\n" in err
    assert (location / "work" / WORK_ID / "source.pdf").read_bytes() == THREE_PAGE.read_bytes()
    # --output is read through the store and written to the local path given.
    assert copy.read_bytes() == (location / "out" / FILE_ID / "output.md").read_bytes()
    assert copy.read_text().startswith("==== page 1 ===\n\ntext of page 1")


def test_work_dir_overrides_the_environment_and_neither_set_exits_1(
    script: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    args = ("--input", str(THREE_PAGE), "--workers", "0", "--no-escalate")

    assert _main(script, monkeypatch, *args) == 1
    err = capsys.readouterr().err
    assert err == "Error: no store location: set PARSER_STORE_LOCATION or pass --work-dir\n"

    monkeypatch.setenv("PARSER_STORE_LOCATION", str(tmp_path / "from-env"))
    assert _main(script, monkeypatch, *args, "--work-dir", str(tmp_path / "from-flag")) == 0
    assert (tmp_path / "from-flag" / "out" / FILE_ID / "output.md").is_file()
    assert not (tmp_path / "from-env").exists()
    capsys.readouterr()

    # Escalation on with no AWS setting: refused at start-up, every problem on one line.
    assert _main(script, monkeypatch, "--input", str(THREE_PAGE)) == 1
    err = capsys.readouterr().err.strip()
    assert err.startswith("Error: ") and "\n" not in err
    assert "AWS_REGION is required" in err and "BEDROCK_VLM_MODEL is required" in err


def test_exit_codes_and_partial_and_restart_on_a_local_location(
    script: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    healthy = {"now": False}

    def by_page(n: int) -> dict[str, Any]:
        if n == 2 and not healthy["now"]:
            raise RuntimeError("boom")
        return _result(f"text of page {n}")

    calls = _install_fake(monkeypatch, by_page)
    location = tmp_path / "location"
    args: tuple[str, ...] = ("--input", str(THREE_PAGE), "--work-dir", str(location))
    args = (*args, "--workers", "0", "--no-escalate")

    assert _main(script, monkeypatch, *args, "--partial") == 2  # not complete
    err = capsys.readouterr().err
    assert "FAILED: 3 pages — 2 ok, 0 degraded, 1 failed, 0 pending" in err
    assert "page 2: failed — exception: RuntimeError: boom (will retry on re-run)" in err
    assert f"partial document: {location / 'out' / FILE_ID / 'output.partial.md'}\n" in err
    assert not (location / "out" / FILE_ID / "output.md").exists()

    healthy["now"] = True
    calls.clear()
    assert _main(script, monkeypatch, *args) == 0  # the re-run resumes: complete
    assert calls == [2]

    calls.clear()
    assert _main(script, monkeypatch, *args, "--restart") == 0
    assert sorted(calls) == [1, 2, 3]
    assert not (location / "out" / FILE_ID / "output.partial.md").exists()  # out/ went too

    calls.clear()
    monkeypatch.setenv("PARSER_ESCALATION_ENGINE", "textract")  # a changed config: own work dir
    assert _main(script, monkeypatch, *args) == 0
    assert sorted(calls) == [1, 2, 3]
    assert len(list((location / "work" / WORK_ID.partition("/")[0]).iterdir())) == 2
    monkeypatch.delenv("PARSER_ESCALATION_ENGINE")
    assert _main(script, monkeypatch, "--input", str(tmp_path / "no.pdf"), *args[2:]) == 1
    assert _main(script, monkeypatch, "--input", str(tmp_path / "x.docx"), *args[2:]) == 1


def test_s3_input_is_downloaded_under_its_base_name_and_cleaned_up(
    script: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed: tuple[Stubber, list[str | None]],
) -> None:
    stubber, regions = stubbed
    _install_fake(monkeypatch, lambda n: _result(f"text of page {n}"))
    monkeypatch.setenv("AWS_REGION", REGION)
    sources: list[Path] = []
    real = script.parse_pdf_pages_in_store

    def spying(source: Path, *args: Any, **kwargs: Any) -> dict[str, Any]:
        sources.append(Path(source))
        assert [p.name for p in Path(source).parent.iterdir()] == ["renamed.pdf"]
        if kwargs.get("restart"):
            raise RuntimeError("the run blew up")
        result: dict[str, Any] = real(source, *args, **kwargs)
        return result

    monkeypatch.setattr(script, "parse_pdf_pages_in_store", spying)
    request = {"Bucket": "input-bucket", "Key": "inbox/2026/renamed.pdf"}
    args: tuple[str, ...] = ("--input", "s3://input-bucket/inbox/2026/renamed.pdf")
    args = (*args, "--work-dir", str(tmp_path))
    args = (*args, "--workers", "0", "--no-escalate")

    stubber.add_response("get_object", _pdf_body(), request)
    assert _main(script, monkeypatch, *args) == 0

    assert regions == [REGION]  # the region comes from settings, explicitly
    file_id = f"renamed-{FILE_ID.rpartition('-')[2]}"  # key stem + content hash
    assert (tmp_path / "work" / WORK_ID / "source.pdf").read_bytes() == THREE_PAGE.read_bytes()
    assert (tmp_path / "out" / file_id / "output.md").is_file()
    assert sources[0].name == "renamed.pdf" and not sources[0].parent.exists()

    stubber.add_response("get_object", _pdf_body(), request)
    with pytest.raises(RuntimeError, match="the run blew up"):  # not caught, not relabelled
        _main(script, monkeypatch, *args, "--restart")
    assert len(sources) == 2 and not sources[1].parent.exists()


def test_missing_s3_object_and_non_pdf_key_exit_1_with_one_line(
    script: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stubbed: tuple[Stubber, list[str | None]],
) -> None:
    stubber, _regions = stubbed
    monkeypatch.setenv("AWS_REGION", REGION)
    tail = ("--work-dir", str(tmp_path / "location"), "--no-escalate")

    stubber.add_client_error("get_object", "NoSuchKey", "The specified key does not exist.", 404)
    assert _main(script, monkeypatch, "--input", "s3://input-bucket/gone.pdf", *tail) == 1
    assert capsys.readouterr().err == "Error: s3://input-bucket/gone.pdf does not exist\n"

    # Refused before any call: nothing is scripted for these. (Any key is accepted
    # since Group 3a: the content decides the kind, so notes.docx is no longer here.)
    for uri in ("s3://input-bucket/", "s3://input-bucket"):
        assert _main(script, monkeypatch, "--input", uri, *tail) == 1
        err = capsys.readouterr().err
        assert err.startswith("Error: ") and err.count("\n") == 1
    assert not (tmp_path / "location").exists()

    monkeypatch.delenv("AWS_REGION")  # an s3:// input needs the region: a settings error
    assert _main(script, monkeypatch, "--input", "s3://input-bucket/a.pdf", *tail) == 1
    assert capsys.readouterr().err == "Error: AWS_REGION is required (the input is s3://)\n"


def test_s3_location_reaches_the_store_and_locations_print_as_uris(
    script: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stubbed: tuple[Stubber, list[str | None]],
) -> None:
    stubber, regions = stubbed
    monkeypatch.setenv("PARSER_STORE_LOCATION", "s3://store-bucket/parser/")
    monkeypatch.setenv("AWS_REGION", REGION)
    seen: dict[str, Any] = {}

    def fake_pipeline(
        source: Path, location: str, region: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        store = kwargs["store"]
        seen.update(source=source, location=location, region=region, **kwargs)
        complete = not kwargs["partial"]
        name = "output.md" if complete else "output.partial.md"
        return {
            "state": "complete" if complete else "failed",
            "file_id": FILE_ID,
            "work_id": WORK_ID,
            "work_dir": store.location(f"work/{WORK_ID}"),
            "out_dir": store.location(f"out/{FILE_ID}"),
            "page_count": 3,
            "counts": {"ok": 3 if complete else 2, "degraded": 0, "failed": 0, "pending": 0},
            "not_ok_pages": [],
            "pages": [],
            "status_path": store.location(f"out/{FILE_ID}/status.json"),
            "output_path": store.location(f"out/{FILE_ID}/{name}") if complete else None,
            "partial_path": None if complete else store.location(f"out/{FILE_ID}/{name}"),
            "output_key": f"out/{FILE_ID}/{name}" if complete else None,
            "partial_key": None if complete else f"out/{FILE_ID}/{name}",
            "elapsed_seconds": 0.0,
        }

    monkeypatch.setattr(script, "parse_pdf_pages_in_store", fake_pipeline)
    copy = tmp_path / "doc.md"
    args = ("--input", str(THREE_PAGE), "--no-escalate", "--workers", "2")
    stubber.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(b"stitched"), 8)},
        {"Bucket": "store-bucket", "Key": f"parser/out/{FILE_ID}/output.md"},
    )

    assert _main(script, monkeypatch, *args, "--restart", "--output", str(copy)) == 0

    store = seen["store"]
    assert isinstance(store, S3Store)
    assert (store.bucket, store.prefix, store.region) == ("store-bucket", "parser", REGION)
    assert regions == [REGION]
    assert (seen["location"], seen["region"]) == ("s3://store-bucket/parser/", REGION)
    assert (seen["workers"], seen["escalate"], seen["restart"]) == (2, False, True)
    base = "s3://store-bucket/parser"
    err = capsys.readouterr().err
    assert f"work dir: {base}/work/{WORK_ID}\n" in err
    assert f"status: {base}/out/{FILE_ID}/status.json\n" in err
    assert f"document: {base}/out/{FILE_ID}/output.md\n" in err
    assert copy.read_bytes() == b"stitched"  # --output: read through the store

    assert _main(script, monkeypatch, *args, "--partial") == 2
    assert f"partial document: {base}/out/{FILE_ID}/output.partial.md\n" in capsys.readouterr().err
    assert seen["partial"] is True and seen["restart"] is False
