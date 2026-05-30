"""
quality_gate.py

Two-layer triage deciding whether a page's Docling output is good enough to
keep, or should be re-processed by the VLM.

  Layer 1 — Docling's own ConfidenceReport (free; already computed during parse).
             Gates on low_grade / mean_grade being POOR or FAIR.

  Layer 2 — Heuristic safety net on extracted text.
             Catches confidently-wrong text (e.g. OCR reading "1ooo" as valid).
             Only runs when Layer 1 passes (short-circuit).

Tables are never gated here — callers must always send table crops to VLM.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds (conservative; Layer 1 already filters the clear failures)
# ---------------------------------------------------------------------------

_GARBLED_TOKEN_RATIO_MAX = 0.20   # fraction of tokens that look garbled
_MEAN_WORD_LENGTH_MIN    = 2.0    # below this → likely noise
_DICT_HIT_RATE_MIN       = 0.50   # fraction of tokens that are content (alpha or clean numbers)
_MAX_REPEATED_CHAR_RUN   = 6      # e.g. "aaaaaaa" → noise
_ASCII_PRINTABLE_MIN     = 0.90   # fraction of printable ASCII chars

# Coverage: catch silent under-extraction (Docling grades the fragment it DID
# read as high-confidence, blind to what it missed — e.g. tables of contents).
_COVERAGE_MIN_TEXT_LAYER_TOKENS = 30    # only check pages with a substantial text layer
_COVERAGE_RATIO_MIN             = 0.30  # extracted must be ≥30% of the text-layer tokens

# Regex: a token looks garbled when it mixes letters and digits in a short span
# e.g. "1ooo", "M0del", "Rece1ved"
_GARBLED_RE = re.compile(r"(?<=[A-Za-z])\d|(?<=\d)[A-Za-z]")
_REPEATED_RE = re.compile(r"(.)\1{%d,}" % _MAX_REPEATED_CHAR_RUN)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class QualitySignals:
    garbled_token_ratio: float = 0.0
    mean_word_length: float = 5.0
    dict_hit_rate: float = 1.0
    has_repeated_char_run: bool = False
    ascii_printable_ratio: float = 1.0
    failing_signals: list[str] = field(default_factory=list)

    @property
    def passes(self) -> bool:
        return len(self.failing_signals) == 0


@dataclass
class Decision:
    action: str          # "keep" | "promote_to_vlm"
    reason: str | None   # human-readable explanation
    layer: int | None    # 1, 2, or None for "keep"


# ---------------------------------------------------------------------------
# Layer 1 — Docling confidence grades
# ---------------------------------------------------------------------------

def _layer1_decision(conversion_result: object, page_no: int) -> Decision | None:
    """
    Returns a promote Decision if Docling's own confidence is POOR/FAIR,
    or None if the grade is acceptable (caller should proceed to Layer 2).
    """
    try:
        confidence = getattr(conversion_result, "confidence", None)
        if confidence is None:
            return None
        pages_conf = getattr(confidence, "pages", None)
        if not pages_conf:
            return None

        # Docling pages_conf may be a dict keyed by 1-based page number
        page_conf = pages_conf.get(page_no) or pages_conf.get(page_no + 1)
        if page_conf is None:
            return None

        low_grade = getattr(page_conf, "low_grade", None)
        mean_grade = getattr(page_conf, "mean_grade", None)

        # Compare by grade *value* ("poor"/"fair"), robust to Docling moving the
        # QualityGrade enum between modules. QualityGrade is a str-Enum, so .value
        # is the lowercase grade name; fall back to the trailing token of str().
        # (The old code imported QualityGrade from a path that no longer exists,
        # then string-compared "qualitygrade.fair" != "fair" — so Layer 1 never
        # fired. This restores it.)
        def _grade(g: object) -> str:
            v = getattr(g, "value", None)
            return (v if isinstance(v, str) else str(g).rsplit(".", 1)[-1]).lower()

        promote_grades = {"poor", "fair"}
        if low_grade is not None and _grade(low_grade) in promote_grades:
            return Decision("promote_to_vlm", f"docling_low_grade={low_grade}", layer=1)
        if mean_grade is not None and _grade(mean_grade) in promote_grades:
            return Decision("promote_to_vlm", f"docling_mean_grade={mean_grade}", layer=1)

    except Exception as exc:
        logger.debug("Layer 1 confidence check failed (non-fatal): %s", exc)

    return None


# ---------------------------------------------------------------------------
# Layer 2 — Heuristic text quality
# ---------------------------------------------------------------------------

def _is_content_token(token: str) -> bool:
    """True if a token is legitimate content: an alphabetic word OR a cleanly
    formatted number (thousands separators, dates, %, currency, ranges).

    Number-dense pages (charts, financial tables) are valid content, not OCR
    garble. Counting only alphabetic tokens made such pages look low-quality and
    falsely escalated them to the VLM — where, on verbatim-OCR benchmarks,
    Docling actually scores better. Genuine garble is still caught by the
    garbled-token, repeated-char, word-length, and printable-ratio signals.
    """
    if token.isalpha():
        return True
    stripped = token.strip("()[].,%$:/+-")
    digits = stripped.replace(",", "").replace(".", "").replace("/", "")
    return len(digits) > 0 and digits.isdigit()


def _measure_text_quality(text: str) -> QualitySignals:
    if not text or not text.strip():
        return QualitySignals(
            mean_word_length=0.0,
            dict_hit_rate=0.0,
            ascii_printable_ratio=0.0,
            failing_signals=["empty_text"],
        )

    tokens = text.split()
    total = len(tokens)
    if total == 0:
        return QualitySignals(failing_signals=["no_tokens"])

    garbled = sum(1 for t in tokens if _GARBLED_RE.search(t))
    content = sum(1 for t in tokens if _is_content_token(t))
    word_lengths = [len(t) for t in tokens]

    garbled_ratio  = garbled / total
    dict_hit_rate  = content / total
    mean_word_len  = sum(word_lengths) / len(word_lengths)
    repeated_run   = bool(_REPEATED_RE.search(text))
    printable_ratio = sum(
        1 for ch in text if unicodedata.category(ch) != "Cc" and ch.isprintable()
    ) / max(len(text), 1)

    failing: list[str] = []
    if garbled_ratio > _GARBLED_TOKEN_RATIO_MAX:
        failing.append(f"garbled_token_ratio={garbled_ratio:.2f}")
    if mean_word_len < _MEAN_WORD_LENGTH_MIN:
        failing.append(f"mean_word_length={mean_word_len:.2f}")
    if dict_hit_rate < _DICT_HIT_RATE_MIN:
        failing.append(f"dict_hit_rate={dict_hit_rate:.2f}")
    if repeated_run:
        failing.append("repeated_char_run")
    if printable_ratio < _ASCII_PRINTABLE_MIN:
        failing.append(f"ascii_printable_ratio={printable_ratio:.2f}")

    return QualitySignals(
        garbled_token_ratio=garbled_ratio,
        mean_word_length=mean_word_len,
        dict_hit_rate=dict_hit_rate,
        has_repeated_char_run=repeated_run,
        ascii_printable_ratio=printable_ratio,
        failing_signals=failing,
    )


def _layer2_decision(page_elements: list[dict]) -> Decision | None:
    """
    Returns a promote Decision if heuristics fail on the combined page text,
    or None if text quality looks acceptable.
    """
    combined = " ".join(e.get("text", "") for e in page_elements if e.get("text"))
    if not combined.strip():
        return None  # no text to evaluate — Layer 1 / zero-element logic handles this

    signals = _measure_text_quality(combined)
    if not signals.passes:
        return Decision(
            "promote_to_vlm",
            f"heuristic_failed: {', '.join(signals.failing_signals)}",
            layer=2,
        )
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _coverage_decision(
    page_elements: list[dict], text_layer_tokens: int | None
) -> Decision | None:
    """
    Promote when Docling extracted far less text than the PDF's embedded text
    layer contains. Docling's confidence reflects the quality of what it read,
    not its coverage, so a near-empty extraction of a text-rich page can still
    grade EXCELLENT and pass the text-quality heuristics.
    """
    if not text_layer_tokens or text_layer_tokens < _COVERAGE_MIN_TEXT_LAYER_TOKENS:
        return None
    extracted = sum(len((e.get("text") or "").split()) for e in page_elements)
    if extracted < _COVERAGE_RATIO_MIN * text_layer_tokens:
        return Decision(
            "promote_to_vlm",
            f"low_coverage: extracted {extracted} of {text_layer_tokens} text-layer tokens",
            layer=2,
        )
    return None


def evaluate_page(
    page_no: int,
    conversion_result: object,
    page_elements: list[dict],
    page_text_layer_tokens: int | None = None,
) -> Decision:
    """
    Run both layers and return a Decision.

    Args:
        page_no:            0-based page index.
        conversion_result:  The object returned by DocumentConverter.convert().
                            May be None if Docling was not used (image path).
        page_elements:      Elements already extracted by Docling for this page.

    Returns:
        Decision with action="keep" or "promote_to_vlm".
    """
    # Layer 1 — Docling confidence (skipped when conversion_result is None)
    if conversion_result is not None:
        decision = _layer1_decision(conversion_result, page_no)
        if decision is not None:
            logger.debug(
                "Page %d → promote (Layer 1): %s", page_no, decision.reason
            )
            return decision

    # Coverage — did we extract a plausible amount for this page?
    decision = _coverage_decision(page_elements, page_text_layer_tokens)
    if decision is not None:
        logger.debug("Page %d → promote (coverage): %s", page_no, decision.reason)
        return decision

    # Layer 2 — heuristic safety net
    decision = _layer2_decision(page_elements)
    if decision is not None:
        logger.debug("Page %d → promote (Layer 2): %s", page_no, decision.reason)
        return decision

    return Decision("keep", reason=None, layer=None)
