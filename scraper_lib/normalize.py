"""Canonical rate-plan picker — one BAR_NON_REF + one BAR_FLEX per cell.

Why this exists
---------------
Cross-channel rate-parity comparisons require comparing the SAME plan-type
on every channel. The 2026-04-26 Chrome verification showed:
  - Direct sells 3 plans per room (Pay Now / Visa / Mastercard)
  - Booking sells 4 plans per room (non-ref / non-ref+parking / flex / flex+parking)
  - Hotels.com sells 4 plans per room (cancel × pay-timing combinations)

The legacy classify_bar() picked the HIGHEST-PRICED refundable plan as BAR.
That collapses non-ref bare and bundled-with-parking into the same comparison
basket, which is why the prior raw_rates.csv had Booking show "no LOS=7
discount" — the pick was landing on the parking-bundled flex rate (40
USD/night more than bare non-ref) and washing out the actual 15% LOS
discount banner Booking publishes.

This module replaces classify_bar with two strict, deterministic pickers:

  pick_canonical_bar(rows)  → row representing BAR_NON_REF for the cell
  pick_canonical_flex(rows) → row representing BAR_FLEX for the cell

A "cell" here means: all rate plans for a single (channel × property ×
room_canonical × checkin × los) tuple. In practice that maps to a single
room.rate_plans[] list inside the extractor JSON, so callers feed one room's
plans at a time.

Rules (BAR_NON_REF):
  1. Filter to rows where bundle_inclusions is empty/null (bare; no parking
     or other inclusions). Bundled rates inflate the comparison basis and
     are tracked separately as BUNDLED_PARKING_NON_REF / BUNDLED_PARKING_FLEX.
  2. Filter to rows whose refundability_state() is NON_REFUNDABLE — verbatim
     non-ref token in cancellation_phrase or rate_plan_label. UNKNOWN /
     REFUNDABLE rows are NEVER picked. (Phase A2.4 #3 fail-closed: prior
     bool _is_refundable() defaulted ambiguous plans to refundable=true,
     which could phantom-pick a "Standard Rate" with unknown cancellation
     policy as BAR_FLEX.)
  3. Filter out member/affiliate rates (Genius, Visa Offer, Mastercard
     Offer, Amex Offer, AAA, etc.). These are loyalty-gated and aren't
     the property's posted BAR.
  4. Among survivors, pick the LOWEST nightly rate.
  5. If no survivors → return None. Caller writes a sentinel row with
     rate_plan_canonical=FAIL_NO_BARE_NON_REF + status=FAIL_NO_BARE_NON_REF
     so the cell is distinguishable from "we never tried" downstream.

Rules (BAR_FLEX):
  1. Same bare filter (bundle_inclusions empty/null).
  2. Filter to rows whose refundability_state() is REFUNDABLE — verbatim
     "free cancellation" / "refundable" / "fully refundable" token in
     cancellation_phrase or rate_plan_label. UNKNOWN never picked.
  3. Pick the LOWEST nightly rate.
  4. If no survivors → return None. Caller writes FAIL_NO_BAR_FLEX sentinel.

Both pickers return the (index, plan_dict) of the chosen plan, or None.
"""
from __future__ import annotations
import re
from typing import Optional

from canonical_maps import NON_BAR_LABEL_TOKENS

# =============================================================================
# Tri-state refundability (Phase A2.4 #3, 2026-04-26)
# =============================================================================
# Replaces the prior bool _is_refundable() which defaulted ambiguous plans
# to refundable=True (could phantom-pick a "Standard Rate" with unknown
# cancellation policy as BAR_FLEX). The fail-closed semantics: only pick a
# plan whose refundability is EXPLICIT in the source, never inferred.
REFUNDABLE: str = "REFUNDABLE"
NON_REFUNDABLE: str = "NON_REFUNDABLE"
UNKNOWN: str = "UNKNOWN"

# Phrase tokens (case-insensitive substring match on cancellation_phrase) —
# the highest-confidence signal because they come from the per-plan
# cancellation copy captured verbatim by the extractor.
NON_REFUNDABLE_PHRASE_TOKENS: tuple[str, ...] = (
    "non-refundable", "non refundable", "nonrefundable",
)
REFUNDABLE_PHRASE_TOKENS: tuple[str, ...] = (
    "free cancellation", "fully refundable", "refundable",
)

# Label tokens — fallback when cancellation_phrase is missing/ambiguous.
# Still deterministic: the label is also captured verbatim from the page.
NON_REFUNDABLE_LABEL_TOKENS: tuple[str, ...] = (
    "non-refund", "non refund", "nonrefund",
    "prepay", "pre-pay", "pre paid", "prepaid",
    "pay now", "pay-now",
    "advance purchase", "advance-purchase",
)
REFUNDABLE_LABEL_TOKENS: tuple[str, ...] = (
    "free cancellation", "fully refundable",
    "pay flex", "pay-flex",
)


# Tokens (case-insensitive substring match on rate_plan_label) that mark a
# rate as non-refundable when the structured `refundable` flag is missing or
# unreliable. Belt-and-braces: prefer the structured flag, fall back to text.
# Kept as alias for backward compatibility with audit_canonical_bar.py.
NON_REF_LABEL_TOKENS: tuple[str, ...] = NON_REFUNDABLE_LABEL_TOKENS


def _label_contains_any(label: Optional[str], tokens: tuple[str, ...]) -> bool:
    if not label:
        return False
    lo = label.lower()
    return any(t in lo for t in tokens)


def refundability_state(plan: dict) -> str:
    """Return REFUNDABLE / NON_REFUNDABLE / UNKNOWN for a plan dict.

    Resolution order (fail-closed — only commit when the signal is explicit):
      1. cancellation_phrase contains a phrase-level non-ref / refundable
         token → NON_REFUNDABLE / REFUNDABLE.
      2. rate_plan_label contains a label-level non-ref / refundable token
         (still deterministic; label captured verbatim from page).
      3. Otherwise → UNKNOWN. The LLM-emitted `refundable` bool is NOT
         consulted: it has been observed to invert on ambiguous Booking
         labels, and trusting it defeats the fail-closed intent.

    UNKNOWN plans are never picked as BAR_NON_REF or BAR_FLEX.
    """
    if not isinstance(plan, dict):
        return UNKNOWN
    phrase = (plan.get("cancellation_phrase") or "").strip().lower()
    label = (plan.get("rate_plan_label") or "").strip().lower()

    if any(t in phrase for t in NON_REFUNDABLE_PHRASE_TOKENS):
        return NON_REFUNDABLE
    if any(t in phrase for t in REFUNDABLE_PHRASE_TOKENS):
        return REFUNDABLE
    if any(t in label for t in NON_REFUNDABLE_LABEL_TOKENS):
        return NON_REFUNDABLE
    if any(t in label for t in REFUNDABLE_LABEL_TOKENS):
        return REFUNDABLE
    return UNKNOWN


def count_unknown_refundability(extracted: dict) -> int:
    """Count plans across all rooms whose refundability_state() is UNKNOWN.
    Used for per-cell diagnostics — high counts signal that the channel's
    cancellation_phrase capture is failing or label tokens are drifting.
    """
    if not isinstance(extracted, dict):
        return 0
    n = 0
    for room in (extracted.get("rooms") or []):
        if not isinstance(room, dict):
            continue
        for plan in (room.get("rate_plans") or []):
            if refundability_state(plan) == UNKNOWN:
                n += 1
    return n


def _is_bare(plan: dict) -> bool:
    """True if no bundle inclusions. Treats empty string and null both as bare."""
    bundles = plan.get("bundle_inclusions")
    if bundles is None or bundles == "":
        return True
    if isinstance(bundles, str) and not bundles.strip():
        return True
    return False


def _is_member_or_card_offer(plan: dict) -> bool:
    """Genius / Visa / Mastercard / loyalty-gated. Disqualified from BAR."""
    if plan.get("is_genius_member_rate") is True:
        return True
    return _label_contains_any(plan.get("rate_plan_label"), NON_BAR_LABEL_TOKENS_NO_NONREF)


# Subset of NON_BAR_LABEL_TOKENS that excludes the non-ref tokens (since
# non-ref rates ARE candidates for BAR_NON_REF).
NON_BAR_LABEL_TOKENS_NO_NONREF: tuple[str, ...] = tuple(
    tok for tok in NON_BAR_LABEL_TOKENS
    if not any(nr in tok for nr in ("non-refund", "non refund", "nonrefund",
                                     "prepay", "pay now", "advance purchase"))
)


def _has_numeric_rate(plan: dict) -> bool:
    r = plan.get("rate_per_night_usd")
    return isinstance(r, (int, float)) and not isinstance(r, bool) and r > 0


# =============================================================================
# Public pickers
# =============================================================================
def pick_canonical_bar(plans: list[dict]) -> Optional[tuple[int, dict]]:
    """Pick the (index, plan) representing BAR_NON_REF for the cell.

    Returns None if no plan satisfies bare + NON_REFUNDABLE + non-member +
    has rate. UNKNOWN refundability is never picked (Phase A2.4 #3 fail-closed).
    """
    if not plans:
        return None
    candidates: list[tuple[int, dict]] = []
    for i, p in enumerate(plans):
        if not isinstance(p, dict):
            continue
        if not _has_numeric_rate(p):
            continue
        if not _is_bare(p):
            continue
        if refundability_state(p) != NON_REFUNDABLE:
            continue
        if _is_member_or_card_offer(p):
            continue
        candidates.append((i, p))
    if not candidates:
        return None
    return min(candidates, key=lambda x: x[1]["rate_per_night_usd"])


def pick_canonical_flex(plans: list[dict]) -> Optional[tuple[int, dict]]:
    """Pick the (index, plan) representing BAR_FLEX for the cell.

    Returns None if no plan satisfies bare + REFUNDABLE + non-member + has
    rate. UNKNOWN refundability is never picked (Phase A2.4 #3 fail-closed).
    """
    if not plans:
        return None
    candidates: list[tuple[int, dict]] = []
    for i, p in enumerate(plans):
        if not isinstance(p, dict):
            continue
        if not _has_numeric_rate(p):
            continue
        if not _is_bare(p):
            continue
        if refundability_state(p) != REFUNDABLE:
            continue
        if _is_member_or_card_offer(p):
            continue
        candidates.append((i, p))
    if not candidates:
        return None
    return min(candidates, key=lambda x: x[1]["rate_per_night_usd"])


def classify_rate_plan(plan: dict) -> Optional[str]:
    """Return the canonical bucket for a single rate plan, or None.

    Buckets:
      BAR_NON_REF / BAR_FLEX                    — bare, non-ref / refundable
      BUNDLED_PARKING_NON_REF / BUNDLED_PARKING_FLEX  — has parking inclusion
      MEMBER_OR_CARD_OFFER                       — Genius / Visa / MC / loyalty-gated
      None                                       — doesn't fit a bucket (skip)

    Note: pick_canonical_bar/flex pick the BEST representative per cell;
    classify_rate_plan tags every row's rate_plan_canonical column. The two
    BAR rows the pickers selected are also tagged BAR_NON_REF / BAR_FLEX.
    """
    if not isinstance(plan, dict) or not _has_numeric_rate(plan):
        return None
    bundles = (plan.get("bundle_inclusions") or "").lower()
    has_parking = "parking" in bundles
    state = refundability_state(plan)
    if _is_member_or_card_offer(plan):
        return "MEMBER_OR_CARD_OFFER"
    if has_parking:
        if state == NON_REFUNDABLE:
            return "BUNDLED_PARKING_NON_REF"
        if state == REFUNDABLE:
            return "BUNDLED_PARKING_FLEX"
        return None
    if _is_bare(plan):
        if state == NON_REFUNDABLE:
            return "BAR_NON_REF"
        if state == REFUNDABLE:
            return "BAR_FLEX"
        return None  # UNKNOWN refundability — can't classify
    return None  # bare-with-non-parking-inclusion, etc.
