"""
test_settings.py — the start-up settings check (``parser_service.settings``).

Offline: every case passes its own environment mapping, so nothing here depends on
(or changes) the real environment.

Run ONLY these tests:
    uv run pytest tests/test_settings.py
"""

from __future__ import annotations

import dataclasses
import logging
import subprocess
import sys

import pytest

from parser_service.settings import Settings, SettingsError, load_settings

REGION = "ap-southeast-2"
MODEL = "au.anthropic.claude-sonnet-4-6"


def _problems(**kwargs: object) -> list[str]:
    with pytest.raises(SettingsError) as raised:
        load_settings(**kwargs)  # type: ignore[arg-type]
    return raised.value.problems


def test_offline_case_needs_no_aws_setting() -> None:
    settings = load_settings(store_location="/data/work", escalate=False, environ={})

    assert settings == Settings(
        store_location="/data/work",
        store_kind="local",
        aws_region=None,
        vlm_model=None,
        escalation_engine="vlm",
        page_workers=None,
        page_max_attempts=3,
        scan_fastpath=False,
        arbitration=False,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.aws_region = "x"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"store_location": "s3://bucket/parser/", "escalate": False}, "store location is s3://"),
        ({"store_location": "/w", "input_is_s3": True, "escalate": False}, "input is s3://"),
        ({"store_location": "/w", "escalate": True}, "escalation is on"),
    ],
)
def test_aws_region_is_required_when_something_needs_it(
    kwargs: dict[str, object], reason: str
) -> None:
    env = {"BEDROCK_VLM_MODEL": MODEL}
    problems = _problems(**kwargs, environ=env)
    assert len(problems) == 1 and problems[0].startswith("AWS_REGION is required")
    assert reason in problems[0]
    for blank in ("", "  "):
        assert _problems(**kwargs, environ={**env, "AWS_REGION": blank}) == problems

    settings = load_settings(**kwargs, environ={**env, "AWS_REGION": REGION})  # type: ignore[arg-type]
    assert settings.aws_region == REGION


def test_vlm_model_is_required_only_for_the_vlm_engine() -> None:
    env = {"AWS_REGION": REGION}
    problems = _problems(store_location="/w", escalate=True, environ=env)
    assert problems == ["BEDROCK_VLM_MODEL is required (escalation is on and the engine is vlm)"]

    textract = load_settings(
        store_location="/w", environ={**env, "PARSER_ESCALATION_ENGINE": "textract"}
    )
    assert textract.escalation_engine == "textract" and textract.vlm_model is None
    assert load_settings(store_location="/w", escalate=False, environ=env).vlm_model is None
    with_model = load_settings(store_location="/w", environ={**env, "BEDROCK_VLM_MODEL": MODEL})
    assert with_model.vlm_model == MODEL


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PARSER_ESCALATION_ENGINE", "ocr"),
        ("PARSER_ESCALATION_ENGINE", ""),
        ("PARSER_PAGE_WORKERS", "-1"),
        ("PARSER_PAGE_WORKERS", "four"),
        ("PARSER_PAGE_WORKERS", ""),
        ("PARSER_PAGE_MAX_ATTEMPTS", "0"),
        ("PARSER_PAGE_MAX_ATTEMPTS", "2.5"),
        ("PARSER_SCAN_FASTPATH", "yes"),
        ("PARSER_ESCALATION_ARBITRATION", "on"),
        ("PARSER_STORE_LOCATION", "gs://bucket/prefix"),
        ("PARSER_STORE_LOCATION", "s3://"),
        ("PARSER_DOCX_MAX_RATIO", "0"),
        ("PARSER_DOCX_MAX_PARAGRAPHS", "many"),
        ("PARSER_DOCX_MAX_EXPANDED_MB", "-300"),
        ("PARSER_MAX_DOCX_MB", "0"),
        ("PARSER_DEADLINE_HTML_S", "soon"),
        ("PARSER_DEADLINE_EMAIL_S", "0"),  # shared by eml and msg: reported once
        ("PARSER_MAX_EMAIL_MB", "-1"),
        ("PARSER_IMAGES_MIN_PIXELS", "0"),
    ],
)
def test_each_invalid_value_is_reported_by_name(name: str, value: str) -> None:
    problems = _problems(escalate=False, environ={"PARSER_STORE_LOCATION": "/w", name: value})
    assert len(problems) == 1
    assert ("store location" if name == "PARSER_STORE_LOCATION" else name) in problems[0]


def test_valid_optional_values_are_parsed() -> None:
    settings = load_settings(
        escalate=False,
        environ={
            "PARSER_STORE_LOCATION": "s3://bucket",
            "AWS_REGION": REGION,
            "PARSER_PAGE_WORKERS": "0",
            "PARSER_PAGE_MAX_ATTEMPTS": "5",
            "PARSER_SCAN_FASTPATH": "TRUE",
            "PARSER_ESCALATION_ARBITRATION": "0",
        },
    )
    assert (settings.store_location, settings.store_kind) == ("s3://bucket", "s3")
    assert (settings.page_workers, settings.page_max_attempts) == (0, 5)
    assert (settings.scan_fastpath, settings.arbitration) == (True, False)


def test_several_problems_are_reported_together_in_one_error() -> None:
    with pytest.raises(SettingsError) as raised:
        load_settings(
            input_is_s3=True,
            environ={"PARSER_PAGE_WORKERS": "x", "PARSER_SCAN_FASTPATH": "maybe"},
        )
    problems = raised.value.problems
    assert len(problems) == 5
    message = str(raised.value)
    assert "\n" not in message  # one line
    for expected in (
        "no store location: set PARSER_STORE_LOCATION or pass --work-dir",
        "AWS_REGION is required (the input is s3://, escalation is on)",
        "BEDROCK_VLM_MODEL",
        "PARSER_PAGE_WORKERS",
        "PARSER_SCAN_FASTPATH",
    ):
        assert expected in message


def test_work_dir_overrides_the_environment_and_logs_both_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = {"PARSER_STORE_LOCATION": "s3://bucket/parser/"}
    caplog.set_level(logging.INFO, logger="parser_service.settings")

    settings = load_settings(store_location="/local/work", escalate=False, environ=env)

    assert settings.store_location == "/local/work" and settings.store_kind == "local"
    override = [r.getMessage() for r in caplog.records if "overrides" in r.getMessage()]
    assert override == [
        "store location: --work-dir /local/work overrides PARSER_STORE_LOCATION s3://bucket/parser/"
    ]
    effective = [r.getMessage() for r in caplog.records if r.getMessage().startswith("settings:")]
    assert len(effective) == 1 and "\n" not in effective[0]
    assert "store_location=/local/work" in effective[0] and "page_max_attempts=3" in effective[0]

    caplog.clear()
    load_settings(
        store_location="s3://bucket/parser/", escalate=False, environ={**env, "AWS_REGION": REGION}
    )  # same value: nothing to report
    assert not [r for r in caplog.records if "overrides" in r.getMessage()]


def test_importing_the_module_reads_nothing_from_the_environment() -> None:
    """Import in a fresh interpreter, with every environment read recorded.

    The package ``__init__`` has its own (older) import-time reads, so it is
    imported first; the spy then sees only what ``settings`` and its imports read.
    """
    code = (
        "import os\n"
        "os.environ.update(PARSER_PAGE_WORKERS='x', PARSER_STORE_LOCATION='gs://no')\n"
        "import parser_service\n"
        "class Spy(dict):\n"
        "    names = []\n"
        "    def get(self, *a, **k): Spy.names.append(a[0]); return super().get(*a, **k)\n"
        "    def __getitem__(self, k): Spy.names.append(k); return super().__getitem__(k)\n"
        "os.environ = Spy(os.environ)\n"
        "import parser_service.settings\n"
        "print(Spy.names)\n"
    )
    done = subprocess.run(  # noqa: S603 — this interpreter, a fixed program
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "[]"


def test_executor_defaults_to_local() -> None:
    settings = load_settings(store_location="/w", escalate=False, environ={})
    assert (settings.executor, settings.kserve_url, settings.kserve_concurrency) == (
        "local",
        None,
        None,
    )


def test_kserve_executor_needs_a_url_and_an_s3_store() -> None:
    env = {"AWS_REGION": REGION, "PARSER_EXECUTOR": "kserve"}
    assert _problems(store_location="s3://b/parser/", escalate=False, environ=env) == [
        "PARSER_KSERVE_URL is required (the executor is kserve)"
    ]
    problems = _problems(
        store_location="/w", escalate=False, environ={**env, "PARSER_KSERVE_URL": "http://svc"}
    )
    assert len(problems) == 1 and "needs an s3:// store location" in problems[0]
    assert _problems(
        store_location="s3://b/parser/",
        escalate=False,
        environ={**env, "PARSER_KSERVE_URL": "svc:8080"},
    ) == ["PARSER_KSERVE_URL must be an http:// or https:// URL, got 'svc:8080'"]

    settings = load_settings(
        store_location="s3://b/parser/",
        escalate=False,
        environ={**env, "PARSER_KSERVE_URL": "http://svc", "PARSER_KSERVE_CONCURRENCY": "16"},
    )
    assert (settings.executor, settings.kserve_url, settings.kserve_concurrency) == (
        "kserve",
        "http://svc",
        16,
    )


def test_executor_arguments_override_the_environment() -> None:
    settings = load_settings(
        store_location="s3://b/parser/",
        escalate=False,
        executor="kserve",
        kserve_url="http://other",
        environ={"AWS_REGION": REGION, "PARSER_EXECUTOR": "local", "PARSER_KSERVE_URL": "http://x"},
    )
    assert (settings.executor, settings.kserve_url) == ("kserve", "http://other")


def test_bad_executor_settings_are_reported() -> None:
    env = {"PARSER_EXECUTOR": "ray", "PARSER_KSERVE_CONCURRENCY": "0"}
    problems = _problems(store_location="/w", escalate=False, environ=env)
    assert "PARSER_EXECUTOR must be local or kserve, got 'ray'" in problems
    assert any(p.startswith("PARSER_KSERVE_CONCURRENCY must be an integer") for p in problems)


_DRIVER = {
    "store_location": "s3://b/parser/",
    "escalate": False,
    "executor": "kserve",
    "kserve_url": "http://svc",
}
_SERVICE_ENV = {
    "PARSER_STORE_LOCATION": "s3://b/parser/",
    "BEDROCK_VLM_MODEL": MODEL,
}


@pytest.mark.parametrize("kwargs", [_DRIVER, {"escalate": True}], ids=["driver", "service"])
def test_settings_refuse_request_timeout_below_deadline_plus_margin(
    kwargs: dict[str, object],
) -> None:
    base = {"AWS_REGION": REGION, **_SERVICE_ENV}

    defaults = load_settings(**kwargs, environ=base)  # type: ignore[arg-type]
    assert (
        defaults.deadline_pdf_page_s,
        defaults.remote_request_timeout_s,
        defaults.worker_start_timeout_s,
    ) == (600, 900, 300)

    edge = load_settings(
        **kwargs,  # type: ignore[arg-type]
        environ={**base, "PARSER_DEADLINE_PDF_PAGE_S": "780"},
    )
    assert (edge.deadline_pdf_page_s, edge.remote_request_timeout_s) == (780, 900)

    for env in (
        {"PARSER_DEADLINE_PDF_PAGE_S": "781"},
        {"PARSER_DEADLINE_PDF_PAGE_S": "780", "PARSER_REMOTE_REQUEST_TIMEOUT_S": "899"},
    ):
        problems = _problems(**kwargs, environ={**base, **env})
        assert len(problems) == 1
        assert problems[0].startswith(
            "PARSER_REMOTE_REQUEST_TIMEOUT_S must be at least the largest parse deadline plus 120 s"
        )

    for name in (
        "PARSER_DEADLINE_PDF_PAGE_S",
        "PARSER_REMOTE_REQUEST_TIMEOUT_S",
        "PARSER_WORKER_START_TIMEOUT_S",
    ):
        for raw in ("0", "-5", "1.5", "ten", ""):
            problems = _problems(**kwargs, environ={**base, name: raw})
            assert problems == [f"{name} must be an integer of 1 or more, got {raw!r}"]


def test_enabled_kinds_are_known_and_have_a_handler_in_this_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PARSER_ENABLED_KINDS ships with every handled kind; a kind whose handler has
    not shipped is refused, so it cannot be turned on before its code exists."""
    from parser_service import settings as settings_module
    from parser_service import unit_handlers
    from parser_service.sniff import KINDS

    kwargs: dict[str, object] = {"store_location": "/w", "escalate": False}
    assert load_settings(**kwargs, environ={}).enabled_kinds == KINDS  # type: ignore[arg-type]
    env = {"PARSER_ENABLED_KINDS": " PDF , pdf,"}
    assert load_settings(**kwargs, environ=env).enabled_kinds == ("pdf",)  # type: ignore[arg-type]

    # Since Group 6 every known kind has a handler and a deadline in this image, so
    # every one of them can be enabled ...
    env = {"PARSER_ENABLED_KINDS": ",".join(KINDS)}
    assert load_settings(**kwargs, environ=env).enabled_kinds == KINDS  # type: ignore[arg-type]

    # ... and the refusal is shown on an image built without one: eml's handler and
    # deadline are taken out.
    monkeypatch.delitem(unit_handlers.HANDLERS, "eml")
    monkeypatch.delitem(settings_module.DEADLINE_ENV, "eml")
    assert _problems(**kwargs, environ={"PARSER_ENABLED_KINDS": "pdf,eml"}) == [
        "PARSER_ENABLED_KINDS enables eml, which has no handler in this image"
    ]
    assert _problems(**kwargs, environ={"PARSER_ENABLED_KINDS": "pdf,xlsx"}) == [
        "PARSER_ENABLED_KINDS names unknown kind(s) xlsx; known: pdf, docx, html, txt, eml, msg"
    ]
    assert _problems(**kwargs, environ={"PARSER_ENABLED_KINDS": " , "}) == [
        "PARSER_ENABLED_KINDS must name at least one kind, got ' , '"
    ]

    # Once a group registers the handler and its deadline, the kind can be enabled,
    # and its deadline joins the request-timeout check.
    monkeypatch.setitem(unit_handlers.HANDLERS, "eml", lambda *args: ("ok", None, None, "", {}))
    assert _problems(**kwargs, environ={"PARSER_ENABLED_KINDS": "pdf,eml"}) == [
        "PARSER_ENABLED_KINDS enables eml, which has no parse deadline"
    ]
    monkeypatch.setitem(settings_module.DEADLINE_ENV, "eml", ("PARSER_DEADLINE_EMAIL_S", 120))
    env = {"PARSER_ENABLED_KINDS": "pdf,eml"}
    assert load_settings(**kwargs, environ=env).enabled_kinds == ("pdf", "eml")  # type: ignore[arg-type]
    env = {**env, "PARSER_DEADLINE_PDF_PAGE_S": "10", "PARSER_DEADLINE_EMAIL_S": "781"}
    [problem] = _problems(**kwargs, environ=env)
    assert "PARSER_DEADLINE_EMAIL_S is 781" in problem
    # A disabled kind's deadline is not part of the check.
    env["PARSER_ENABLED_KINDS"] = "pdf"
    assert load_settings(**kwargs, environ=env).enabled_kinds == ("pdf",)  # type: ignore[arg-type]
