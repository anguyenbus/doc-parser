"""
test_observability.py — metrics, structured log events and the alert rule (spec D9).

Offline: ``parse_to_markdown`` is faked (``test_page_pipeline._install_fake``),
non-PDF kinds get an in-process test handler (``_enable_test_kind``), and the page
service side is driven through ``process_page_request`` with a fake runner. Metric
values are read from ``prometheus_client``'s default registry as deltas.

Run ONLY these tests:
    uv run pytest tests/test_observability.py
"""

from __future__ import annotations

import json
import logging
import os
import string
import subprocess
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from test_page_pipeline import THREE_PAGE, XLSX, _enable_test_kind, _install_fake, _result

from parser_service import observability, page_pipeline
from parser_service.delivery.publish import PublishRequest
from parser_service.formats.sniff import KINDS
from parser_service.service.worker import Reply, Request
from parser_service.storage.store import LocalStore

prometheus_client = pytest.importorskip("prometheus_client")

K8S = Path(__file__).parent.parent / "deploy" / "k8s"
MARKER = "QZX7-OBSERVABILITY-MARKER-4f1c"

# The label set of every service metric (spec D9).
SERVICE_LABELS = {
    "parser_guard_rejections": {"kind", "reason"},
    "parser_timeouts": {"kind"},
    "parser_units": {"kind", "state", "reason_code"},
    "parser_flags": {"kind", "flag"},
    "parser_parse_seconds": {"kind"},
    "parser_input_bytes": {"kind"},
    "parser_output_bytes": {"kind"},
    "parser_worker_restarts": {"cause"},
}


def _sample(name: str, **labels: str) -> float:
    return prometheus_client.REGISTRY.get_sample_value(name, labels) or 0.0


def _events(caplog: pytest.LogCaptureFixture, name: str | None = None) -> list[dict[str, Any]]:
    found = [
        json.loads(r.getMessage()) for r in caplog.records if r.name == observability.logger.name
    ]
    return [e for e in found if name is None or e["event"] == name]


class ScriptedRunner:
    """The parse worker's interface; replies by the unit file's first line."""

    pid = 4242

    def __init__(self, replies: dict[str, Reply]) -> None:
        self.replies = replies

    def run(self, request: Request, deadline_s: float) -> Reply:
        kind, unit_path, _page, _escalate, _cap = request
        first = Path(unit_path).read_text(errors="replace").splitlines()[0] if kind != "pdf" else ""
        return self.replies[first if kind != "pdf" else "pdf"]


def test_metrics_and_events_emitted_per_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from parser_service import settings

    observability.service_metrics()  # registered, as the page service does at import
    # Every service metric is on the default registry with exactly the D9 labels.
    registered = {m.name: m for m in prometheus_client.REGISTRY.collect()}
    for name, labels in SERVICE_LABELS.items():
        assert name in registered, name
        metric = registered[name]
        for sample in metric.samples:
            assert set(sample.labels) - {"le"} == labels, (name, sample.labels)

    # --- the page service: one PDF page, two txt documents (one over its output cap)
    _enable_test_kind(monkeypatch, "txt")
    store = LocalStore(tmp_path / "service")
    pdf_id = page_pipeline.work_id_for(THREE_PAGE)
    page_pipeline._intake(store, pdf_id, THREE_PAGE, "pdf")
    txt_ids = []
    for name in ("short", "huge"):
        source = tmp_path / "in" / f"{name}.txt"
        source.parent.mkdir(exist_ok=True)
        source.write_text(f"{name}\nbody\n")
        work_id = page_pipeline.work_id_for(source, "txt")
        page_pipeline._intake(store, work_id, source, "txt")
        txt_ids.append(work_id)
    runner = ScriptedRunner(
        {
            "pdf": ("ok", None, None, "page text", {"flags": ["images_not_ocrd"]}),
            "short": ("ok", None, None, "short text", {}),
            "huge": (
                "failed",
                "output_too_large: 30000000 bytes",
                "output_too_large",
                "",
                {"limits": {"max_output_mb": 20}},
            ),
        }
    )
    before = {
        "pdf_ok": _sample("parser_units_total", kind="pdf", state="ok", reason_code="none"),
        "txt_ok": _sample("parser_units_total", kind="txt", state="ok", reason_code="none"),
        "txt_big": _sample(
            "parser_units_total", kind="txt", state="failed", reason_code="output_too_large"
        ),
        "guard": _sample("parser_guard_rejections_total", kind="txt", reason="output_too_large"),
        "flag": _sample("parser_flags_total", kind="pdf", flag="images_not_ocrd"),
        "seconds": _sample("parser_parse_seconds_count", kind="txt"),
        "input": _sample("parser_input_bytes_sum", kind="txt"),
        "output": _sample("parser_output_bytes_sum", kind="pdf"),
    }
    with caplog.at_level(logging.INFO):
        page_pipeline.process_page_request(store, pdf_id, 1, False, 3, runner=runner)
        for work_id in txt_ids:
            page_pipeline.process_page_request(store, work_id, 1, False, 3, runner=runner)

    after = {
        "pdf_ok": _sample("parser_units_total", kind="pdf", state="ok", reason_code="none"),
        "txt_ok": _sample("parser_units_total", kind="txt", state="ok", reason_code="none"),
        "txt_big": _sample(
            "parser_units_total", kind="txt", state="failed", reason_code="output_too_large"
        ),
        "guard": _sample("parser_guard_rejections_total", kind="txt", reason="output_too_large"),
        "flag": _sample("parser_flags_total", kind="pdf", flag="images_not_ocrd"),
        "seconds": _sample("parser_parse_seconds_count", kind="txt"),
        "input": _sample("parser_input_bytes_sum", kind="txt"),
        "output": _sample("parser_output_bytes_sum", kind="pdf"),
    }
    delta = {k: after[k] - before[k] for k in before}
    assert delta["pdf_ok"] == delta["txt_ok"] == delta["txt_big"] == 1
    assert delta["guard"] == delta["flag"] == 1 and delta["seconds"] == 2
    assert delta["input"] == len(b"short\nbody\n") + len(b"huge\nbody\n")
    assert delta["output"] == len(b"page text")

    units = _events(caplog, "unit_done")
    assert [(e["kind"], e["state"], e["reason_code"]) for e in units] == [
        ("pdf", "ok", None),
        ("txt", "ok", None),
        ("txt", "failed", "output_too_large"),
    ]
    assert units[0]["flags"] == ["images_not_ocrd"] and units[0]["unit"] == 1
    assert units[0]["work_id"] == pdf_id and units[0]["parse_worker_pid"] == 4242
    [guard] = _events(caplog, "guard_reject")
    assert (guard["kind"], guard["reason_code"]) == ("txt", "output_too_large")
    assert guard["limits"] == {"max_output_mb": 20}

    # --- the driver: file_intake and file_done per file, with the D9 fields
    caplog.clear()
    monkeypatch.setitem(settings.RAW_CAP_ENV, "txt", ("TEST_MAX_TXT_MB", 1))
    _install_fake(monkeypatch, lambda n: _result(f"p{n}"))
    inputs = tmp_path / "driver"
    inputs.mkdir()
    renamed = inputs / "report.docx"  # a PDF named .docx: routed to pdf, a mismatch
    renamed.write_bytes(THREE_PAGE.read_bytes())
    odd = inputs / "scan.weird"  # an extension outside the known set: "other"
    odd.write_bytes(THREE_PAGE.read_bytes() + b"\n")
    big = inputs / "notes.txt"
    big.write_bytes(b"plain text line\n" * 70_000)  # 1.12 MB, over the 1 MB cap
    sources = [renamed, odd, XLSX, big]
    with caplog.at_level(logging.INFO):
        results = [
            page_pipeline.parse_file_in_store(
                source, str(tmp_path / "store"), workers=0, publish=PublishRequest("doc-1", "job-1")
            )
            for source in sources
        ]
    assert [r["state"] for r in results] == ["complete", "complete", "failed", "failed"]

    intake = _events(caplog, "file_intake")
    done = _events(caplog, "file_done")
    assert [(e["kind"], e["detected_type"], e["extension"], e["mismatch"]) for e in intake] == [
        ("pdf", "pdf", "docx", True),
        ("pdf", "pdf", "other", True),
        ("unsupported", "xlsx", "xlsx", False),
        ("txt", "text", "txt", False),
    ]
    assert [e["reason_code"] for e in intake] == [None, None, "unsupported_type", "too_large"]
    assert [(e["kind"], e["state"], e["reason_code"], e["unit"]) for e in done] == [
        ("pdf", "complete", None, "page"),
        ("pdf", "complete", None, "page"),
        ("unsupported", "failed", "unsupported_type", "document"),
        ("txt", "failed", "too_large", "document"),
    ]
    for event in intake + done:
        assert set(event) <= observability.EVENT_FIELDS
        assert (event["job_id"], event["doc_id"]) == ("job-1", "doc-1")
        assert event["input_bytes"] > 0 and event["file_id"]
    assert done[0]["output_chars"] == len("p1") * 3 and done[0]["work_id"]
    assert done[2]["work_id"] is None and isinstance(done[3]["elapsed_seconds"], float)
    [too_large] = _events(caplog, "guard_reject")
    assert too_large["limits"] == {"raw_cap_bytes": 1_000_000}
    assert observability.extension_label("README") == "none"
    assert observability.extension_label("a.HTM") == "htm"

    # The driver's counters, derived from its events.
    counters = observability.driver_counters(_events(caplog))
    assert counters["parser_files_total"] == {("pdf",): 2, ("unsupported",): 1, ("txt",): 1}
    assert counters["parser_sniff_mismatch_total"] == {("pdf", "docx"): 1, ("pdf", "other"): 1}
    assert counters["parser_unsupported_total"] == {("xlsx",): 1}
    assert counters["parser_guard_rejections_total"] == {("txt", "too_large"): 1}


def _email_fixture(path: Path, body: str, attachment: str) -> Path:
    message = EmailMessage()
    message["From"] = f"{MARKER} <sender@example.com>"
    message["To"] = "someone@example.com"
    message["Subject"] = f"Quarterly {MARKER}"
    message["Date"] = "Tue, 14 Oct 2026 09:00:00 +1000"
    message["Message-ID"] = "<1@example.com>"
    message.set_content(body)
    message.add_attachment(
        b"%PDF-1.4\n", maintype="application", subtype="pdf", filename=attachment
    )
    path.write_bytes(message.as_bytes())
    return path


def test_logs_carry_no_document_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from email import message_from_bytes, policy

    from parser_service.formats import handlers as unit_handlers

    # A PDF whose page text holds the marker (the parse is faked: no Docling).
    _install_fake(monkeypatch, lambda n: _result(f"page {n} says {MARKER}"))
    # Email, routed to a test handler: the marker is in the text, the headers and
    # an attachment name; one message makes the handler raise with the marker
    # past the 500th character of the exception text.
    _enable_test_kind(monkeypatch, "eml")

    def handler(path: Path, page_number: int, escalate: bool) -> Reply:
        message: Any = message_from_bytes(path.read_bytes(), policy=policy.default)
        if "RAISE" in message.get_body(("plain",)).get_content():
            raise ValueError("e" * 600 + MARKER)
        email = {
            "headers": {"from": str(message["From"]), "subject": str(message["Subject"])},
            "attachments": [
                {"name": part.get_filename(), "type": part.get_content_type(), "size": 9}
                for part in message.iter_attachments()
            ],
        }
        text = f"# {message['Subject']}\n\nbody: {MARKER}"
        return "ok", None, None, text, {"email": email, "flags": ["images_not_ocrd"]}

    monkeypatch.setitem(unit_handlers.HANDLERS, "eml", handler)
    inputs = tmp_path / "in"
    inputs.mkdir()
    pdf = inputs / "report.pdf"
    pdf.write_bytes(THREE_PAGE.read_bytes())
    mail = _email_fixture(inputs / "mail.eml", f"Body text {MARKER}", f"{MARKER}.pdf")
    bad = _email_fixture(inputs / "bad.eml", f"RAISE {MARKER}", "plain.pdf")

    store_root = tmp_path / "store"
    with caplog.at_level(logging.DEBUG):
        results = [
            page_pipeline.parse_file_in_store(
                source, str(store_root), workers=0, publish=PublishRequest(source.stem, "job-1")
            )
            for source in (pdf, mail, bad)
        ]
        # The page service path too, from a fresh store.
        service = LocalStore(tmp_path / "service")
        work_id = page_pipeline.work_id_for(mail, "eml")
        page_pipeline._intake(service, work_id, mail, "eml")

        class Runner:
            pid = 7

            def run(self, request: Request, deadline_s: float) -> Reply:
                return handler(Path(request[1]), request[2], request[3])

        page_pipeline.process_page_request(service, work_id, 1, False, 3, runner=Runner())

    assert [r["state"] for r in results] == ["complete", "complete", "failed"]
    # The marker did reach the outputs, so its absence from the logs means something.
    assert MARKER in Path(results[1]["output_path"]).read_text()
    [document_json] = [
        p for p in (store_root / "published").rglob("document.json") if "mail" in p.parts
    ]
    assert MARKER in document_json.read_text()  # the email headers and attachment name
    [failed_unit] = results[2]["not_ok_pages"]
    assert failed_unit["reason"].startswith("exception: ValueError: eee")
    assert MARKER not in failed_unit["reason"]  # the exception text is cut at 500
    assert len(failed_unit["reason"]) <= len("exception: ") + 500

    assert _events(caplog, "unit_done") and _events(caplog, "file_done")
    logged = [caplog.text] + [r.getMessage() for r in caplog.records]
    captured = capsys.readouterr()
    for text in [*logged, captured.out, captured.err]:
        assert MARKER not in text
        assert "sender@example.com" not in text
    for record in caplog.records:
        assert len(record.getMessage()) < 5000  # nothing like a document in one line


def test_alert_rule_file_is_valid() -> None:
    yaml = pytest.importorskip("yaml")

    template = (K8S / "alerts.yaml.tpl").read_text()
    rendered = yaml.safe_load(string.Template(template).substitute(NAMESPACE="parser-test"))
    rules = [rule for group in rendered["groups"] for rule in group["rules"]]
    [rule] = [r for r in rules if r.get("alert") == "ParserKindFailureRateHigh"]
    assert rule["for"] == "15m"
    expr = " ".join(rule["expr"].split())
    # The failed share of finished files per kind over 1 h, above 10%, from file_done.
    assert expr.count("sum by (kind)") == 3 and expr.count("[1h]") == 3
    assert expr.count('event="file_done"') == 3 and expr.count('state="failed"') == 1
    assert ") > 0.10 and" in expr and expr.endswith(">= 20")
    assert 'namespace="parser-test"' in expr and "${" not in expr

    # The ConfigMap takes PARSER_ENABLED_KINDS from deploy/test.env, whose default (no
    # override in the environment) is every kind with a tested handler.
    env = {k: v for k, v in os.environ.items() if k != "PARSER_ENABLED_KINDS"}
    default = subprocess.run(
        ["bash", "-c", 'source deploy/test.env && printf %s "$PARSER_ENABLED_KINDS"'],
        cwd=K8S.parent.parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert default.split(",") == list(KINDS)
    namespace = string.Template((K8S / "namespace.yaml.tpl").read_text()).safe_substitute(
        PARSER_ENABLED_KINDS=default
    )
    configmap = [d for d in yaml.safe_load_all(namespace) if d["kind"] == "ConfigMap"]
    assert configmap[0]["data"]["PARSER_ENABLED_KINDS"].split(",") == list(KINDS)
