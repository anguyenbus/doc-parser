"""
test_image_handler.py — PNG and JPEG as page-pipeline kinds: the header-only pixel
guard (``guards.check_image``), the image handler (``formats/image.py``), the bytes
sent to an escalation engine (``render.fit_engine_image``) and the pipeline end to end.

Images are built at test time with PIL in ``tmp_path``. The handler and pipeline
tests run Docling on small images; no AWS call is made (``escalate=False``).

Run ONLY these tests:
    uv run pytest tests/test_image_handler.py
"""

from __future__ import annotations

import io
import json
import logging
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from parser_service.formats import handlers as unit_handlers
from parser_service.formats.guards import check_image, image_header
from parser_service.formats.handlers import parse_unit
from parser_service.formats.result import Reply
from parser_service.parsing.render import (
    ENGINE_MAX_IMAGE_BYTES,
    ENGINE_MAX_IMAGE_SIDE,
    fit_engine_image,
)
from parser_service.pipeline import api, config, intake, layout, units
from parser_service.storage.store import LocalStore

FIXTURES = Path(__file__).parent / "fixtures"
_REJECT_BUDGET_S = 0.1


def _text_image(path: Path, size: tuple[int, int] = (1200, 900), **save: object) -> Path:
    """Black lines of text on white, saved in the format of ``path``'s suffix."""
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=max(12, size[1] // 30))
    for line in range(8):
        draw.text(
            (size[0] // 20, size[1] // 20 + line * size[1] // 10),
            f"Invoice line {line}: the quick brown fox jumps over the lazy dog",
            fill="black",
            font=font,
        )
    image.save(path, **save)
    return path


def _png_header_only(path: Path, width: int, height: int) -> Path:
    """A PNG signature and an IHDR declaring ``width`` x ``height``, and no pixel data:
    a few dozen bytes that declare an image of any size."""
    ihdr = struct.pack(">II", width, height) + bytes([8, 2, 0, 0, 0])
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + bytes(4))
    return path


# ---------------------------------------------------------------------------
# The header-only pixel guard
# ---------------------------------------------------------------------------


def test_image_header_matches_pil(tmp_path: Path) -> None:
    """PNG, baseline and progressive JPEG, a JPEG whose frame header sits behind a
    large EXIF segment, and an animated PNG's frame count."""
    exif = Image.Exif()
    exif[0x010E] = "x" * 60_000  # ImageDescription: pushes SOF ~60 KB into the file
    cases = [
        _text_image(tmp_path / "a.png", (640, 480)),
        _text_image(tmp_path / "b.jpg", (1600, 1200), quality=85),
        _text_image(tmp_path / "c.jpg", (1000, 700), progressive=True),
        _text_image(tmp_path / "d.jpg", (300, 200), exif=exif),
    ]
    for path in cases:
        header = image_header(path)
        with Image.open(path) as image:
            assert header is not None and (header.width, header.height) == image.size, path.name
            assert header.frames == 1

    frames = [Image.new("RGB", (100, 50), color) for color in ("white", "red", "blue")]
    frames[0].save(tmp_path / "anim.png", save_all=True, append_images=frames[1:])
    header = image_header(tmp_path / "anim.png")
    assert header is not None and (header.width, header.height, header.frames) == (100, 50, 3)
    assert header.megapixels == pytest.approx(100 * 50 * 3 / 1e6)

    # Not an image, a truncated header, a JPEG with no frame header: unreadable -> None.
    (tmp_path / "x.txt").write_bytes(b"hello")
    (tmp_path / "short.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    (tmp_path / "nosof.jpg").write_bytes(b"\xff\xd8\xff\xda\x00\x02" + bytes(64))
    for name in ("x.txt", "short.png", "nosof.jpg"):
        assert image_header(tmp_path / name) is None
        assert check_image(tmp_path / name, 1) is None  # unreadable passes to the parser


def test_check_image_rejects_over_the_limit_without_decoding(tmp_path: Path) -> None:
    bomb = _png_header_only(tmp_path / "bomb.png", 20_000, 20_000)  # 400 MP in 33 bytes
    started = time.perf_counter()
    rejection = check_image(bomb, 50)
    assert time.perf_counter() - started < _REJECT_BUDGET_S
    assert rejection == (
        "too_complex",
        "20000x20000 pixels, 400.0 megapixels over the limit of 50",
        {"image_max_megapixels": 50},
    )
    assert check_image(_png_header_only(tmp_path / "ok.png", 8000, 6000), 50) is None  # 48 MP

    frames = [Image.new("RGB", (2000, 1000), "white")] * 3  # 2 MP x 3 frames
    frames[0].save(tmp_path / "anim.png", save_all=True, append_images=frames[1:])
    assert check_image(tmp_path / "anim.png", 5) == (
        "too_complex",
        "2000x1000 pixels x 3 frames, 6.0 megapixels over the limit of 5",
        {"image_max_megapixels": 5},
    )

    # The guard alone, in a fresh interpreter: no Docling or torch is imported (it
    # reads the header itself and never opens the image with PIL).
    script = (
        "import sys\n"
        "from parser_service.formats.guards import check_image\n"
        "assert check_image(sys.argv[1], 50)[0] == 'too_complex'\n"
        "loaded = [m for m in ('docling', 'torch') if m in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script, str(bomb)], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr[-2000:]


def test_guard_rejection_is_final_and_never_reaches_the_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Through ``_process_page``: an image over the limit is one final ``failed``
    ``too_complex`` record; a raised limit releases it; a normal image is parsed."""
    handled: list[Path] = []

    def stub_handler(path: Path, page_number: int, escalate: bool) -> Reply:
        handled.append(path)
        return "ok", None, None, "# parsed", {"route": "docling-kept"}

    monkeypatch.setitem(unit_handlers.HANDLERS, "png", stub_handler)
    store = LocalStore(tmp_path / "store")
    caplog.set_level(logging.INFO, logger="parser_service.events")

    bomb = _png_header_only(tmp_path / "bomb.png", 20_000, 20_000)
    work_id = intake.work_id_for(bomb, "png")
    intake._intake(store, work_id, bomb, "png")
    record = units._process_page(store, work_id, 1, False, 3, kind="png")
    assert (record["state"], record["reason_code"], record["attempt"]) == (
        "failed",
        "too_complex",
        1,
    )
    assert record["limits"] == {"image_max_megapixels": 50}
    assert (record["markdown"], record["kind"], handled) == ("", "png", [])
    assert layout._read_attempts(store, work_id, 1) == 1
    events = [
        json.loads(r.getMessage()) for r in caplog.records if r.name == "parser_service.events"
    ]
    assert any(e["event"] == "guard_reject" and e["reason_code"] == "too_complex" for e in events)

    limits_now = config.current_limits("png")
    assert limits_now == {"deadline_s": 600, "max_output_mb": 20, "image_max_megapixels": 50}
    assert config._is_final(record, limits_now)
    monkeypatch.setenv("PARSER_IMAGE_MAX_MEGAPIXELS", "500")
    assert not config._is_final(record, config.current_limits("png"))
    monkeypatch.delenv("PARSER_IMAGE_MAX_MEGAPIXELS")

    normal = _text_image(tmp_path / "normal.png", (400, 300))
    work_id = intake.work_id_for(normal, "png")
    intake._intake(store, work_id, normal, "png")
    record = units._process_page(store, work_id, 1, False, 3, kind="png")
    assert (record["state"], record["markdown"]) == ("ok", "# parsed")
    assert [path.name for path in handled] == ["source.png"]


# ---------------------------------------------------------------------------
# The bytes sent to an escalation engine
# ---------------------------------------------------------------------------


def _encoded(image: Image.Image, fmt: str, **save: object) -> bytes:
    out = io.BytesIO()
    image.save(out, format=fmt, **save)
    return out.getvalue()


def test_fit_engine_image_keeps_a_fitting_image_unchanged() -> None:
    data = (FIXTURES / "screenshot.png").read_bytes()
    assert fit_engine_image(data) is data
    jpeg = _encoded(Image.new("RGB", (800, 600), "white"), "JPEG")
    assert fit_engine_image(jpeg) is jpeg
    assert fit_engine_image(b"not an image") == b"not an image"  # never raises


def test_fit_engine_image_turns_upright_and_flattens() -> None:
    portrait = Image.new("RGB", (300, 100), "white")  # stored on its side
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise to view
    fitted = Image.open(io.BytesIO(fit_engine_image(_encoded(portrait, "JPEG", exif=exif))))
    assert fitted.size == (100, 300) and fitted.getexif().get(0x0112, 1) == 1

    clear = Image.new("RGBA", (50, 50), (0, 0, 0, 0))  # black under full transparency
    webp = _encoded(clear, "WEBP", lossless=True)  # neither PNG nor JPEG: re-encoded
    fitted = Image.open(io.BytesIO(fit_engine_image(webp)))
    assert fitted.format == "PNG" and fitted.mode == "RGB"
    assert fitted.getpixel((25, 25)) == (255, 255, 255)


def test_fit_engine_image_shrinks_to_the_engine_limits() -> None:
    wide = _encoded(Image.new("RGB", (9000, 100), "white"), "PNG")
    fitted = Image.open(io.BytesIO(fit_engine_image(wide)))
    assert max(fitted.size) == ENGINE_MAX_IMAGE_SIDE

    noise = Image.frombytes("RGB", (2000, 2000), os.urandom(2000 * 2000 * 3))
    big = _encoded(noise, "PNG")
    assert len(big) > ENGINE_MAX_IMAGE_BYTES
    small = fit_engine_image(big)
    assert len(small) <= ENGINE_MAX_IMAGE_BYTES
    assert Image.open(io.BytesIO(small)).format == "JPEG"


# ---------------------------------------------------------------------------
# The handler and the pipeline (Docling, no AWS)
# ---------------------------------------------------------------------------


def test_blank_images_are_ok_without_docling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from parser_service.formats import image as image_handler

    def no_parse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a blank image must not be parsed")

    monkeypatch.setattr(image_handler, "parse_to_markdown", no_parse)
    white = tmp_path / "white.jpg"
    Image.new("RGB", (2000, 1500), "white").save(white)
    clear = tmp_path / "clear.png"
    Image.new("RGBA", (800, 600), (0, 0, 0, 0)).save(clear)  # transparent black: blank
    for kind, path in (("jpeg", white), ("png", clear)):
        assert parse_unit(kind, path, 1, True) == (
            "ok",
            None,
            None,
            "",
            {"route": "blank", "route_reason": "blank_image"},
        )


def test_image_handler_parses_text(tmp_path: Path) -> None:
    path = _text_image(tmp_path / "scan.jpg", quality=90)
    state, reason, reason_code, markdown, extra = parse_unit("jpeg", path, 1, False)
    assert state in ("ok", "degraded"), reason  # degraded: the gate wanted the engine
    assert reason_code is None
    assert "quick brown fox" in markdown
    assert extra["route"] == "docling-kept"
    assert [route["page_index"] for route in extra["page_routes"]] == [0]
    assert extra["call_counts"] == {"vlm": 0, "textract": 0}


def test_pipeline_parses_png_and_jpeg_and_refuses_other_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PARSER_ENABLED_KINDS", raising=False)
    store = tmp_path / "store"
    jpeg = _text_image(tmp_path / "photo.jpeg", quality=90)
    png = FIXTURES / "screenshot.png"
    for source, kind, ext in ((jpeg, "jpeg", "jpg"), (png, "png", "png")):
        result = api.parse_pdf_pages(source, store, workers=0, escalate=False)
        assert (result["kind"], result["unit"], result["page_count"]) == (kind, "document", 1)
        assert result["state"] == "complete" or result["pages"][0]["state"] == "degraded"
        manifest = json.loads((Path(result["work_dir"]) / "manifest.json").read_text())
        assert manifest["config"]["kind"] == kind and "escalation_engine" in manifest["config"]
        assert (Path(result["work_dir"]) / f"source.{ext}").exists()

    gif = tmp_path / "anim.gif"
    Image.new("RGB", (100, 100), "white").save(gif)
    result = api.parse_pdf_pages(gif, store, workers=0)
    assert (result["state"], result["detected_type"], result["pages"][0]["reason_code"]) == (
        "failed",
        "image",
        "unsupported_type",
    )

    # The kill switch: with images off, a PNG is refused at intake.
    monkeypatch.setenv("PARSER_ENABLED_KINDS", "pdf,docx")
    result = api.parse_pdf_pages(png, tmp_path / "other", workers=0)
    assert (result["state"], result["detected_type"], result["pages"][0]["reason_code"]) == (
        "failed",
        "png",
        "unsupported_type",
    )
