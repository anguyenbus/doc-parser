# syntax=docker/dockerfile:1
#
# The page service image: the KServe predictor (default entrypoint) and the
# driver (scripts/parse_pages.py, run with a command override) in one image, so
# both sides of a run share one pipeline version.
#
# - CPU torch: uv.lock resolves torch from PyPI with CUDA wheels (several GB of
#   nvidia-* packages). Here the locked versions are installed from the PyTorch
#   CPU index and the CUDA packages are dropped.
# - Models baked in: the build parses a digital and a scanned page, so every model
#   Docling and its OCR load is in the image; HF_HUB_OFFLINE=1 at runtime makes a
#   missing model fail loudly instead of downloading.
#
# Build (from the repo root):
#   sudo docker build -f deploy/docker/Dockerfile -t doc-parser-page:dev .

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv export --frozen --extra serve --no-emit-project --no-hashes -o /tmp/requirements.txt \
 && grep -v -E '^(nvidia-|triton|cuda-)' /tmp/requirements.txt > /tmp/requirements-cpu.txt \
 && uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install -r /tmp/requirements-cpu.txt \
      --index-url https://download.pytorch.org/whl/cpu \
      --extra-index-url https://pypi.org/simple \
      --index-strategy unsafe-best-match
COPY src ./src
RUN VIRTUAL_ENV=/opt/venv uv pip install --no-deps . \
 && /opt/venv/bin/python -c "import torch; assert not torch.version.cuda, torch.version.cuda"

FROM python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    HF_HOME=/opt/hf \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=2
COPY tests/fixtures/scanned.pdf /tmp/warm/scanned.pdf
# An image page forces a real OCR pass, so RapidOCR downloads its torch models now.
# The runtime user cannot write to site-packages, and a scanned PDF page may skip OCR.
COPY tests/fixtures/screenshot.png /tmp/warm/screenshot.png
RUN python -c "\
from pathlib import Path; \
from parser_service.page_worker import WARMUP_PDF; \
from parser_service.markdown_pipeline import parse_to_markdown; \
[parse_to_markdown(Path(p), escalate=False) for p in (WARMUP_PDF, '/tmp/warm/scanned.pdf', '/tmp/warm/screenshot.png')]" \
 && python -c "\
import pathlib, rapidocr; \
models = pathlib.Path(rapidocr.__file__).parent / 'models'; \
missing = [n for n in ('PP-OCRv6_det_small.pth', 'PP-OCRv6_rec_small.pth', 'ch_ppocr_mobile_v2.0_cls_mobile.pth') if not (models / n).is_file()]; \
assert not missing, f'OCR models not baked into the image: {missing}'" \
 && rm -rf /tmp/warm \
 && chmod -R a+rX /opt/hf /opt/venv
ENV HF_HUB_OFFLINE=1
WORKDIR /app
COPY scripts/parse_pages.py scripts/parse_pages.py
RUN useradd --uid 10001 --no-create-home parser
USER 10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "parser_service.kserve_predictor"]
