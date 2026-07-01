"""
markdown_pipeline.py — the markdown-first parsing path (roadmap item 26).

``parse_to_markdown(file_path)`` produces one clean, RAG-ready Markdown string
per document via a per-page-gated pipeline, plus routing telemetry. It mirrors
``parser_service.parse()``'s never-raises contract: all failures are captured in
``warnings[]`` and reflected in ``page_routes`` — it does NOT raise.

Architecture (Route B, adopted 2026-05-31 after benchmark evidence reversed the
Task-Group-1 spike decision — see planning/spike-route-decision.md):
  - Run Docling ONCE over the document.
  - Group Docling items into eval-harness elements by 0-based page index (reusing
    ``_docling_item_to_element``), then render EACH page's element list to markdown
    via ``markdown.render_markdown`` — the parser-owned renderer, which scores
    ~0.05 NID HIGHER than Docling's native ``export_to_markdown`` dialect against
    the verbatim-text gold on both DP-Bench and OmniDocBench. There is no
    whole-doc ``export_to_markdown(page_break_placeholder=...)`` + split anymore.
  - For each page run the existing two-layer ``quality_gate.evaluate_page``:
      keep            → use that page's rendered Docling markdown.
      promote_to_vlm  → render the page image, call the VLM, render the returned
                        element-JSON to markdown, and overwrite the page. On VLM
                        garbage, fall back to the rendered Docling markdown.
  - Concatenate page markdown in page order with plain ``\\n\\n`` (no inline page
    markers — provenance lives in ``page_routes``).

Format routing (reuses ``parser_service._classify``):
  - PDF                → per-page gated path described above.
  - DOCX/XLSX/HTML     → whole-doc ``export_to_markdown()``, gate skipped, one
                         logical page (see ``_whole_doc_to_markdown`` note).
  - Image (PNG/JPEG/…) → gated one-page path (Docling OCR → gate → VLM only on
                         failure).
  - Unknown            → empty markdown + ``unsupported_type`` warning.

``wrap_md_as_prediction(md, source)`` is the ONLY place JSON is produced on the
markdown path: it wraps a markdown string in a single ``paragraph`` element inside
a schema-``1.0.0``-valid prediction so the doc-bench wheel can grade it.

This module reuses (does NOT reimplement): ``quality_gate.evaluate_page`` /
``quality_gate._measure_text_quality``, ``render.render_page`` /
``render.text_layer_tokens``, ``vlm_client.call_vlm`` (+ call-count helpers), and
``markdown.render_markdown``. ``parse()`` and ``render_markdown()`` are untouched
and stay importable — and ``render_markdown`` + the element layer are now
load-bearing (they ARE the better renderer), so they must not be modified.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from parser_service.confidence import (
    LOW_CONFIDENCE_THRESHOLD,
    document_confidence,
    page_confidence,
)
from parser_service.markdown import render_markdown
from parser_service.parser_service import (
    PARSER_VERSION,
    SCHEMA_VERSION,
    _append_warning,
    _classify,
    _docling_item_to_element,
    _emit_vlm_elements,
    _empty_output,
    _input_size_error,
)
from parser_service.quality_gate import _measure_text_quality, evaluate_page
from parser_service.render import render_page, text_layer_tokens
from parser_service.textract_client import (
    analyze_page,
    reset_textract_call_count,
)
from parser_service.vlm_client import (
    call_vlm,
    reset_vlm_call_count,
)

logger = logging.getLogger(__name__)

# ``route`` vocabulary for ``page_routes`` entries:
_ROUTE_DOCLING_KEPT = "docling-kept"  # gate kept Docling's page markdown
_ROUTE_VLM = "vlm"  # VLM markdown replaced the page
_ROUTE_VLM_FALLBACK = "vlm-fallback-docling"  # VLM promoted but garbage → Docling
# Textract escalation engine (selected via PARSER_ESCALATION_ENGINE=textract):
_ROUTE_TEXTRACT = "textract"  # Textract markdown replaced the page
_ROUTE_TEXTRACT_FALLBACK = "textract-fallback-docling"  # Textract promoted but garbage → Docling
# Arbitration (PARSER_ESCALATION_ARBITRATION, default off): the engine produced a
# successful, non-empty rendering, but it was detectably low-quality while the
# Docling rendering of the same page was clean, so Docling shipped instead.
_ROUTE_VLM_REJECTED = "vlm-rejected-kept-docling"  # VLM output rejected → Docling
_ROUTE_TEXTRACT_REJECTED = "textract-rejected-kept-docling"  # Textract output rejected → Docling


def _document_converter() -> Any:
    """Return a fresh Docling ``DocumentConverter``.

    Wrapped in a helper so tests can monkeypatch the converter (to force a
    conversion failure) without importing Docling at module load.
    """
    from docling.document_converter import DocumentConverter

    return DocumentConverter()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_to_markdown(file_path: Path) -> dict[str, Any]:
    """Parse a document into a single RAG-ready markdown string + telemetry.

    Never raises — all failures are captured in ``warnings[]`` and reflected in
    ``page_routes`` (mirroring ``parser_service.parse()``'s contract).

    Args:
        file_path: Path to the document to parse.

    Returns:
        ``{"markdown": str, "page_routes": [{page_index, route, reason}, ...],
           "warnings": [...]}``.
    """
    file_path = Path(file_path).resolve()
    reset_vlm_call_count()
    reset_textract_call_count()

    mime = mimetypes.guess_type(str(file_path))[0] or ""
    kind = _classify(file_path, mime)

    # We return only markdown + page_routes + warnings — no source/sha256 — so we
    # do NOT build the _empty_output skeleton here (that would read the whole file
    # to compute a hash we discard). Just an empty, validated warnings list.
    warnings: list[dict[str, Any]] = []
    page_routes: list[dict[str, Any]] = []

    container: dict[str, Any] = {"warnings": warnings, "elements": []}

    markdown = ""
    try:
        size_error = _input_size_error(file_path)
        if size_error is not None:
            # Reject oversized input before any Docling/render work (stat-based;
            # no file read on this path at all).
            _append_warning(
                container, code="input_too_large", message=size_error, scope="document"
            )
        elif kind == "unknown":
            _append_warning(
                container,
                code="unsupported_type",
                message=f"Unsupported file type: extension={file_path.suffix!r}, mime={mime!r}",
                scope="document",
            )
        elif kind == "pdf":
            markdown = _pdf_to_markdown(file_path, container, page_routes)
        elif kind == "image":
            markdown = _image_to_markdown(file_path, container, page_routes)
        elif kind in ("docx", "xlsx", "html"):
            markdown = _whole_doc_to_markdown(file_path, container, page_routes)
    except Exception as exc:  # noqa: BLE001 — never-raises contract
        logger.exception("Unexpected failure in parse_to_markdown for %s", file_path)
        _append_warning(
            container,
            code="unhandled_exception",
            message=str(exc),
            scope="document",
        )

    # Advisory confidence (read-only over the fully-populated page_routes). This
    # NEVER gates anything — it does not touch markdown, page_routes, or any
    # promote/keep decision. It is a transparent routing-derived heuristic, not a
    # gold-calibrated probability (see parser_service.confidence).
    page_confidences = [
        {"page_index": r["page_index"], "confidence": page_confidence(r)}
        for r in page_routes
    ]
    confidence = {
        "document": document_confidence(page_routes),
        "pages": page_confidences,
    }
    # Surface below-threshold pages as additive, advisory-only warnings.
    for pconf in page_confidences:
        if pconf["confidence"] < LOW_CONFIDENCE_THRESHOLD:
            _append_warning(
                container,
                code="low_confidence_page",
                message=(
                    f"advisory: page {pconf['page_index']} scored "
                    f"{pconf['confidence']:.2f}, below the review threshold "
                    f"{LOW_CONFIDENCE_THRESHOLD}"
                ),
                scope="page",
                page_index=pconf["page_index"],
            )

    return {
        "markdown": markdown,
        "page_routes": page_routes,
        "warnings": warnings,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# Testing bridge — the ONLY place JSON is produced on the markdown path
# ---------------------------------------------------------------------------


def wrap_md_as_prediction(md: str, source: Path | str | dict[str, Any]) -> dict[str, Any]:
    """Wrap a markdown string in a schema-``1.0.0``-valid 1-element prediction.

    This is the ONLY place JSON is produced on the markdown path. The doc-bench
    wheel grades markdown by wrapping it as a single ``paragraph`` element — proven
    to grade identically to full element JSON — so this bridges ``parse_to_markdown``
    output to the grader without re-deriving structure.

    Args:
        md: The full document markdown (becomes the single paragraph's ``text``).
        source: Either a file path (``Path``/``str``) — in which case the full
            four-field ``source`` is built EXACTLY as ``parser_service._empty_output``
            does (sha256 + mime + doc_id + filename) — or a prebuilt full ``source``
            dict (passed through unchanged). A path is the common eval-path contract;
            the dict form lets a caller that already built the source reuse it.

    Returns:
        A dict conforming to ``parser_output.schema.json`` v1.0.0 (NO version bump):
        ``schema_version`` / ``parser_version`` / ``parsed_at`` / ``source`` / ``pages``
        / ``elements`` / ``warnings``, with ``additionalProperties: false`` satisfied
        and exactly one ``paragraph`` element carrying the entire markdown.

    The schema requires ``source`` with all four fields, ``additionalProperties:
    false``, and a 64-hex ``sha256`` — so when given a path the full source is built
    via the same code as ``_empty_output`` (reused below) to keep a partial source
    from slipping through.
    """
    if isinstance(source, dict):
        # Caller already built a full source dict — pass it through, reusing
        # _empty_output's top-level shape (schema_version / parser_version /
        # parsed_at / pages / elements / warnings) only.
        skeleton: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "parsed_at": datetime.now(tz=UTC).isoformat(),
            "source": source,
            "pages": [],
            "elements": [],
            "warnings": [],
        }
    else:
        path = Path(source).resolve()
        mime = mimetypes.guess_type(str(path))[0] or ""
        # Reuse _empty_output's source + skeleton construction verbatim so the
        # source (sha256/mime/doc_id/filename) cannot drift from parse().
        skeleton = _empty_output(path, mime)

    skeleton["elements"] = [
        {
            "element_id": "md_0001",
            "type": "paragraph",
            "page_index": 0,
            "char_span": [0, len(md)],
            "text": md,
            "content": {"kind": "text"},
        }
    ]
    return skeleton


# ---------------------------------------------------------------------------
# PDF per-page routing (Route B: per-page render_markdown over Docling elements)
# ---------------------------------------------------------------------------


def _pdf_to_markdown(
    path: Path, container: dict[str, Any], page_routes: list[dict[str, Any]]
) -> str:
    """Per-page gated markdown for a PDF (Route B).

    Runs Docling once, groups its items into elements per 0-based page index,
    renders each page's elements to markdown via ``render_markdown``, gates each
    page, and replaces gate-promoted pages with VLM markdown.
    """
    converter = _document_converter()
    try:
        result = converter.convert(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Docling conversion failed for %s: %s", path.name, exc)
        _append_warning(container, "docling_failed", str(exc), scope="document")
        return ""

    doc = result.document

    # 0-based page indices in page order (Docling keys ``doc.pages`` 1-based).
    page_indices = sorted(k - 1 for k in getattr(doc, "pages", {}).keys())
    if not page_indices:
        # No page metadata: nothing to render; treat as empty document.
        return ""

    # Route B: group Docling items by 0-based page index, then render each page's
    # element list with our own ``render_markdown`` (the better renderer). There
    # is no whole-doc export + placeholder split — a page with no items simply has
    # no rendered markdown and is routed straight to the VLM (no Docling fallback),
    # so per-page alignment is exact by construction (no slice-shift hazard).
    page_elements = _elements_by_page(doc, container)

    try:
        raw_tokens = text_layer_tokens(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("text_layer_tokens failed for %s: %s", path.name, exc)
        raw_tokens = {}

    pages_md: list[str] = []
    for page_idx in page_indices:
        page_elems = page_elements.get(page_idx, [])
        docling_md = _render_page_markdown(page_elems)

        if not docling_md:
            # No Docling content for this page (scanned / image-only / dropped).
            # The gate would promote a zero-text page anyway; go straight to VLM
            # with no Docling fallback available.
            page_md = _vlm_page_markdown(
                path,
                page_idx,
                container,
                page_routes,
                docling_fallback=None,
                reason="no_docling_content",
                layer=None,
            )
            pages_md.append(page_md)
            continue

        decision = evaluate_page(
            page_idx,
            result,
            page_elems,
            page_text_layer_tokens=raw_tokens.get(page_idx),
        )

        if decision.action == "keep":
            page_routes.append(
                {
                    "page_index": page_idx,
                    "route": _ROUTE_DOCLING_KEPT,
                    "reason": None,
                    "n_chars": len(docling_md),
                }
            )
            pages_md.append(docling_md)
            continue

        # promote_to_vlm
        page_md = _vlm_page_markdown(
            path,
            page_idx,
            container,
            page_routes,
            docling_fallback=docling_md,
            reason=decision.reason,
            layer=decision.layer,
        )
        pages_md.append(page_md)

    return _join_pages(pages_md)


def _render_page_markdown(page_elems: list[dict[str, Any]]) -> str:
    """Render one page's Docling elements to markdown via ``render_markdown``.

    Returns the stripped markdown, or ``""`` when the page has no rendered
    content (the caller treats an empty result as "no Docling content").
    """
    if not page_elems:
        return ""
    return render_markdown({"elements": page_elems}).strip()


def _elements_by_page(doc: Any, container: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    """Group Docling items into eval-harness element dicts keyed by 0-based page.

    Reuses ``_docling_item_to_element`` so the gate AND ``render_markdown`` see
    exactly the same element shapes as ``parse()``. Items are appended in Docling
    iteration order, so each page's element list preserves reading order.
    char_span values are irrelevant here; a throwaway running offset is used.
    """
    from docling_core.types.doc.document import ContentLayer as _CL

    by_page: dict[int, list[dict[str, Any]]] = {}
    char_offset = 0
    layers = {_CL.BODY, _CL.FURNITURE}
    for item, _level in doc.iterate_items(traverse_pictures=True, included_content_layers=layers):
        elem, char_offset = _docling_item_to_element(item, char_offset, container)
        if elem is None:
            continue
        by_page.setdefault(elem["page_index"], []).append(elem)
    return by_page


# ---------------------------------------------------------------------------
# VLM page path (shared by promoted PDF pages, image-only pages, and images)
# ---------------------------------------------------------------------------


def _vlm_page_markdown(
    path: Path,
    page_idx: int,
    container: dict[str, Any],
    page_routes: list[dict[str, Any]],
    docling_fallback: str | None,
    reason: str | None,
    layer: int | None,
    image_bytes: bytes | None = None,
) -> str:
    """Render a page via the selected escalation engine, falling back to
    ``docling_fallback`` on garbage.

    The escalation engine is chosen by ``PARSER_ESCALATION_ENGINE`` (``vlm`` default
    | ``textract``), read here — this is the single escalation seam (``_image_to_markdown``
    inherits it for free). ``vlm`` calls ``vlm_client.call_vlm``; ``textract`` calls
    ``textract_client.analyze_page``. Both return the SAME element-JSON shape, so the
    ``_emit_vlm_elements`` → ``render_markdown`` conversion below is reused unchanged.

    Records the route in ``page_routes`` (``vlm`` / ``textract`` on success, or the
    matching ``*-fallback-docling`` when the engine produced nothing usable). The four
    fallback triggers are identical for both engines: ``{"error": ...}``, non-list
    ``elements``, empty ``elements``, or whitespace-only rendered markdown. Runs
    ``_measure_text_quality`` on the engine markdown and records it as a SIGNAL ONLY in
    the route entry — the route is NEVER gated on it (re-rejecting would just return
    the worse output).
    """
    engine = os.environ.get("PARSER_ESCALATION_ENGINE", "vlm")
    is_textract = engine == "textract"
    engine_label = "Textract" if is_textract else "VLM"
    route_ok = _ROUTE_TEXTRACT if is_textract else _ROUTE_VLM
    route_fallback = _ROUTE_TEXTRACT_FALLBACK if is_textract else _ROUTE_VLM_FALLBACK
    route_rejected = _ROUTE_TEXTRACT_REJECTED if is_textract else _ROUTE_VLM_REJECTED
    # Post-escalation arbitration (default OFF): keep-the-better-of Docling vs
    # engine. When OFF the block below is skipped entirely and the signal-only
    # record path is byte-identical to today.
    arbitration_on = os.environ.get("PARSER_ESCALATION_ARBITRATION", "").lower() in (
        "1",
        "true",
    )

    def _fallback(detail: str) -> str:
        route = route_fallback if docling_fallback is not None else route_ok
        _append_warning(
            container,
            "vlm_fallback_docling" if docling_fallback is not None else "page_unparseable",
            f"{engine_label} {detail} on page {page_idx}"
            + ("; kept Docling output" if docling_fallback is not None else ""),
            scope="page",
            page_index=page_idx,
        )
        page_routes.append(
            {
                "page_index": page_idx,
                "route": route,
                "reason": reason,
                "n_chars": len(docling_fallback or ""),
            }
        )
        return docling_fallback or ""

    # Obtain the page image (rendered from the PDF unless raw bytes were given).
    if image_bytes is None:
        try:
            image_bytes = render_page(path, page_idx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("render_page failed for page %d: %s", page_idx, exc)
            return _fallback(f"render failed ({exc})")

    if is_textract:
        engine_result = analyze_page(image_bytes)
    else:
        engine_result = call_vlm(image_bytes, mode="page")

    if not isinstance(engine_result, dict) or "error" in engine_result:
        detail = (
            engine_result.get("error", "returned a non-dict")
            if isinstance(engine_result, dict)
            else "returned a non-dict"
        )
        return _fallback(f"error: {detail}")

    elements_raw = engine_result.get("elements")
    if not isinstance(elements_raw, list):
        return _fallback("returned non-list 'elements'")
    if not elements_raw:
        return _fallback("returned empty 'elements'")

    # Convert the VLM element-JSON to markdown via the existing renderer. We reuse
    # _emit_vlm_elements so list-splitting / table-text mirroring match parse().
    buf: dict[str, Any] = {"elements": []}
    local_offset = 0
    for i, raw_elem in enumerate(elements_raw):
        if not isinstance(raw_elem, dict):
            continue
        local_offset = _emit_vlm_elements(buf, raw_elem, page_idx, i, local_offset)

    engine_md = render_markdown({"elements": buf["elements"]}).strip()
    if not engine_md:
        return _fallback("rendered whitespace-only markdown")

    # Quality signal on the engine markdown (already computed regardless of path).
    signals = _measure_text_quality(engine_md)

    if not arbitration_on:
        # Flag OFF: signal-only record, byte-identical to today. The route is
        # NEVER gated on the signal here (re-rejecting would return the worse
        # output); arbitration is the only path that may keep Docling on quality.
        page_routes.append(
            {
                "page_index": page_idx,
                "route": route_ok,
                "reason": reason,
                "vlm_quality_passes": signals.passes,
                "vlm_quality_failing_signals": signals.failing_signals,
                "n_chars": len(engine_md),
            }
        )
        return engine_md

    # Arbitration ON: choose, post-escalation, between the two already-produced
    # renderings (engine_md and docling_fallback). Revert to Docling only when
    # ALL hold:
    #   (1) the engine output is detectably low-quality (not signals.passes);
    #   (2) a real Docling rendering exists AND its text is clean; and
    #   (3) the promotion was NOT a coverage promotion — reverting a coverage
    #       promotion would re-introduce the missing content escalation recovered.
    #       The discriminator is the reason prefix, NOT layer (coverage and
    #       Layer-2 garble both carry layer=2).
    # Measure the Docling markdown here rather than reusing a gate signal: the gate
    # only runs its text-quality check for Layer-2 promotions (Layer-1 and coverage
    # promotions return before `_layer2_decision`), so most promoted pages were never
    # measured; and even for a Layer-2 promotion the gate scored the joined element
    # TEXT, not the rendered markdown that actually ships. This scores the shipped
    # string directly, once, only on the arbitration-on revert-eligible path.
    docling_signals = (
        _measure_text_quality(docling_fallback) if docling_fallback is not None else None
    )
    is_coverage_promotion = reason is not None and reason.startswith("low_coverage:")
    revert_to_docling = (
        not signals.passes
        and docling_signals is not None
        and docling_signals.passes
        and not is_coverage_promotion
    )

    record: dict[str, Any] = {
        "page_index": page_idx,
        "reason": reason,
        # Engine signals — recorded under both the descriptive keys and the
        # back-compat vlm_quality_* keys the Studio inspector already reads.
        "engine_quality_passes": signals.passes,
        "engine_quality_failing_signals": signals.failing_signals,
        "vlm_quality_passes": signals.passes,
        "vlm_quality_failing_signals": signals.failing_signals,
        # Docling signals populated only when a Docling rendering exists.
        "docling_quality_passes": (
            docling_signals.passes if docling_signals is not None else None
        ),
        "docling_quality_failing_signals": (
            docling_signals.failing_signals if docling_signals is not None else None
        ),
    }

    if revert_to_docling:
        _append_warning(
            container,
            "vlm_rejected_kept_docling",
            f"{engine_label} output rejected on page {page_idx}; kept Docling "
            f"(engine failing signals: {signals.failing_signals})",
            scope="page",
            page_index=page_idx,
        )
        record["route"] = route_rejected
        record["arbitration"] = "kept-docling"
        record["n_chars"] = len(docling_fallback or "")
        page_routes.append(record)
        return docling_fallback or ""

    record["route"] = route_ok
    record["arbitration"] = "kept-engine"
    record["n_chars"] = len(engine_md)
    page_routes.append(record)
    return engine_md


# ---------------------------------------------------------------------------
# Image routing (gated one-page path — NOT always-VLM)
# ---------------------------------------------------------------------------


def _image_to_markdown(
    path: Path, container: dict[str, Any], page_routes: list[dict[str, Any]]
) -> str:
    """Image (PNG/JPEG/TIFF) → gated one-page path (Route B).

    Docling OCR → ``render_markdown`` over page 0's elements → ``evaluate_page`` →
    VLM only when the gate fires (or Docling extracted nothing). This is the gated
    default, NOT always-VLM.

    NOTE (README discrepancy, deferred / out of scope): the README states images
    route VLM-only, but the gated Docling-first behavior is what ships here (it
    matches ``parser_service._parse_image``). Switching images to always-VLM is a
    conscious cost call gated on a known-hard image corpus (roadmap item 27).
    """
    try:
        converter = _document_converter()
        result = converter.convert(str(path))
        doc = result.document
    except Exception as exc:  # noqa: BLE001
        logger.warning("Docling conversion failed for image %s: %s", path.name, exc)
        _append_warning(container, "docling_failed", str(exc), scope="document")
        return ""

    page_elements = _elements_by_page(doc, container)
    page_elems = page_elements.get(0, [])
    docling_md = _render_page_markdown(page_elems)
    decision = evaluate_page(0, result, page_elems)

    if decision.action == "keep" and page_elems:
        page_routes.append(
            {
                "page_index": 0,
                "route": _ROUTE_DOCLING_KEPT,
                "reason": None,
                "n_chars": len(docling_md),
            }
        )
        return docling_md

    # Gate fired (or Docling extracted nothing) → VLM on the raw image bytes.
    docling_fallback = docling_md if page_elems else None
    return _vlm_page_markdown(
        path,
        0,
        container,
        page_routes,
        docling_fallback=docling_fallback,
        reason=decision.reason,
        layer=decision.layer,
        image_bytes=path.read_bytes(),
    )


# ---------------------------------------------------------------------------
# DOCX / XLSX / HTML routing (whole-doc, gate skipped, one logical page)
# ---------------------------------------------------------------------------


def _whole_doc_to_markdown(
    path: Path, container: dict[str, Any], page_routes: list[dict[str, Any]]
) -> str:
    """DOCX/XLSX/HTML → whole-doc ``export_to_markdown()``, gate skipped.

    Structured formats Docling reads reliably; treated as one logical page.

    NOTE (Route B exception): unlike the PDF/image paths, these two formats keep
    Docling's whole-document ``export_to_markdown`` rather than ``render_markdown``
    over per-page elements. Office/HTML documents have no meaningful page geometry
    (Docling's per-item page numbers are not reliable page boundaries for flowing
    DOCX/HTML), so there is no per-page element grouping to drive ``render_markdown``
    the way there is for PDFs. Keeping ``export_to_markdown`` here preserves the
    exact pre-existing behavior for these formats; the renderer choice that the
    benchmarks measured was on the PDF path.
    """
    try:
        converter = _document_converter()
        result = converter.convert(str(path))
        doc = result.document
    except Exception as exc:  # noqa: BLE001
        logger.warning("Docling conversion failed for %s: %s", path.name, exc)
        _append_warning(container, "docling_failed", str(exc), scope="document")
        return ""

    markdown: str = doc.export_to_markdown().strip()
    page_routes.append(
        {
            "page_index": 0,
            "route": _ROUTE_DOCLING_KEPT,
            "reason": None,
            "n_chars": len(markdown),
        }
    )
    return markdown


# ---------------------------------------------------------------------------
# Page join
# ---------------------------------------------------------------------------


def _join_pages(pages_md: list[str]) -> str:
    """Concatenate per-page markdown in page order with plain ``\\n\\n``.

    No inline page markers or page-number headers — they inject tokens absent
    from marker-free gold (penalizing NID/BLEU). Page provenance lives in
    ``page_routes``. Empty pages are dropped from the join.
    """
    return "\n\n".join(p for p in pages_md if p and p.strip())
