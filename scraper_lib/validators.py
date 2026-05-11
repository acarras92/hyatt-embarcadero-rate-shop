"""Guardrails for Firecrawl extract outputs.
Two gates:
  check_bot_block(markdown)   — RUN BEFORE PASSING MARKDOWN TO LLM
  check_plausibility(extract, ...) — RUN AFTER EXTRACT RETURNS

Philosophy: prefer false-positive (reject legit data, retry) over false-negative
(accept fabricated data). Fabrication is catastrophic; a flagged-for-retry cell
gets a null + status_code and a human can triage. Fake data poisons the analysis
silently."""

from __future__ import annotations
import re
from typing import Optional, Tuple, Any, List

# =============================================================================
# Cell-outcome verdict enum (Resolution 8 — rm-dashboard-rollout skill).
#
# Prior to skill packaging, the manual_rates.csv `expected_verdict` field had
# four values (PASS / FAIL_EXPECTED / FAIL_KNOWN_GAP / NO_DATA), and "no rows
# because the URL is broken" collapsed into NO_DATA alongside "no rows because
# the cell legitimately has no inventory." This conflation made the
# regression-test categorization imprecise: a silently-broken booking URL and
# a sold-out date both appeared identical to the harness.
#
# Resolution 8 introduces FAIL_URL_BROKEN and PASS_NO_INVENTORY as separate
# enum values. Both still resolve to "zero rows persisted," but they encode
# WHY zero rows landed:
#
#   FAIL_URL_BROKEN     — the URL itself is the problem. Bot-block, 404,
#                          5xx, DataDome challenge, Imperva interruption,
#                          or a markdown body that lacks expected-property
#                          identity. The cell needs URL recon, not a re-run.
#
#   PASS_NO_INVENTORY   — the URL works and the page renders for the right
#                          property, but the booking engine returned
#                          "no rooms available" / "sold out" / "not available
#                          on our site for your dates" for the requested
#                          arrival date. Legitimate empty cell; no recon
#                          needed.
#
# The other four values are preserved for backwards compatibility with
# existing manual_rates.csv fixtures.
#
# Consumers (apply_chrome_verification.py et al.) should compare against the
# named constants below rather than literal strings.
# =============================================================================
VERDICT_PASS: str = "PASS"
VERDICT_FAIL_EXPECTED: str = "FAIL_EXPECTED"
VERDICT_FAIL_KNOWN_GAP: str = "FAIL_KNOWN_GAP"
VERDICT_NO_DATA: str = "NO_DATA"
VERDICT_PASS_NO_INVENTORY: str = "PASS_NO_INVENTORY"
VERDICT_FAIL_URL_BROKEN: str = "FAIL_URL_BROKEN"

EXPECTED_VERDICT_VALUES: tuple[str, ...] = (
    VERDICT_PASS,
    VERDICT_FAIL_EXPECTED,
    VERDICT_FAIL_KNOWN_GAP,
    VERDICT_NO_DATA,
    VERDICT_PASS_NO_INVENTORY,
    VERDICT_FAIL_URL_BROKEN,
)

# =============================================================================
# Bot-block signatures — case-insensitive substring match on raw markdown.
# Source: user spec (13) + 2 additional patterns observed in our own recon
# (DataDome "You have been blocked", "Bot or Not").
# =============================================================================
BOT_BLOCK_SIGNATURES: tuple[str, ...] = (
    "captcha-delivery",
    "we can't tell if you're a human",
    "access denied",
    "pardon our interruption",
    "please verify you are a human",
    "checking your browser",
    "something isn't quite right",
    "enable javascript and cookies to continue",
    "just a moment",
    "cloudflare ray id",
    "403 forbidden",
    "request unsuccessful",
    # Observed in our own recon CAPTCHA pages:
    "you have been blocked",
    "bot or not",
    # Added during comp verification (Capital Hilton served this via Akamai):
    "powered and protected by",
    "![akamai]",
    "request blocked",
)


def check_bot_block(markdown: str) -> Optional[str]:
    """Return the matched signature string if bot-block detected, else None.
    Runs BEFORE markdown is passed to an LLM extract step."""
    if not markdown:
        return None
    m_lower = markdown.lower()
    for sig in BOT_BLOCK_SIGNATURES:
        if sig in m_lower:
            return sig
    return None


# =============================================================================
# Plausibility gate on extracted JSON
# =============================================================================
# Reject if property name matches these tokens (LLM fallback placeholders)
FABRICATED_NAME_PATTERN = re.compile(r"\b(example|sample|demo)\b|^\s*test\s*$", re.I)

# For our property universe (luxury DC hotels + AKA extended-stay), no room
# should ever plausibly have a BAR < $100 (ADA/hard-constraint floor from spec)
# and no room should ever plausibly be > $10,000 (spec upper bound).
MIN_PLAUSIBLE_RATE = 100.0
MAX_PLAUSIBLE_RATE = 10000.0

# For placeholder-pattern detection:
# If 3+ rooms ALL have rates ≤ $250 (and our properties are all luxury), it's
# the canonical LLM fallback ($150/$180/$200 pattern). $250 floor chosen because
# no AKA or comp product in the 6-property set has a BAR under $250.
PLACEHOLDER_RATE_CEILING = 250.0

NO_INVENTORY_SIGNATURES: tuple[str, ...] = (
    "we have no availability here between",
    "not available on our site for your dates",
    "no availability",
    "sold out",
    "no rooms available",
)

BROKEN_URL_SIGNATURES: tuple[str, ...] = (
    "404 not found",
    "page not found",
    "this page doesn't exist",
    "we couldn't find",
    "could not find",
    "hotel not found",
)


def _norm_identity_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().replace("-", " ")).strip()


def classify_empty_inventory_page(
    markdown: str,
    *,
    expected_property_tokens: list[str],
) -> Optional[str]:
    """Classify an empty extraction's source page.

    Returns one of (per Resolution 8 verdict enum):
      - ``VERDICT_PASS_NO_INVENTORY`` when the page is for the expected property
        and explicitly says no inventory is available for the requested stay.
      - ``VERDICT_FAIL_URL_BROKEN`` when the page has broken-url signatures, or
        has no expected-property identity to disambiguate an empty extract.
      - ``None`` when there is no empty-inventory signal and normal
        plausibility handling should own the verdict.

    Backwards-compatibility note: prior callers received the literal string
    ``"no_inventory"``. After Resolution 8 this becomes
    ``VERDICT_PASS_NO_INVENTORY`` ("PASS_NO_INVENTORY"). Update consumers
    that string-match the legacy literal.
    """
    if not markdown:
        return None
    md_norm = _norm_identity_text(markdown)
    tokens_norm = [_norm_identity_text(t) for t in expected_property_tokens if t]
    has_expected_identity = (
        not tokens_norm or any(t and t in md_norm for t in tokens_norm)
    )
    has_no_inventory = any(sig in md_norm for sig in NO_INVENTORY_SIGNATURES)
    has_broken = any(sig in md_norm for sig in BROKEN_URL_SIGNATURES)

    if has_broken:
        return VERDICT_FAIL_URL_BROKEN
    if has_no_inventory:
        return (
            VERDICT_PASS_NO_INVENTORY if has_expected_identity
            else VERDICT_FAIL_URL_BROKEN
        )
    return None


# =============================================================================
# Resolution 8 — verification-harness adapter
# =============================================================================
def classify_chrome_cell_verdict(
    *,
    cell_status: Optional[str],
    expected_present: bool,
    rows_extracted: int,
    bot_block_signature: Optional[str] = None,
    http_status: Optional[int] = None,
    empty_page_classification: Optional[str] = None,
) -> str:
    """Map a Chrome-verification cell observation to one of the
    EXPECTED_VERDICT_VALUES, distinguishing FAIL_URL_BROKEN (the URL is the
    problem) from PASS_NO_INVENTORY (the URL works but the cell is legitimately
    empty).

    Inputs:
      cell_status              — opaque status string from the Cowork harness
                                 (e.g. ``"ok"``, ``"bot_blocked"``, ``"http_404"``).
                                 Optional; ``None`` falls through to other signals.
      expected_present         — True if the spec asserts inventory should be
                                 present on this date × channel × property.
      rows_extracted           — count of rate rows extracted from the cell.
      bot_block_signature      — the matched bot-block signature returned by
                                 ``check_bot_block(markdown)``, if any.
      http_status              — observed HTTP status code, if available.
      empty_page_classification — output of ``classify_empty_inventory_page()``
                                  on the cell's markdown, if computed.

    Decision order (most specific first):
      1. bot_block_signature truthy           → FAIL_URL_BROKEN
      2. http_status in {404, 410, 500-599}   → FAIL_URL_BROKEN
      3. cell_status in {"http_404", ...}     → FAIL_URL_BROKEN
      4. empty_page_classification ==
         VERDICT_FAIL_URL_BROKEN              → FAIL_URL_BROKEN
      5. rows_extracted == 0 and
         empty_page_classification ==
         VERDICT_PASS_NO_INVENTORY            → PASS_NO_INVENTORY
      6. rows_extracted > 0 and
         expected_present                     → PASS
      7. rows_extracted == 0 and
         not expected_present                 → PASS_NO_INVENTORY
      8. fallthrough                          → NO_DATA
    """
    if bot_block_signature:
        return VERDICT_FAIL_URL_BROKEN
    if http_status is not None:
        if http_status in (404, 410) or 500 <= http_status < 600:
            return VERDICT_FAIL_URL_BROKEN
    if cell_status:
        cs = cell_status.lower()
        if cs in {
            "http_404", "http_410", "http_5xx", "url_broken",
            "bot_blocked", "datadome", "imperva", "captcha",
        }:
            return VERDICT_FAIL_URL_BROKEN
    if empty_page_classification == VERDICT_FAIL_URL_BROKEN:
        return VERDICT_FAIL_URL_BROKEN
    if rows_extracted == 0:
        if empty_page_classification == VERDICT_PASS_NO_INVENTORY:
            return VERDICT_PASS_NO_INVENTORY
        if not expected_present:
            return VERDICT_PASS_NO_INVENTORY
        return VERDICT_NO_DATA
    if rows_extracted > 0:
        return VERDICT_PASS
    return VERDICT_NO_DATA



def check_plausibility(
    extracted: dict,
    *,
    expected_property_tokens: list[str],
    expected_arrival_date: str,
    markdown: str = "",
    channel: Optional[str] = None,
) -> Tuple[bool, str]:
    """Return (passed, reason).
    expected_property_tokens: tokens that MUST appear in property_name (case-insensitive).
        e.g. ["white house"] for AKA, or ["willard"] for Willard InterContinental.
        Pass at least one token that's distinctive to the target property.
    expected_arrival_date: ISO YYYY-MM-DD. If the extracted arrival_date is present
        and doesn't match, we reject — the LLM fabricated or grabbed a nearby date.
    markdown: the raw markdown body we passed to the extract step. Used to
        (a) distinguish 'page genuinely has no inventory' (thin page, empty rooms → OK)
        from 'page has content but extract missed it' (fat page, empty rooms → reject),
        and (b) verify that extracted rates anchor to $-prefixed tokens in source
        (anti-hallucination guard, see Rule 7).
    channel: channel id ("direct", "booking", "hotels_com"). Enables Rule 7
        Path B (bare-integer anchoring) for Booking.com markdown which renders
        prices without a $-prefix.
    """
    markdown_len = len(markdown or "")
    if not isinstance(extracted, dict):
        return False, f"extract is not a dict: {type(extracted).__name__}"

    pname = str(extracted.get("property_name") or "").strip()

    # Rule 1: no fabricated-name tokens
    if FABRICATED_NAME_PATTERN.search(pname):
        return False, f"property_name looks fabricated: '{pname}'"

    # Rule 2: property_name must contain at least one expected token.
    # Normalize hyphens/extra whitespace on both sides so "The Hay - Adams" matches "hay-adams".
    if expected_property_tokens:
        pname_norm = _norm_identity_text(pname)
        tokens_norm = [_norm_identity_text(t) for t in expected_property_tokens if t]
        if tokens_norm and not any(t in pname_norm for t in tokens_norm):
            return False, (
                f"property_name '{pname}' contains none of expected tokens "
                f"{expected_property_tokens} (after hyphen-normalization)"
            )

    # Rule 3: arrival_date must match the date we requested (when present)
    arrival = str(extracted.get("arrival_date") or "").strip()
    if arrival and arrival != expected_arrival_date:
        return False, (
            f"arrival_date mismatch: got '{arrival}', expected "
            f"'{expected_arrival_date}'"
        )

    rooms = extracted.get("rooms") or []
    if not isinstance(rooms, list):
        return False, f"rooms is not a list: {type(rooms).__name__}"

    # Rule 4: empty rooms[] on a non-thin page = extract failure
    # Threshold 500 chars is well above typical CAPTCHA page length (~1200c)
    # but below any real hotel listing page (~5000c+).
    if not rooms and markdown_len > 500:
        return False, (
            f"empty rooms[] but page has {markdown_len} chars — LLM likely missed structure"
        )

    # Rule 5: rate bounds
    for i, r in enumerate(rooms):
        if not isinstance(r, dict):
            return False, f"room[{i}] is not a dict"
        rate = r.get("rate_per_night_usd")
        # None is legitimate (sold-out)
        if rate is None:
            continue
        if not isinstance(rate, (int, float)) or isinstance(rate, bool):
            return False, f"room[{i}] rate is non-numeric: {rate!r}"
        if rate < MIN_PLAUSIBLE_RATE:
            return False, (
                f"room[{i}] '{r.get('marketing_name','?')}' rate ${rate} below ${MIN_PLAUSIBLE_RATE} floor"
            )
        if rate > MAX_PLAUSIBLE_RATE:
            return False, (
                f"room[{i}] '{r.get('marketing_name','?')}' rate ${rate} above ${MAX_PLAUSIBLE_RATE} ceiling"
            )

    # Rule 6: placeholder pattern — 3+ rooms all at sub-$250 rates.
    # Only meaningful for our luxury property universe; for other use cases
    # this rule should be reparameterized.
    rates_present = [
        r.get("rate_per_night_usd")
        for r in rooms
        if isinstance(r.get("rate_per_night_usd"), (int, float))
        and not isinstance(r.get("rate_per_night_usd"), bool)
    ]
    if len(rates_present) >= 3:
        sub_ceiling = [x for x in rates_present if x <= PLACEHOLDER_RATE_CEILING]
        if len(sub_ceiling) >= 3:
            return False, (
                f"3+ rooms at ≤${PLACEHOLDER_RATE_CEILING} ({sub_ceiling[:5]}) — "
                "LLM placeholder pattern for luxury property"
            )

    # Rule 7: rate-anchor check (anti-hallucination guard, added 2026-04-25
    # after manual PDF validation found 49% of comp cells had fabricated rates).
    # At least one rate the LLM extracted must appear as a $<rate> token in the
    # source markdown. If zero rates anchor, the LLM almost certainly fabricated
    # them onto a page that displayed none. Catches:
    #   - Booking returning 'no availability' but LLM filling plausible discount
    #     rates (Hay-Adams Booking 2026-09-16: $130/$150/$180/$200 invented onto
    #     a page with zero $-prefixed price tokens).
    #   - Direct booking engines returning generic property/rooms-overview pages
    #     without rates but LLM filling a plausible rate band (Willard direct
    #     2026-05-12: $365-$600 invented across all 4 rooms including the
    #     $4,239 Henry Augustus Willard Suite).
    #   - Placeholder direct URLs (Hay-Adams direct, Jefferson direct, St. Regis
    #     direct, Willard direct) that returned generic homepages without rate
    #     ladders for the entire 2026-04-24 run.
    extracted_rates: set[int] = set()
    for r in rooms:
        for p in (r.get("rate_plans") or []):
            v = p.get("rate_per_night_usd")
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                extracted_rates.add(int(v))
    if extracted_rates:
        # Strip thousand-separator commas so e.g. $1,756 anchors to extracted 1756.
        md_normalized = re.sub(r"(\d),(\d{3})", r"\1\2", markdown or "")
        # Path A (Direct, Hotels.com): require $-prefixed token in markdown.
        matched = [v for v in extracted_rates
                   if re.search(rf"\${v}\b", md_normalized)]
        # Path B (Booking, added 2026-04-28): Booking.com markdown renders prices
        # as bare integers (e.g. "1234" or post-comma-strip "1234") with zero $
        # prefix. The 2026-04-28 diagnostic showed Path A structurally fails every
        # Booking cell. Allow bare-integer anchoring on `booking` only, with a
        # negative-lookbehind to exclude already-$-prefixed tokens (so we don't
        # double-count Path A matches) and tight guards against false positives:
        #   - reject 2026 / 2027 (year tokens that frequently appear in markdown)
        #   - reject < 100 (day-of-month, room counts, etc.)
        #   - reject > 9999 (implausible USD/night, probably an ID/number)
        if channel == "booking":
            for v in extracted_rates:
                if v in matched:
                    continue
                if v in (2026, 2027) or v < 100 or v > 9999:
                    continue
                if re.search(rf"(?<!\$)\b{v}\b", md_normalized):
                    matched.append(v)
        if not matched:
            sample = sorted(extracted_rates)[:8]
            more = "..." if len(extracted_rates) > 8 else ""
            return False, (
                f"rate-anchor check: 0 of {len(extracted_rates)} unique extracted "
                f"rates ({sample}{more}) appear as $<rate> tokens in markdown — "
                "extract appears fabricated"
            )

    return True, ""


# =============================================================================
# Rule 8: room-type anchor (v2, added 2026-04-26 after Chrome verification
# found "Two Bedroom Duplex Penthouse" hallucinated onto Direct cells where
# no such SKU exists). Every extracted marketing_name MUST appear verbatim
# in the source markdown, or the extract is treated as fabricated room-type
# data. Caller blocks the cell from raw_rates.csv on FAIL.
# =============================================================================
def check_room_type_anchored(
    extracted: dict,
    markdown: str,
) -> Tuple[bool, str]:
    """Return (passed, reason). Walks rooms[]; each marketing_name must
    appear as substring of markdown. Verbatim — no normalization.

    Conservative: a single hallucinated room fails the whole cell so the
    scraper retries with the sharper prompt. Channel-symmetric: applies
    to Direct, Booking, Hotels.com equally.
    """
    if not isinstance(extracted, dict):
        return True, ""  # plausibility gate handles this case
    rooms = extracted.get("rooms") or []
    if not rooms:
        return True, ""  # empty rooms is plausibility's domain, not anchoring's
    md = markdown or ""
    if not md:
        return False, "room-type anchor check: markdown is empty"

    unanchored: list[str] = []
    for r in rooms:
        if not isinstance(r, dict):
            continue
        name = (r.get("marketing_name") or "").strip()
        if not name:
            continue
        if name not in md:
            unanchored.append(name)
    if unanchored:
        sample = unanchored[:3]
        more = f" (+{len(unanchored)-3} more)" if len(unanchored) > 3 else ""
        return False, (
            f"room-type anchor check: {len(unanchored)} marketing_name(s) not "
            f"verbatim in markdown — likely hallucinated: {sample}{more}"
        )
    return True, ""


# =============================================================================
# Rule 9: BAR canonical rate-anchor (Phase A2.4 #5, 2026-04-26).
#
# Problem the prior Rule 7 missed: it required AT LEAST ONE extracted rate
# to anchor in markdown. So a cell with real $295 + hallucinated $312 still
# passes Rule 7 (the $295 anchors). The hallucinated $312 silently lands in
# raw_rates.csv. If $312 was the BAR_FLEX pick, downstream channel-parity
# analysis is wrong.
#
# This rule tightens the check for the two CANONICAL plans only:
#   - The picked BAR_NON_REF rate must anchor in markdown.
#   - The picked BAR_FLEX rate must anchor in markdown.
# Other plan rates remain best-effort (Rule 7 still catches total fabrication).
#
# Hotels.com tier add-on exception: when a plan label contains "+$N"
# (the multi-tier add-on math from EXTRACT_PROMPT), the rate is computed
# (base_nightly + addon). We accept the anchor if BOTH the base ($N0 - $N)
# and the literal "+$N" string appear in markdown — same evidence the
# extractor used to compute the rate.
# =============================================================================
def _normalize_markdown_dollar_tokens(markdown: str) -> str:
    """Strip thousand-separator commas inside $-prefixed tokens so e.g.
    '$1,807' anchors to extracted 1807."""
    return re.sub(r"(\d),(\d{3})", r"\1\2", markdown or "")


def _bar_rate_anchored(rate: float, label: str, md_normalized: str) -> bool:
    """Return True if `rate` (a picked BAR_NON_REF or BAR_FLEX rate) is
    evidenced in `md_normalized`. Two acceptance paths:
      1. Direct: `${int(rate)}` appears verbatim in markdown.
      2. Hotels.com tier add-on: label has "+$N" → check base ($int(rate)-N)
         appears AND the literal "+$N" appears.

    The trailing `(?!\\d)` (negative lookahead, "not followed by a digit")
    replaces a previous `\\b` end-anchor. `\\b` is a word/non-word transition,
    which fails when the digit is immediately followed by a letter — e.g.
    Firecrawl serializes Hotels.com radio button rows as
    `\\+ $163Reserve now, pay deposit`, with no whitespace between `$163` and
    `Reserve`. We still need to reject longer numbers ("$1634"), so we
    forbid only a trailing digit, not arbitrary word chars.
    """
    try:
        rate_int = int(rate)
    except (TypeError, ValueError):
        return False
    if rate_int <= 0:
        return False
    if re.search(rf"\${rate_int}(?!\d)", md_normalized):
        return True
    # Hotels.com tier add-on path. Accept "+$46" or "+ $46" (Hotels.com's
    # markdown sometimes inserts whitespace between the operator and amount).
    m = re.search(r"\+\s*\$(\d+)", label or "")
    if m:
        addon = int(m.group(1))
        base = rate_int - addon
        if base > 0:
            if (re.search(rf"\${base}(?!\d)", md_normalized)
                    and re.search(rf"\+\s*\${addon}(?!\d)", md_normalized)):
                return True
    return False


def check_canonical_bar_anchored(
    extracted: dict, markdown: str,
) -> Tuple[bool, str, List[dict]]:
    """Verify every per-room BAR_NON_REF and BAR_FLEX picked rate appears in
    `markdown` (via _bar_rate_anchored).

    Returns (passed, reason, warnings):
      passed   — False if ANY BAR pick fails its anchor; cell must be
                 rejected (no rows persisted).
      reason   — Human-readable summary of the first failure (truncated).
      warnings — List of partial-hallucination notes for non-canonical
                 plans (does NOT fail the cell; logged as WARN downstream).

    Imports normalize lazily so validators.py has no module-load cycle on
    test import (normalize.py imports from canonical_maps.py only).
    """
    from normalize import (
        pick_canonical_bar, pick_canonical_flex, refundability_state,
        NON_REFUNDABLE, REFUNDABLE,
    )
    from scrape import parse_bundle_from_text  # local import — runtime only

    if not isinstance(extracted, dict):
        return True, "", []
    rooms = extracted.get("rooms") or []
    if not rooms:
        return True, "", []
    md_normalized = _normalize_markdown_dollar_tokens(markdown)

    warnings: List[dict] = []
    fail_msgs: List[str] = []

    for r_idx, room in enumerate(rooms):
        if not isinstance(room, dict):
            continue
        plans = room.get("rate_plans") or []
        # Mirror _flatten_rooms_to_rows preprocessing so the picker sees
        # the same plans the row writer will. Mutates in place.
        for p in plans:
            if not isinstance(p, dict):
                continue
            p["bundle_inclusions"] = parse_bundle_from_text(p.get("rate_plan_label") or "")
        bar_pick = pick_canonical_bar(plans)
        flex_pick = pick_canonical_flex(plans)
        room_label = (room.get("marketing_name") or f"<room {r_idx}>")

        for kind, pick in (("BAR_NON_REF", bar_pick), ("BAR_FLEX", flex_pick)):
            if pick is None:
                continue
            _, plan = pick
            rate = plan.get("rate_per_night_usd")
            label = plan.get("rate_plan_label") or ""
            if not _bar_rate_anchored(rate, label, md_normalized):
                fail_msgs.append(
                    f"{kind} ${rate} for room '{room_label}' "
                    f"(label: '{label[:40]}') not anchored in markdown"
                )

        # Best-effort: warn for non-canonical plans whose rate doesn't anchor.
        # Pass-through; doesn't fail the cell.
        for i, p in enumerate(plans):
            if not isinstance(p, dict):
                continue
            if (bar_pick and i == bar_pick[0]) or (flex_pick and i == flex_pick[0]):
                continue
            rate = p.get("rate_per_night_usd")
            label = p.get("rate_plan_label") or ""
            if not isinstance(rate, (int, float)) or isinstance(rate, bool):
                continue
            if rate <= 0:
                continue
            if not _bar_rate_anchored(rate, label, md_normalized):
                warnings.append({
                    "kind": "non_canonical_rate_unanchored",
                    "room": room_label,
                    "rate": rate,
                    "label": label[:60],
                })

    if fail_msgs:
        msg = "; ".join(fail_msgs[:3])
        more = f" (+{len(fail_msgs)-3} more)" if len(fail_msgs) > 3 else ""
        return False, f"FAIL_BAR_NOT_ANCHORED: {msg}{more}", warnings
    return True, "", warnings


# =============================================================================
# Convenience: full pipeline gate — call this from the scraper
# =============================================================================
def gate(
    markdown: str,
    extracted_json: Optional[dict],
    *,
    expected_property_tokens: list[str],
    expected_arrival_date: str,
    channel: Optional[str] = None,
) -> Tuple[str, str]:
    """Returns (status, reason).
    status ∈ {'ok', 'bot_blocked', 'extract_failed'}.
    'ok' means the extract passed all gates — safe to persist.
    """
    sig = check_bot_block(markdown)
    if sig:
        return "bot_blocked", f"bot-block signature matched: {sig!r}"
    if extracted_json is None:
        return "extract_failed", "no extracted_json returned by scraper"
    passed, reason = check_plausibility(
        extracted_json,
        expected_property_tokens=expected_property_tokens,
        expected_arrival_date=expected_arrival_date,
        markdown=markdown or "",
        channel=channel,
    )
    if not passed:
        return "extract_failed", reason
    return "ok", ""
