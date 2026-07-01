"""test_confidence_page.py — Task Group 2 tests for ``page_confidence``.

Pure, deterministic per-page scorer over a single ``page_routes`` record. No
mocks — hand-built record dicts (task 2.1). Offline.

Run ONLY these tests (task 2.5):
    uv run pytest tests/test_confidence_page.py
"""

from __future__ import annotations

from parser_service.confidence import LOW_CONFIDENCE_THRESHOLD, page_confidence


# ---------------------------------------------------------------------------
# Tier mapping (spec table).
# ---------------------------------------------------------------------------


def test_docling_kept_scores_0_95() -> None:
    assert page_confidence({"route": "docling-kept", "reason": None}) == 0.95


def test_engine_passing_scores_0_85() -> None:
    for route in ("vlm", "textract"):
        assert (
            page_confidence({"route": route, "engine_quality_passes": True}) == 0.85
        )


def test_engine_failing_but_kept_scores_0_60() -> None:
    for route in ("vlm", "textract"):
        assert (
            page_confidence({"route": route, "engine_quality_passes": False}) == 0.60
        )


def test_rejected_kept_docling_scores_0_70() -> None:
    for route in ("vlm-rejected-kept-docling", "textract-rejected-kept-docling"):
        assert page_confidence({"route": route}) == 0.70


def test_fallback_docling_scores_0_50() -> None:
    for route in ("vlm-fallback-docling", "textract-fallback-docling"):
        assert page_confidence({"route": route}) == 0.50


def test_error_or_empty_scores_0_15() -> None:
    # A route_ok route on a page that produced no content (docling_fallback=None
    # fallback and the no_docling_content promote path) → error tier.
    assert page_confidence({"route": "vlm", "reason": "no_docling_content"}) == 0.15
    assert page_confidence({"route": "textract", "reason": "no_docling_content"}) == 0.15


# ---------------------------------------------------------------------------
# Defensive key reads across record shapes.
# ---------------------------------------------------------------------------


def test_arbitration_off_shape_uses_vlm_quality_passes() -> None:
    # Arbitration-off records carry only vlm_quality_passes (no engine_* key).
    assert (
        page_confidence(
            {"route": "vlm", "reason": None, "vlm_quality_passes": True}
        )
        == 0.85
    )
    assert (
        page_confidence(
            {"route": "textract", "reason": None, "vlm_quality_passes": False}
        )
        == 0.60
    )


def test_records_without_booleans_score_at_flat_tier() -> None:
    # docling-kept and *-fallback-docling carry no quality booleans at all.
    assert page_confidence({"route": "docling-kept"}) == 0.95
    assert page_confidence({"route": "vlm-fallback-docling"}) == 0.50
    # An engine route (vlm/textract) with NO quality boolean at all is only ever
    # the error/empty _fallback record (docling_fallback is None) → error tier.
    assert page_confidence({"route": "vlm", "reason": None, "n_chars": 0}) == 0.15


# ---------------------------------------------------------------------------
# Determinism + tier ordering.
# ---------------------------------------------------------------------------


def test_determinism() -> None:
    rec = {"route": "vlm", "engine_quality_passes": True, "reason": "x"}
    assert page_confidence(rec) == page_confidence(rec) == page_confidence(dict(rec))


def test_tier_ordering_holds_by_construction() -> None:
    s_docling = page_confidence({"route": "docling-kept"})
    s_rejected = page_confidence({"route": "vlm-rejected-kept-docling"})
    s_engine_pass = page_confidence({"route": "vlm", "engine_quality_passes": True})
    s_engine_fail = page_confidence({"route": "vlm", "engine_quality_passes": False})
    s_fallback = page_confidence({"route": "vlm-fallback-docling"})
    s_error = page_confidence({"route": "vlm", "reason": "no_docling_content"})

    # spec.md's load-bearing monotone chain (audit line 41): the tier bands never
    # invert. (The tasks.md 2.1 prose interleaves engine-passing between
    # rejected-kept and fallback, but the fixed tier numbers put engine-passing
    # 0.85 ABOVE rejected-kept 0.70 — the numbers are authoritative, so we assert
    # the spec.md chain plus the engine sub-chain, the pairs that hold by
    # construction.)
    assert s_docling > s_rejected > s_fallback > s_error
    assert s_engine_pass >= s_engine_fail > s_fallback > s_error
    assert s_docling > s_engine_pass


def test_review_threshold_separates_trusted_from_untrusted_tiers() -> None:
    # The advisory threshold must sit in the gap between the highest UNTRUSTED tier
    # (engine-failing-kept 0.60) and the lowest TRUSTED tier (rejected-kept-docling
    # 0.70) — never coinciding with a tier value. Untrusted output (the pipeline
    # does not trust what it shipped) is flagged; trusted output is not.
    untrusted = [
        {"route": "vlm", "reason": "no_docling_content"},          # error 0.15
        {"route": "vlm-fallback-docling"},                          # fallback 0.50
        {"route": "vlm", "engine_quality_passes": False},           # engine-failing 0.60
    ]
    trusted = [
        {"route": "vlm-rejected-kept-docling"},                     # 0.70
        {"route": "vlm", "engine_quality_passes": True},            # 0.85
        {"route": "docling-kept"},                                  # 0.95
    ]
    for rec in untrusted:
        assert page_confidence(rec) < LOW_CONFIDENCE_THRESHOLD, rec
    for rec in trusted:
        assert page_confidence(rec) >= LOW_CONFIDENCE_THRESHOLD, rec


def test_scores_within_unit_interval() -> None:
    for rec in (
        {"route": "docling-kept"},
        {"route": "vlm", "engine_quality_passes": True},
        {"route": "vlm", "engine_quality_passes": False},
        {"route": "vlm-rejected-kept-docling"},
        {"route": "vlm-fallback-docling"},
        {"route": "vlm", "reason": "no_docling_content"},
        {"route": "totally-unknown-route"},
    ):
        s = page_confidence(rec)
        assert 0.0 <= s <= 1.0
