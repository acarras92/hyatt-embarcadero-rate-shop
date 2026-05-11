"""Production scraper for Hyatt Regency San Francisco Embarcadero rate-market diligence.
Firecrawl-based, with guardrails and retries.

Key design decisions (all derived from user requirements):
  1. Schema extracts EVERY rate plan per room (BAR, non-refundable, member, mobile-only, etc.)
     as separate entries. BAR is classified post-extract via classify_bar().
  2. Bot-block guard runs on raw markdown BEFORE the LLM's JSON is trusted.
  3. Plausibility guard runs on the extracted JSON (see validators.py).
  4. Variance check scans subject-specific markdown patterns harvested from the
     canonical room map; if a known marketing-name token appears in markdown but
     not in extracted rooms, retry ONCE with a sharper prompt, then commit as
     extract_incomplete. # narrative slot — drop AKA-specific examples per-deal
  5. Bot-block retries: up to 3 attempts with 60s pause between them.
  6. Channels filtered per-property: Expedia runs only on AKA (Option A credit strategy).
  7. Hard credit ceiling: stop the run if nominal credits exceed 5500.

Usage:
    py scrape.py --cells <prop>:<channel>:<arrival>:<los> [<prop>:... ...]
    py scrape.py --full
    py scrape.py --full --dry-run
"""

from __future__ import annotations
import os, sys, json, time, datetime, re, csv, argparse
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv
import requests

from validators import (
    check_bot_block, check_plausibility, check_room_type_anchored,
    check_canonical_bar_anchored, classify_empty_inventory_page,
)
from schema import RAW_HEADER, NEW_COLUMN_DEFAULTS
from canonical_maps import lookup_canonical_room
from normalize import (
    pick_canonical_bar, pick_canonical_flex, classify_rate_plan,
    count_unknown_refundability,
)
# Phase A2.5 (2026-04-27) — Hotels.com vision path. Firecrawl screenshot +
# claude-haiku-4-5 vision extraction. Returns the same {success, data:
# {markdown, json}} shape as firecrawl_scrape_json, so gate-1..5 plumbing
# below is unchanged. Routed via use_vision_for_hotels_com config flag.
# Codex item 3 (2026-04-28): import lazily inside the vision branch so
# production deployments without anthropic + Pillow installed don't pay
# an import-time cost when use_vision_for_hotels_com is False.
# Phase A2.5b (2026-04-27) — Hotels.com cancellation-tier deterministic
# parser. Replaces LLM-extracted rate_plans with markdown-anchored tier
# expansion when the per-room cancellation block rendered. Runs as a
# post-processor on extracted JSON before the gate-1..5 chain.
from hotels_markdown import expand_tiers_from_markdown

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
# .env + config.json live in the production working dir (not in the repo).
# Defer the hard-fail to first use so tests can import the module from the
# repo without those files. Anything that actually calls Firecrawl /
# build_url will trip the runtime check below.
KEY = os.environ.get("FIRECRAWL_API_KEY") or ""
try:
    CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
except FileNotFoundError:
    CONFIG = {"properties": [], "channels": [], "date_matrix": [],
              "length_of_stay_nights": []}
OUT_DIR = HERE / "cell_output"
OUT_DIR.mkdir(exist_ok=True)
LOG_PATH = HERE / "scrape_log.txt"
RAW_CSV = HERE / "raw_rates.csv"
RESUME_STATE_PATH = HERE / "resume_state.json"

HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

# -------- Credit model (nominal; we count calls since Firecrawl doesn't return credit headers) --------
CREDITS_BASIC_JSON = 5          # scrape+json via LLM
CREDITS_STEALTH_JSON = 25       # + stealth 5x multiplier
CREDITS_CEILING = 5500          # per user requirement — hard stop

# -------- Bot-block retry configuration --------
BOT_BLOCK_MAX_ATTEMPTS = 3      # up to 3 tries on a bot_blocked cell
BOT_BLOCK_PAUSE_SECONDS = 60    # pause between bot-block retries
EXTRACT_INCOMPLETE_MAX_ATTEMPTS = 2  # 1 initial + 1 retry with sharper prompt

# Phase A2.4 #7 (2026-04-26) — expected room counts per channel for Embarcadero.
# Used by the Hotels.com gate-4 retry logic to catch the case where the
# page rendered some rooms but not all of them (e.g., 1 room with 3 tiers
# passes the per-room max>=2 gate even when 4 other expected rooms are
# missing). Override per-channel from config["expected_room_count_by_channel"]
# when present.
# Phase A2.5b (2026-04-27): hotels_com expected count raised 5→6. Hotels.com
# breaks out the Mobility-Accessible 1BR variant as a 6th room card, while
# direct/booking present 5 SKUs. Live page footer reads "Showing 6 of 6 rooms".
DEFAULT_EXPECTED_ROOM_COUNT_BY_CHANNEL: dict[str, int] = {
    "direct": 18, "booking": 12, "hotels_com": 17, "expedia": 18,
}
EXPECTED_ROOM_COUNT_BY_CHANNEL: dict[str, int] = {
    **DEFAULT_EXPECTED_ROOM_COUNT_BY_CHANNEL,
    **(CONFIG.get("expected_room_count_by_channel") or {}),
}

# -------- CLI-driven overrides (set by main()); 0 means "use channel default" --------
_OVERRIDE_FIRECRAWL_TIMEOUT_MS = 0
_OVERRIDE_DIRECT_WAIT_FOR_MS = 0
_OVERRIDE_MIN_WAIT_FOR_MS = 0       # floor applied to ALL channels' waitFor

# -------- Schema: rate_plans[] inside each room --------
# v2.2 (2026-04-26 smoke #2): bundle_inclusions REMOVED from the schema.
# The LLM nondeterministically tagged property-amenity WiFi as a per-rate
# bundle, breaking BAR-bare picking on AKA Direct + Booking. Bundle is now
# derived deterministically in _flatten_rooms_to_rows from the rate-plan
# label only, anchored on explicit "Includes" / "+" markers (see
# parse_bundle_from_text). The LLM has no input on bundle classification.
#
# v2.2 also adds cancellation_phrase: verbatim capture of the per-plan
# cancellation copy ("Non-refundable" / "Free cancellation before [date]"
# / "Free cancellation"). _flatten_rooms_to_rows derives refundable from
# this phrase deterministically, overriding the LLM's `refundable` field
# (which inverted on Booking when labels were ambiguous).
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "property_name": {"type": "string"},
        "arrival_date": {"type": "string"},
        "nights": {"type": "integer"},
        "rooms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "marketing_name": {"type": "string"},
                    "sub_description": {"type": "string"},
                    "bed_config": {"type": "string"},
                    "occupancy_max": {"type": "integer"},
                    "rate_plans": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "rate_plan_label": {"type": "string"},
                                "rate_per_night_usd": {"type": "number"},
                                "total_stay_usd": {"type": "number"},
                                "refundable": {"type": "boolean"},
                                "is_genius_member_rate": {"type": "boolean"},
                                "includes_breakfast": {"type": "boolean"},
                                "rate_plan_confidence": {"type": "string"},
                                "availability_status": {"type": "string"},
                                # v2.2: verbatim cancellation phrase (deterministic refundability source)
                                "cancellation_phrase": {"type": "string"},
                                # v2: strikethrough still LLM-extracted (works reliably; small numeric)
                                "strikethrough_orig_rate": {"type": "number"},
                            },
                            "required": ["rate_plan_label", "rate_per_night_usd"],
                        },
                    },
                },
                "required": ["marketing_name", "rate_plans"],
            },
        },
    },
    "required": ["property_name", "rooms"],
}

EXTRACT_PROMPT = (
    "Extract EVERY bookable room type shown on this page. "
    "For each room type, list EVERY visible rate plan as a separate entry in rate_plans[]. "
    "CRITICAL: many hotel booking pages show an UNLABELED standard rate (the BAR / Best Available Rate / Flex Rate) "
    "displayed alongside named promotional rates. This unlabeled rate is typically the highest-priced refundable option "
    "and is usually shown first or displayed more prominently. You MUST include it as a rate plan with label "
    "'Standard Rate' or 'Best Flexible Rate' even if the page doesn't explicitly label it. "
    "Complete list of rate-plan types to capture if present: "
    "  - standard BAR (unlabeled, or 'Standard Rate', 'Flex Rate', 'Best Available Rate', 'Free Cancellation'), "
    "  - non-refundable prepay rates ('Non-refundable', 'Pay Now and Save', 'Advance Purchase'), "
    "  - member-only rates ('Genius Member', 'Marriott Bonvoy', 'Hilton Honors', 'World of Hyatt'), "
    "  - mobile-app-exclusive rates ('Mobile-only', 'Mobile App Deal'), "
    "  - card-issuer rates ('Visa Offer', 'Mastercard Offer', 'Amex Offer'), "
    "  - breakfast-included rates. "
    "Use the EXACT rate_plan_label as displayed on the page. "
    "Set refundable=true ONLY if the specific rate plan's cancellation policy permits free cancellation. "
    "Set refundable=false if it says 'Non-refundable' or 'pre-paid'. "
    "Do NOT use the page default — inspect the per-plan cancellation text. "
    "Set rate_plan_confidence='high' only if the rate_plan_label is explicitly shown; 'medium' if inferred; 'low' if ambiguous. "
    "Set rate_per_night_usd to the displayed per-night USD rate (pre-tax). Use null ONLY if the specific plan is sold out. "
    "Include the ACTUAL property_name from the page header — do not substitute generic names. "
    "If the page is a captcha, error, or empty, return property_name:'' and rooms:[]. "
    # ---- v2 additions (2026-04-26 Chrome verification fix) ----
    "ROOM-TYPE ANCHORING: marketing_name MUST be the EXACT visible text in the same rate-card "
    "container as the price. Walk up at most 2 DOM levels from the price to find the room heading. "
    "If you cannot find a clearly-associated room-type heading in that scope, set marketing_name to '' "
    "(empty string) for that rate plan rather than guessing. Do NOT generate a descriptive label from "
    "your own knowledge of the property. Do NOT abbreviate. Do NOT add qualifiers like 'Duplex', "
    "'Penthouse Suite', or '2BR' that aren't in the source text. "
    # ---- v2.4 (2026-04-27): exclude off-channel sales-team-only rooms ----
    "OFF-CHANNEL SALES ROOMS: ignore any room marked as 'available exclusively through our sales "
    "team' or with similar inquire-only / contact-sales / off-channel sales copy. These are NOT "
    "bookable rate-card rooms and must NOT appear in rooms[]. Common signals to exclude: section "
    "headings or descriptions containing 'available exclusively through our sales team', 'specialty "
    "suite available exclusively', 'To inquire or reserve', 'To inquire, please contact', or that "
    "direct the user to email an AKA sales address (e.g. nysales@stayaka.com, "
    "akawp.reservations@stayaka.com, akamaryleboneteam@stayaka.com). If the room block contains a "
    "contact-sales instruction in lieu of a bookable nightly rate, exclude that room. Only include "
    "rooms that have a visible bookable nightly rate alongside a rate-plan selection. "
    # ---- v2.2 (2026-04-26 smoke #2 fix): verbatim cancellation phrase + no bundle inference ----
    "CANCELLATION PHRASE: copy the per-plan cancellation copy VERBATIM into cancellation_phrase. "
    "Examples of strings to capture exactly as shown: 'Non-refundable', 'Free cancellation before "
    "August 5, 2026', 'Free cancellation', 'Non-refundable - no changes allowed'. Do NOT paraphrase. "
    "Do NOT translate 'Non-refundable' to 'Pay Now' or vice versa — capture the literal text shown "
    "next to that specific rate plan's cancellation policy. If no phrase is visible, leave empty. "
    "Refundability is derived deterministically downstream from this phrase, so verbatim accuracy "
    "matters more than your own classification of the plan. "
    "DO NOT POPULATE bundle_inclusions. Bundle membership is derived deterministically from the "
    "rate-plan label downstream. The LLM's job here is to capture the LABEL TEXT verbatim "
    "(e.g. 'Non-refundable + Parking', 'Includes 1 parking spot') in rate_plan_label — the "
    "post-processor extracts bundle from that. Do NOT separately tag amenities like WiFi as bundles "
    "unless they appear as 'Includes WiFi' in the rate-plan label itself. "
    "STRIKETHROUGH: if the page shows an original price with strikethrough adjacent to the displayed "
    "rate (e.g. ~~$495~~ → $492), set strikethrough_orig_rate to the original numeric value. "
    "If no strikethrough is shown, omit the field. "
    # ---- v2.3 (2026-04-26 Phase A2.3): Hotels.com tier add-on math ----
    "HOTELS.COM TIER ADD-ONS: when the page shows a per-room 'Cancellation policy' (or similar) "
    "block with rows like 'Non-Refundable + $0', 'Fully refundable before <date> + $46', "
    "'Non-Refundable + $81', 'Fully refundable + $128', these are FOUR DISTINCT rate plans, NOT "
    "duplicates. Each '+$N' is an add-on amount over the base nightly rate displayed elsewhere "
    "in the room block (e.g., '$295 nightly'). For each tier row, set: "
    "  rate_per_night_usd = base_nightly + add_on_amount  "
    "  (so the four plans above with base=$295 become $295, $341, $376, $423 respectively). "
    "Set rate_plan_label to a string that disambiguates the tier — append the pay-timing copy "
    "if present, e.g. 'Non-Refundable + $0 (pay deposit)', 'Non-Refundable + $81 (pay later)'. "
    "Do NOT emit four plans all at the same rate; that erases the cancellation/pay-timing premium "
    "spread that is the headline structural feature of Hotels.com's rate display."
)

EXTRACT_PROMPT_SHARPER = EXTRACT_PROMPT + (
    "\n\nIMPORTANT: a previous extraction attempt on this page missed some room types. "
    "Be especially thorough. Look for ALL of the following on this page and include them if present: "
    "H Street View variants, Platinum Suite tiers, Penthouse suites, Private Terrace units, "
    "Duplex units, Accessible/ADA variants. Each of these is a distinct bookable product. "
    "If you see any of these mentioned in the page text, they MUST appear in rooms[]."
)

# -------- Variance-check patterns (AKA-specific) --------
# Each entry: (label, markdown_heading_pattern, rooms_concat_pattern).
# Markdown pattern requires section-heading context (### on Synxis/Expedia,
# | [ on Booking tables) to avoid narrative-text false-positives.
# Rooms pattern matches the bare term anywhere in extracted marketing_names.
VARIANCE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("H Street View",   r"(?:###|\|\s*\[)[^\n|]*h\s*street\s*view",   r"h\s*street\s*view"),
    ("Platinum Suite",  r"(?:###|\|\s*\[)[^\n|]*platinum\s*suite",    r"platinum\s*suite"),
    ("Penthouse",       r"(?:###|\|\s*\[)[^\n|]*penthouse",           r"penthouse"),
    ("Private Terrace", r"(?:###|\|\s*\[)[^\n|]*private\s*terrace",   r"private\s*terrace"),
    ("Duplex",          r"(?:###|\|\s*\[)[^\n|]*duplex",              r"duplex"),
    ("Accessible",      r"(?:###|\|\s*\[)[^\n|]*(?:accessible|mobility)", r"accessible|mobility"),
)


# =============================================================================
# Room classifier — unchanged from prior version except adds is_ada flag
# =============================================================================
def classify_room(marketing_name: str, property_id: str) -> dict:
    name = (marketing_name or "").strip()
    nlo = name.lower()
    ada = bool(re.search(r"accessible|mobility|ada", nlo))

    if property_id != "aka_white_house":
        bedrooms = 0
        if re.search(r"\b(two|2)[\s\-]*(bedroom|br)\b", nlo): bedrooms = 2
        elif re.search(r"\b(one|1)[\s\-]*(bedroom|br)\b", nlo): bedrooms = 1
        tier = ("tier_5_penthouse" if ("presidential" in nlo or "penthouse" in nlo) else
                "tier_4_premier"   if "suite" in nlo else
                "tier_2_view"      if ("view" in nlo or "deluxe" in nlo) else
                "tier_1_entry")
        view = "view" if "view" in nlo else "non_view"
        return {"internal_code": f"CMP_{bedrooms}BR_{tier}", "tier": tier,
                "view": view, "bedrooms": bedrooms or None, "ada": ada,
                "mapping_source": "heuristic_comp", "penthouse_variant": None}

    is_penthouse = "penthouse" in nlo
    is_private_terrace = ("private terrace" in nlo or "terrace" in nlo) and not is_penthouse
    is_h_view = "h street" in nlo
    bedrooms = (2 if re.search(r"\b(two|2)[\s\-]*bedroom|2 bedrooms", nlo)
                else 1 if re.search(r"\b(one|1)[\s\-]*bedroom|1 bedroom", nlo)
                else None)

    if is_penthouse:
        # Keep "PH" stable so build_dashboard.ROOM_ORDER and downstream
        # PH-bucket aggregations stay intact; carry the 1BR/2BR split in
        # penthouse_variant for analyses that need the granularity.
        return {"internal_code": "PH", "tier": "tier_5_penthouse",
                "view": "penthouse", "bedrooms": bedrooms, "ada": ada,
                "mapping_source": "aka_deterministic",
                "penthouse_variant": f"{bedrooms}BR" if bedrooms else None}
    if is_private_terrace:
        return {"internal_code": f"PTR{bedrooms}" if bedrooms else "PTR",
                "tier": "tier_4_premier", "view": "terrace",
                "bedrooms": bedrooms, "ada": ada,
                "mapping_source": "aka_deterministic", "penthouse_variant": None}
    if is_h_view and bedrooms:
        return {"internal_code": f"{bedrooms}BDPRC",
                "tier": "tier_2_view" if bedrooms == 1 else "tier_3_suite",
                "view": "h_street", "bedrooms": bedrooms, "ada": ada,
                "mapping_source": "aka_deterministic", "penthouse_variant": None}
    if bedrooms:
        return {"internal_code": f"{bedrooms}BD",
                "tier": "tier_1_entry" if bedrooms == 1 else "tier_3_suite",
                "view": "non_view", "bedrooms": bedrooms, "ada": ada,
                "mapping_source": "aka_deterministic", "penthouse_variant": None}
    return {"internal_code": "UNMAPPED", "tier": "unknown",
            "view": "unknown", "bedrooms": None, "ada": ada,
            "mapping_source": "none", "penthouse_variant": None}


# =============================================================================
# BAR classification — see scraper_lib/normalize.py:pick_canonical_bar()
# (legacy classify_bar() removed 2026-04-26 — superseded by canonical pickers
# that select bare + non-ref + cheapest, vs the legacy max-priced-refundable
# pick which conflated bundled rates and washed out LOS discounts)
# =============================================================================


# =============================================================================
# Promo banner + weekly disclosure (E1 fix, 2026-04-26)
# =============================================================================
# The LLM extractor only captures $-prefixed price tokens. It misses banner
# text like "15% off" (Booking) and "Pay Now and Save up to 15pct" (Direct).
# The 2026-04-26 Chrome verification proved every channel runs banner-style
# discounts; without capturing them, channel-symmetry analyses are wrong.
# These regex passes scan the cell's markdown after extraction and populate
# promo_banner_text / promo_banner_pct / weekly_rate_disclosure at cell-level.
# (Per-room association is not attempted — markdown structure varies too
# much across channels. Cell-level is sufficient for the analyses we run.)

# Phase A2.4 #6 (2026-04-26): collect-all-then-rank. Each pattern is tagged
# with its kind. The matcher collects ALL matches across markdown, then:
#   1. Filters out VISA_MC_OFFER + MEMBER_GATED_OFFER (these are NOT the
#      property's posted promo — they're card-issuer / loyalty gates).
#   2. Picks the canonical promo by priority:
#        PAY_NOW_AND_SAVE > PERCENT_OFF_BANNER > WEEKLY_RATE_DISCLOSURE
#   3. Filtered offers go into the new schema column member_or_card_offer_text.
#
# This replaces v2.1's first-match-wins ordering, which had the failure
# mode where the generic "up to N pct off" pattern (intended to catch
# card-issuer copy) could fire BEFORE the more specific Pay-Now banner if
# the card-offer copy appeared earlier in markdown — even after pattern
# reordering, ordering-by-text-position is fragile.
#
# Each entry: (kind, regex, has_pct_capture).
PROMO_PATTERNS: tuple[tuple[str, str, bool], ...] = (
    # PAY_NOW_AND_SAVE — AKA Direct's primary banner
    ("PAY_NOW_AND_SAVE",
     r"\bpay\s+now\s+and\s+(?:save|enjoy\s+savings)\s+up\s+to\s+(\d{1,3})", True),
    # PAY_NOW_AND_SAVE — AKA LOS=7 variant ("Pay Now, Stay 7+ and Save!")
    ("PAY_NOW_AND_SAVE",
     r"\bpay\s+now,?\s+stay\s+\d+\+?\s+and\s+save", False),
    # PERCENT_OFF_BANNER — Booking-style "15% off" / Hotels.com strikethrough
    ("PERCENT_OFF_BANNER",
     r"\b(\d{1,3})\s*%\s*off\b", True),
    # PERCENT_OFF_BANNER — generic "save up to X percent"
    ("PERCENT_OFF_BANNER",
     r"\bsave\s+up\s+to\s+(\d{1,3})\s*p?c?t\b", True),
    # WEEKLY_RATE_DISCLOSURE — Booking weekly trigger (no pct)
    ("WEEKLY_RATE_DISCLOSURE",
     r"\breduced\s+weekly\s+rate\b", False),
    # VISA_MC_OFFER — explicit card-issuer context near a "% off" / "pct off"
    ("VISA_MC_OFFER",
     r"(?:visa|mastercard|\bmc\b|amex|american\s+express|world\s+of\s+hyatt)[^.\n]{0,120}up\s+to\s+(\d{1,3})\s*p?c?t",
     True),
    # VISA_MC_OFFER — explicit "best flexible rate" qualifier (card-issuer copy idiom)
    ("VISA_MC_OFFER",
     r"\bup\s+to\s+(\d{1,3})\s*p?c?t\s+off\s+best\s+flexible\s+rate\b", True),
    # MEMBER_GATED_OFFER — sign-in / member-only language
    ("MEMBER_GATED_OFFER",
     r"\bsign\s+in\s+to\s+(?:see|save|unlock|view)[^\n.]{0,120}", False),
    ("MEMBER_GATED_OFFER",
     r"\bmembers?\s+only\s+rate\b", False),
)

# Priority for canonical-promo selection (lower = higher priority).
PROMO_KIND_PRIORITY: dict[str, int] = {
    "PAY_NOW_AND_SAVE": 1,
    "PERCENT_OFF_BANNER": 2,
    "WEEKLY_RATE_DISCLOSURE": 3,
}
PROMO_KINDS_FILTERED_FROM_CANONICAL: tuple[str, ...] = (
    "VISA_MC_OFFER", "MEMBER_GATED_OFFER",
)

# Booking-style explicit weekly-rate disclosure copy
WEEKLY_RATE_DISCLOSURE_RE = re.compile(
    r"weekly\s*rate\s*-?\s*\$[\d,]+(?:\.\d{2})?[^\n]*"
    r"(?:reduced\s+weekly\s+rate[^\n]*)?",
    re.IGNORECASE,
)

# "Sign in" / "Member rate" gating signal (Booking Genius, etc.)
MEMBER_GATE_RE = re.compile(
    r"\bsign\s+in\s+to\s+(?:see|save|unlock|view)|members?\s+only\s+rate|"
    r"sign\s+in\s+for\s+(?:a\s+)?(?:member|genius|loyalty)\s+(?:price|rate|discount)",
    re.IGNORECASE,
)


def extract_promo_signals(markdown: str) -> dict:
    """Cell-level scan of markdown for promo banner / weekly / member-gate.

    Phase A2.4 #6: collect-all-then-rank. Returns:
      promo_banner_text / promo_banner_pct  — canonical posted promo
                                               (PAY_NOW_AND_SAVE > PERCENT_OFF_BANNER
                                               > WEEKLY_RATE_DISCLOSURE),
      weekly_rate_disclosure                — Booking weekly-rate paragraph,
      member_rate_gated                     — bool (Genius / sign-in gate),
      member_or_card_offer_text             — card-issuer / member-gated copy
                                               filtered out of canonical.
    """
    md = markdown or ""
    empty = {
        "promo_banner_text": None, "promo_banner_pct": None,
        "weekly_rate_disclosure": None, "member_rate_gated": None,
        "member_or_card_offer_text": None,
    }
    if not md:
        return empty

    # Collect every regex match across the markdown.
    candidates: list[tuple[str, str, Optional[float]]] = []
    for kind, pattern, has_pct in PROMO_PATTERNS:
        for m in re.finditer(pattern, md, re.IGNORECASE):
            text = md[m.start():m.end()].strip()
            pct: Optional[float] = None
            if has_pct:
                try:
                    pct = float(m.group(1))
                except (ValueError, IndexError):
                    pct = None
            candidates.append((kind, text, pct))

    # Canonical promo: pick highest-priority kind among non-filtered candidates.
    promo_text: Optional[str] = None
    promo_pct: Optional[float] = None
    canonical_pool = [c for c in candidates if c[0] in PROMO_KIND_PRIORITY]
    if canonical_pool:
        chosen = min(canonical_pool, key=lambda c: PROMO_KIND_PRIORITY[c[0]])
        promo_text = chosen[1]
        promo_pct = chosen[2]

    # Filtered (card-issuer / member-gated) offers go in their own column.
    member_or_card_text: Optional[str] = None
    for kind, text, _ in candidates:
        if kind in PROMO_KINDS_FILTERED_FROM_CANONICAL:
            member_or_card_text = text
            break

    weekly: Optional[str] = None
    m = WEEKLY_RATE_DISCLOSURE_RE.search(md)
    if m:
        weekly = md[m.start():m.end()].strip()

    member_gated = bool(MEMBER_GATE_RE.search(md))

    return {
        "promo_banner_text": promo_text,
        "promo_banner_pct": promo_pct,
        "weekly_rate_disclosure": weekly,
        "member_rate_gated": member_gated if member_gated else None,
        "member_or_card_offer_text": member_or_card_text,
    }


# =============================================================================
# Bundle inclusions parser — used as a post-extract backstop in case the LLM
# misses bundle_inclusions on a rate row. Pulls "+ X" / "Includes X" markers
# out of the RATE-PLAN LABEL ONLY (NEVER the room sub_description).
#
# v2.1 fix (2026-04-26 smoke #1): the prior version scanned sub_description
# too, which falsely tagged every plan with bundle='wifi' on AKA Direct
# (room descriptions mention "WiFi included"). That broke the BAR-bare
# check for the entire Direct channel.
#
# Each pattern requires an EXPLICIT inclusion anchor ("+", "includes",
# "incl.", "with") right before the keyword — no bare-keyword matches.
# =============================================================================

# (anchored_pattern, normalized_name). Each pattern requires an explicit
# inclusion marker to avoid matching descriptive copy that mentions the
# keyword incidentally.
_BUNDLE_ANCHOR = r"(?:\+|\bincludes?\b|\bincl\.?|\bwith)\s*(?:\d+\s+)?"
BUNDLE_KEYWORDS: tuple[tuple[str, str], ...] = (
    (rf"{_BUNDLE_ANCHOR}(?:parking|valet)\b",                 "parking"),
    (rf"{_BUNDLE_ANCHOR}(?:wi[\s-]?fi|internet)\b",           "wifi"),
    (rf"{_BUNDLE_ANCHOR}breakfast\b",                         "breakfast"),
    (rf"{_BUNDLE_ANCHOR}(?:resort|dining|f\s*&\s*b)\s+credit\b", "credit"),
    (rf"{_BUNDLE_ANCHOR}(?:transfer|airport\s+shuttle)\b",    "transfer"),
)


def parse_bundle_from_text(text: str) -> Optional[str]:
    """Extract a normalized comma-list of bundle inclusions from a rate-plan
    LABEL (e.g. 'Non-refundable + Parking', 'Includes 1 parking spot').

    DO NOT pass room sub_description / amenity blurbs — those mention WiFi
    and parking incidentally and produce false positives. Label only.

    Returns None if no inclusions found, else a sorted comma-string.
    """
    t = (text or "").lower()
    if not t:
        return None
    found: set[str] = set()
    for pattern, normalized in BUNDLE_KEYWORDS:
        if re.search(pattern, t):
            found.add(normalized)
    if not found:
        return None
    return ",".join(sorted(found))


# =============================================================================
# Hotels.com multi-scroll action plan (Phase A2.3, 2026-04-26)
# =============================================================================
# Mirrors the Cowork live-Chrome harness that paints every per-room rate tier:
# short post-navigate settle, then 20 scroll-down events with brief waits,
# then a final settle so XHR-driven rate-tier rows complete before extract.
#
# Firecrawl caps `waitFor + sum(wait actions)` at 60_000 ms. Initial attempt
# targets ~35s total page time (waitFor=8000 + ~27s of action waits). On the
# gate-4 retry path, switch to a more aggressive plan (~57s total) by
# lengthening per-scroll settles + final settle, NOT by bumping waitFor.
def _build_hotels_actions(retry: bool) -> list[dict]:
    # Phase A2.3 best-known config (2026-04-26 fix-pass): 20 scrolls × 800ms
    # rendered the full ladder *once* but was non-deterministic on repeat.
    # Slower cadences (12 × 1800ms, 8 × 1500ms) never landed it, so the
    # answer wasn't "longer settle".
    #
    # Phase A2.5b (2026-04-27): the dry-run parser found per-room
    # cancellation blocks rendered in 5 of 33 saved cells. The other 28 cells
    # never reached the lower room cards' viewport-observer trigger before
    # extract. Fix is a re-traverse pass: scroll down through every room,
    # bump back up partway, then scroll down again. This re-triggers the
    # IntersectionObserver-driven XHR for lower rooms that didn't catch on
    # the first pass.
    #
    # Two Firecrawl caps to respect:
    #   1. waitFor + sum(wait actions) <= 60_000ms  (waitFor=8000ms for hotels_com)
    #   2. len(actions) <= 50  (each scroll + wait pair counts as 2 actions)
    # Cap (2) is the tighter constraint here — a paired scroll+wait at every
    # step bounds total scrolls at ~22. We use a 15+3+5 down/up/down split so
    # 23 scrolls fit in 49 total actions with the initial / mid-wave / final
    # waits.
    if retry:
        n_down_first = 15
        per_scroll_wait_ms = 1500
        n_up = 3
        up_wait_ms = 1000
        n_down_second = 5
        down2_wait_ms = 1500
        final_settle_ms = 6000
        mid_wave_idx = 7
    else:
        n_down_first = 15
        per_scroll_wait_ms = 1100
        n_up = 3
        up_wait_ms = 700
        n_down_second = 5
        down2_wait_ms = 1100
        final_settle_ms = 6000
        mid_wave_idx = 7

    actions: list[dict] = [{"type": "wait", "milliseconds": 3000}]
    # Wave 1: scroll all the way down so every room enters viewport at least once.
    for i in range(n_down_first):
        actions.append({"type": "scroll", "direction": "down"})
        actions.append({"type": "wait", "milliseconds": per_scroll_wait_ms})
        if i == mid_wave_idx:
            actions.append({"type": "wait", "milliseconds": 4000})
    # Re-traverse: bump back up partway so the lower rooms' IntersectionObserver
    # can fire again on re-entry. Empirically, lazy-loaded radio-button rows
    # often render only on second viewport intersection.
    for _ in range(n_up):
        actions.append({"type": "scroll", "direction": "up"})
        actions.append({"type": "wait", "milliseconds": up_wait_ms})
    # Wave 2: scroll down through the lower rooms again.
    for _ in range(n_down_second):
        actions.append({"type": "scroll", "direction": "down"})
        actions.append({"type": "wait", "milliseconds": down2_wait_ms})
    actions.append({"type": "wait", "milliseconds": final_settle_ms})
    return actions


# =============================================================================
# Phase A2.4 #1 — AKA canonical-room coverage check
# =============================================================================
# AKA-only fail-closed gate: every extracted marketing_name must resolve
# through canonical_maps.lookup_canonical_room(). An unmapped string is
# either a hallucination (the v1 scraper produced "Two Bedroom Duplex
# Penthouse" against an inventory that has no such SKU) or a real-but-new
# SKU that needs a human to add to ROOM_TYPE_CANONICAL. Either way the
# cell must FAIL CLOSED — no partial-cell row landing in raw_rates.csv,
# and the cell must be re-queued by the resume logic.
def find_unmapped_aka_marketing_names(extracted: dict) -> list[str]:
    """Return the unique list of marketing_name strings that did NOT resolve
    through lookup_canonical_room(). Caller is responsible for passing
    only AKA cells — comp cells legitimately don't canonicalize in v2.
    Empty marketing names are ignored (room-type anchor gate handles those).
    """
    if not isinstance(extracted, dict):
        return []
    seen: list[str] = []
    seen_set: set[str] = set()
    for room in (extracted.get("rooms") or []):
        if not isinstance(room, dict):
            continue
        name = (room.get("marketing_name") or "").strip()
        if not name:
            continue
        if lookup_canonical_room(name) is not None:
            continue
        if name in seen_set:
            continue
        seen.append(name)
        seen_set.add(name)
    return seen


# =============================================================================
# Phase A2.4 #7 — Hotels.com retry gate (room-count + per-room expansion)
# =============================================================================
def should_trigger_hotels_com_retry(
    *, channel: str, n_rooms: int, max_plans_per_room: int,
    min_plans_per_room: int = 0, expected_room_count: int = 0,
) -> tuple[bool, str]:
    """Decide whether to flip to the longer-settle Hotels.com action plan.

    Two conditions, either of which fires the retry:
      (1) max_plans_per_room < 2  — no room expanded its rate ladder at all
                                    (legacy gate, Phase A2.3).
      (2) n_rooms < expected_room_count AND tier rendering looks thin —
          historical max-room count expects 6 SKUs, but a given date may
          legitimately have fewer available. We retry only when fewer rooms
          AND the existing rooms also have thin tier expansion (max < 4 OR
          min < 2). When every rendered room has full tiers (min ≥ 2 and
          max ≥ 4), the cell is in good shape and the retry path empirically
          BLOCKS more than it helps (Phase A2.5b live smoke 2026-04-27 saw
          the retry overwrite a 5-room/4-tier extract with a 0-room
          bot-blocked thin response).

    Returns (trigger, reason). Caller guards on `not hotels_retry` and
    `ic_attempt < EXTRACT_INCOMPLETE_MAX_ATTEMPTS - 1`.
    """
    if channel != "hotels_com":
        return False, ""
    reasons: list[str] = []
    if max_plans_per_room < 2:
        reasons.append(f"max={max_plans_per_room} plans across {n_rooms} rooms")
    if expected_room_count > 0 and n_rooms < expected_room_count:
        # Phase A2.5b: only retry on room-count short when at least one
        # rendered room is itself thin (min < 2). Two distinct reasons we
        # may see n_rooms < expected:
        #   (a) Real-availability gap — Hotels.com legitimately shows fewer
        #       SKUs on this date (e.g. 5 of 6 because Penthouse 2BR is sold
        #       out). Every rendered room has its full per-page tier set
        #       (min >= 2 = standard + non-ref-alternative). Retrying can't
        #       produce rooms that aren't bookable; it only risks bot-block.
        #   (b) Render-incomplete — the page DID have more rooms but they
        #       didn't paint, OR some rooms got 0 plans because their
        #       cancellation block didn't render. min < 2 catches both.
        # The 2026-04-27 live smoke proved (a) cost real data: a 5-room/2-tier
        # extract was correctly produced on attempt 1, then the retry hit
        # Hotels.com bot-block and the THIN response overwrote it. Gate
        # only on (b).
        if min_plans_per_room < 2:
            reasons.append(
                f"only {n_rooms} of expected {expected_room_count} rooms rendered"
                f" (some thin: min={min_plans_per_room})"
            )
    if not reasons:
        return False, ""
    return True, "; ".join(reasons)


# Phase A2.4b #2 — room-count fail-closed gate. Mirrors the gate-4 retry
# (#7) trigger but runs AFTER the inner retry loop has exhausted, so it
# operates on the final extracted dict rather than the intermediate one.
# When the cell still returned fewer rooms than the channel's expected
# count, the caller writes a single FAIL_ROOM_COUNT_SHORT sentinel and
# persists NO plan rows; resume then re-queues the cell via the sentinel
# gate in load_completed_cells.
_HOTELS_COM_INVENTORY_DECLARATION_RE = re.compile(
    r"Showing\s+(\d+)\s+of\s+(\d+)\s+rooms?", re.IGNORECASE,
)


def _parse_page_declared_inventory(markdown: str) -> Optional[int]:
    """Hotels.com renders 'Showing X of Y rooms' to declare the day's full
    inventory. When present, Y is the page's authoritative count for THIS
    check-in date — it's typically <= the canonical-max EXPECTED_ROOM_COUNT
    because some SKUs are sold out. Returns Y if found, else None.
    """
    if not markdown:
        return None
    m = _HOTELS_COM_INVENTORY_DECLARATION_RE.search(markdown)
    if not m:
        return None
    try:
        return int(m.group(2))
    except ValueError:
        return None


def detect_room_count_short(
    extracted, *, channel: str, markdown: str = "", property_id: str = "",
) -> tuple[bool, int, int]:
    """Returns (is_short, n_rooms_extracted, effective_expected_n).

    `expected_n` from EXPECTED_ROOM_COUNT_BY_CHANNEL is the canonical-max
    SKU count for the channel. Phase A2.5b adds a markdown-derived override:
    if Hotels.com itself rendered 'Showing X of Y rooms', that Y is the
    page's declared inventory for this check-in date — typically smaller
    than the canonical max because some SKUs are sold out. When extracted
    n_rooms == declared Y, the cell is NOT short, even if Y < canonical.

    Falls back to the configured EXPECTED_ROOM_COUNT_BY_CHANNEL when the
    markdown declaration is absent (other channels, or older/different
    Hotels.com renders).

    When `expected_n` is 0 the gate is disabled.

    Subject-only: EXPECTED_ROOM_COUNT_BY_CHANNEL is calibrated against the
    subject property's SKU inventory (per config.json
    expected_room_count_by_channel). Comp properties have different counts,
    so the gate is disabled for non-subject properties to avoid false
    FAIL_ROOM_COUNT_SHORT and infinite re-queue. SFOEM expected counts:
    direct=18, booking=12, hotels_com=17, expedia=18.
    """
    if property_id != "hr_embarcadero":
        n_rooms = sum(1 for r in (extracted.get("rooms") or [])
                      if isinstance(r, dict)) if isinstance(extracted, dict) else 0
        return False, n_rooms, 0
    expected_n = EXPECTED_ROOM_COUNT_BY_CHANNEL.get(channel, 0)
    if not isinstance(extracted, dict) or expected_n <= 0:
        return False, 0, expected_n
    rooms_list = extracted.get("rooms") or []
    n_rooms = sum(1 for r in rooms_list if isinstance(r, dict))
    # Phase A2.5b: prefer the page's declared inventory when present.
    declared = _parse_page_declared_inventory(markdown) if channel == "hotels_com" else None
    effective_expected = declared if declared is not None else expected_n
    return (n_rooms < effective_expected), n_rooms, effective_expected


# =============================================================================
# Variance check
# =============================================================================
def check_variance(markdown: str, rooms: list[dict], property_id: str) -> list[str]:
    """For AKA: return pattern-labels whose heading appears in markdown
    but whose term is absent from any extracted marketing_name."""
    if property_id != "aka_white_house":
        return []
    md_lower = (markdown or "").lower()
    rooms_concat = " ".join((r.get("marketing_name") or "") for r in (rooms or [])).lower()
    missed = []
    for label, md_pattern, rooms_pattern in VARIANCE_PATTERNS:
        if re.search(md_pattern, md_lower) and not re.search(rooms_pattern, rooms_concat):
            missed.append(label)
    return missed


# =============================================================================
# Credit/call log
# =============================================================================
def log_event(event: dict) -> None:
    event = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), **event}
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


# =============================================================================
# URL builder
# =============================================================================
def build_url(property_id: str, channel_id: str, arrival: str, los: int) -> tuple[str, dict]:
    arrive_d = datetime.date.fromisoformat(arrival)
    depart_d = arrive_d + datetime.timedelta(days=los)
    prop = next(p for p in CONFIG["properties"] if p["id"] == property_id)
    channel = next(c for c in CONFIG["channels"] if c["id"] == channel_id)
    key = f"{channel_id}_url_template"
    if channel_id == "direct":
        key = "direct_url_template"
    template = prop.get(key, "")
    if not template:
        raise ValueError(f"no URL template for property={property_id} channel={channel_id} "
                         f"(missing config key '{key}')")
    # Support both ISO dates ({arrive}/{depart}) and split day/MMYYYY tokens for IHG-style engines
    fmt = {
        "arrive": arrive_d.isoformat(),
        "depart": depart_d.isoformat(),
        "arrive_day": arrive_d.strftime("%d"),
        "depart_day": depart_d.strftime("%d"),
        "arrive_mmyyyy": arrive_d.strftime("%m%Y"),
        "depart_mmyyyy": depart_d.strftime("%m%Y"),
        "arrive_mdy": arrive_d.strftime("%m/%d/%Y"),
        "depart_mdy": depart_d.strftime("%m/%d/%Y"),
    }
    url = template.format(**fmt)
    return url, channel


# =============================================================================
# Firecrawl wrapper with HTTP-level retry on 5xx only
# =============================================================================
def firecrawl_scrape_json(
    url: str, *, stealth: bool, wait_for_ms: int, schema: dict, prompt: str,
    actions: Optional[list[dict]] = None, timeout_ms: int = 0, http_retries: int = 2,
) -> dict:
    if not KEY:
        raise RuntimeError("FIRECRAWL_API_KEY missing from .env")
    payload = {
        "url": url, "formats": ["markdown", "json"],
        "jsonOptions": {"schema": schema, "prompt": prompt},
        "waitFor": wait_for_ms,
    }
    if stealth:
        payload["proxy"] = "stealth"
    if actions:
        # Firecrawl actions run after page load, in order. Used by Hotels.com
        # to scroll-to-bottom so lazy-loaded room cards render before extract.
        payload["actions"] = actions
    # Firecrawl rejects requests where waitFor > timeout/2 (default timeout
    # is ~30s, so bare waitFor > 15s is a 4xx). v2.1 (2026-04-26 smoke #1):
    # pair them implicitly here so callers can't bump waitFor without bumping
    # timeout. Precedence: CLI override > explicit timeout_ms arg > derived
    # 2 × waitFor when waitFor > 15s.
    if _OVERRIDE_FIRECRAWL_TIMEOUT_MS > 0:
        payload["timeout"] = _OVERRIDE_FIRECRAWL_TIMEOUT_MS
    elif timeout_ms > 0:
        payload["timeout"] = timeout_ms
    elif wait_for_ms > 15000:
        payload["timeout"] = 2 * wait_for_ms
    last_err = ""
    for attempt in range(http_retries + 1):
        try:
            r = requests.post("https://api.firecrawl.dev/v1/scrape", headers=HDR, json=payload, timeout=280)
            if r.status_code >= 500 and attempt < http_retries:
                time.sleep(3 + attempt * 3)
                last_err = f"http {r.status_code}"
                continue
            return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"success": False, "error": r.text[:2000]}
        except Exception as ex:
            last_err = f"exc: {ex}"
            if attempt < http_retries:
                time.sleep(5)
                continue
            return {"success": False, "error": last_err}
    return {"success": False, "error": last_err}


# =============================================================================
# Per-cell scraper — top-level logic with retry orchestration
# =============================================================================
@dataclass
class CellResult:
    property_id: str
    channel: str
    arrival: str
    los: int
    url: str
    status: str      # 'ok' | 'bot_blocked' | 'extract_failed' | 'extract_incomplete' | 'http_error' | 'no_url' |
                     # 'FAIL_UNKNOWN_ROOM_TYPE' | 'FAIL_ROOM_COUNT_SHORT' | 'FAIL_CHAIN_PAGE_SOFT' |
                     # 'FAIL_URL_BROKEN' | 'no_inventory' (cell-level verdicts)
    reason: str
    rows: list[dict] = field(default_factory=list)
    markdown_len: int = 0
    elapsed_s: float = 0.0
    credits_nominal: int = 0
    attempts: int = 0


def _flatten_rooms_to_rows(
    extracted: dict, property_id: str, channel_id: str, arrival: str, los: int,
    source_url: str, missed_patterns: list[str], *, markdown: str = "",
) -> list[dict]:
    """Flatten rooms[] × rate_plans[] into one row per (room, plan). Tag BAR per room.
    v2: also populates promo banner / weekly disclosure / bundle / strikethrough columns.
    """
    rows: list[dict] = []
    ts_utc = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    # v2: cell-level promo signals (same banner copy applies to every row in cell).
    # Per-room banner association is not attempted — markdown structure is too
    # variable across channels to infer reliably.
    promo_signals = extract_promo_signals(markdown)

    for room_idx, room in enumerate(extracted.get("rooms") or []):
        name = room.get("marketing_name") or ""
        cls = classify_room(name, property_id)
        # Canonical room lookup (v2). None for subject cells with unmapped strings
        # gets surfaced as FAIL_UNKNOWN_ROOM_TYPE downstream; comp cells stay null
        # because canonicalization is subject-only in v2 (SFOEM = hr_embarcadero).
        room_canonical = lookup_canonical_room(name) if property_id == "hr_embarcadero" else None
        plans = room.get("rate_plans") or []
        # v2.2 (2026-04-26 smoke #2 fix): derive refundable + bundle deterministically
        # in-place BEFORE the picker sees the plans. The LLM nondeterministically
        # tags amenity WiFi as a bundle and inverts refundability when labels are
        # ambiguous; both corrupt BAR-bare picking. Mutate plans directly so all
        # downstream code (picker + row writer) sees the same canonical values.
        for p in plans:
            if not isinstance(p, dict):
                continue
            phrase = (p.get("cancellation_phrase") or "").lower()
            if any(t in phrase for t in ("non-refund", "non refund", "nonrefund")):
                p["refundable"] = False
            elif any(t in phrase for t in ("free cancellation", "fully refundable", "refundable")):
                p["refundable"] = True
            # else: keep LLM-emitted refundable as fallback when phrase ambiguous/missing
            # Bundle: derived from LABEL only via the anchored parser. Ignore any
            # LLM-emitted bundle_inclusions (proven unreliable in smoke #1 + #2).
            p["bundle_inclusions"] = parse_bundle_from_text(p.get("rate_plan_label") or "")
        # Canonical pickers operate on the deterministically-cleaned plans
        bar_pick = pick_canonical_bar(plans)
        flex_pick = pick_canonical_flex(plans)
        bar_idx = bar_pick[0] if bar_pick else None
        flex_idx = flex_pick[0] if flex_pick else None
        for i, p in enumerate(plans):
            rate = p.get("rate_per_night_usd")
            label = p.get("rate_plan_label") or ""
            is_bar = (i == bar_idx)
            # status semantics: 'ok' for BAR rows, 'non_bar_rate' for non-BAR
            row_status = "ok" if is_bar else "non_bar_rate"
            if missed_patterns:
                # non-fatal note — propagate to every row
                row_status = "extract_incomplete"

            # v2.2: bundle is already deterministically derived above (label-only,
            # anchored). Just read the post-processed value off the mutated plan dict.
            bundle = p.get("bundle_inclusions")

            # v2: strikethrough — populate from extractor + derive pct
            strike_orig = p.get("strikethrough_orig_rate")
            strike_pct = None
            if isinstance(strike_orig, (int, float)) and not isinstance(strike_orig, bool) \
                    and isinstance(rate, (int, float)) and not isinstance(rate, bool) \
                    and strike_orig > 0 and rate > 0 and strike_orig > rate:
                strike_pct = round((strike_orig - rate) / strike_orig, 4)

            row = {
                "property_id": property_id,
                "channel": channel_id,
                "arrival_date": arrival,
                "nights": los,
                "scraped_property_name": extracted.get("property_name") or "",
                "scraped_marketing_name": name,
                "sub_description": room.get("sub_description") or "",
                "bed_config": room.get("bed_config") or "",
                "occupancy_max": room.get("occupancy_max"),
                "rate_plan_label": label,
                "rate_per_night_usd": rate,
                "total_stay_usd": p.get("total_stay_usd"),
                "refundable": p.get("refundable"),
                "is_genius_member_rate": p.get("is_genius_member_rate"),
                "includes_breakfast": p.get("includes_breakfast"),
                "rate_plan_confidence": p.get("rate_plan_confidence") or "",
                "availability_status": p.get("availability_status") or "unknown",
                "is_bar": is_bar,
                "mapped_internal_code": cls["internal_code"],
                "mapped_tier": cls["tier"],
                "mapped_view": cls["view"],
                "mapped_bedrooms": cls["bedrooms"],
                "mapped_ada": cls["ada"],
                "mapping_source": cls["mapping_source"],
                "status": row_status,
                "missed_patterns": ",".join(missed_patterns),
                "scrape_timestamp_utc": ts_utc,
                "source_url": source_url,
            }
            # v2 columns
            row.update(NEW_COLUMN_DEFAULTS)
            row["room_type_canonical"] = room_canonical
            row["penthouse_variant"] = cls["penthouse_variant"]
            # rate_plan_canonical: tag every row by bucket; the chosen
            # BAR_NON_REF / BAR_FLEX rows inherit BAR_NON_REF / BAR_FLEX from
            # the picker, others get classify_rate_plan(). Member/card-offers
            # get tagged MEMBER_OR_CARD_OFFER even if they happen to be the
            # cheapest bare/refundable, since those aren't a posted BAR.
            if i == bar_idx:
                row["rate_plan_canonical"] = "BAR_NON_REF"
            elif i == flex_idx:
                row["rate_plan_canonical"] = "BAR_FLEX"
            else:
                row["rate_plan_canonical"] = classify_rate_plan(p)
            row["strikethrough_orig_rate"] = strike_orig if isinstance(strike_orig, (int, float)) and not isinstance(strike_orig, bool) else None
            row["strikethrough_pct"] = strike_pct
            row["promo_banner_text"] = promo_signals["promo_banner_text"]
            row["promo_banner_pct"] = promo_signals["promo_banner_pct"]
            row["weekly_rate_disclosure"] = promo_signals["weekly_rate_disclosure"]
            row["member_rate_gated"] = promo_signals["member_rate_gated"]
            row["member_or_card_offer_text"] = promo_signals.get("member_or_card_offer_text")
            row["bundle_inclusions"] = bundle
            rows.append(row)

        # Phase A2.4 #2 — sentinel rows when canonical pickers found nothing.
        # Distinguishes "we tried, no bare non-ref/flex exists" from "we
        # never tried." Resume logic uses row-status: a sentinel-only cell
        # has no ok/extract_incomplete/non_bar_rate row → cell is re-queued.
        # Cells where SOME rooms have a canonical pick (and others don't)
        # still mark as done via the picked-room rows; the sentinel surfaces
        # the per-room gap for analysis.
        if bar_pick is None:
            rows.append(_make_sentinel_row(
                property_id=property_id, channel_id=channel_id, arrival=arrival,
                los=los, source_url=source_url, ts_utc=ts_utc,
                extracted=extracted, room=room, room_canonical=room_canonical,
                cls=cls, missed_patterns=missed_patterns,
                promo_signals=promo_signals,
                kind="FAIL_NO_BARE_NON_REF",
            ))
        if flex_pick is None:
            rows.append(_make_sentinel_row(
                property_id=property_id, channel_id=channel_id, arrival=arrival,
                los=los, source_url=source_url, ts_utc=ts_utc,
                extracted=extracted, room=room, room_canonical=room_canonical,
                cls=cls, missed_patterns=missed_patterns,
                promo_signals=promo_signals,
                kind="FAIL_NO_BAR_FLEX",
            ))
    return rows


# Phase A2.4b #1 / #2 — sentinel row statuses. A cell that has any of these
# rows is NOT considered complete by load_completed_cells, even if it also
# has ok / extract_incomplete / non_bar_rate rows. The presence of a
# sentinel means at least one room failed picker (#1) or the channel-level
# room count came up short on the gate-4 retry (#2), and the cell should
# be re-queued so the next run can try to recover the missing data.
SENTINEL_STATUSES: tuple[str, ...] = (
    "FAIL_NO_BARE_NON_REF",
    "FAIL_NO_BAR_FLEX",
    "FAIL_ROOM_COUNT_SHORT",
)


def _make_sentinel_row(*, property_id: str, channel_id: str, arrival: str,
                       los: int, source_url: str, ts_utc: str,
                       extracted: dict, room: dict, room_canonical: Optional[str],
                       cls: dict, missed_patterns: list[str],
                       promo_signals: dict, kind: str) -> dict:
    """Build a sentinel row for a room where pick_canonical_bar/flex returned
    None. Captures the room identity + cell context but no rate data.

    `kind` is the row's status AND rate_plan_canonical — one of
    FAIL_NO_BARE_NON_REF / FAIL_NO_BAR_FLEX. The status is in
    SENTINEL_STATUSES, so load_completed_cells treats any cell containing
    such a row as NOT complete and re-queues it on resume.
    """
    row: dict = {
        "property_id": property_id,
        "channel": channel_id,
        "arrival_date": arrival,
        "nights": los,
        "scraped_property_name": extracted.get("property_name") or "",
        "scraped_marketing_name": room.get("marketing_name") or "",
        "sub_description": room.get("sub_description") or "",
        "bed_config": room.get("bed_config") or "",
        "occupancy_max": room.get("occupancy_max"),
        "rate_plan_label": "",
        "rate_per_night_usd": None,
        "total_stay_usd": None,
        "refundable": None,
        "is_genius_member_rate": None,
        "includes_breakfast": None,
        "rate_plan_confidence": "",
        "availability_status": "unknown",
        "is_bar": False,
        "mapped_internal_code": cls["internal_code"],
        "mapped_tier": cls["tier"],
        "mapped_view": cls["view"],
        "mapped_bedrooms": cls["bedrooms"],
        "mapped_ada": cls["ada"],
        "mapping_source": cls["mapping_source"],
        "status": kind,
        "missed_patterns": ",".join(missed_patterns),
        "scrape_timestamp_utc": ts_utc,
        "source_url": source_url,
    }
    row.update(NEW_COLUMN_DEFAULTS)
    row["room_type_canonical"] = room_canonical
    row["rate_plan_canonical"] = kind
    row["penthouse_variant"] = cls["penthouse_variant"]
    row["promo_banner_text"] = promo_signals["promo_banner_text"]
    row["promo_banner_pct"] = promo_signals["promo_banner_pct"]
    row["weekly_rate_disclosure"] = promo_signals["weekly_rate_disclosure"]
    row["member_rate_gated"] = promo_signals["member_rate_gated"]
    row["member_or_card_offer_text"] = promo_signals.get("member_or_card_offer_text")
    return row


def _make_room_count_short_sentinel(
    *, property_id: str, channel_id: str, arrival: str, los: int,
    source_url: str, markdown: str, n_rooms: int, expected_n: int,
) -> dict:
    """Build a single cell-level FAIL_ROOM_COUNT_SHORT sentinel row.

    Unlike the per-room sentinels from _flatten_rooms_to_rows, this fires
    on a cell-wide condition (the channel's lazy-load returned fewer
    rooms than expected after retry exhaustion), so it carries no room
    identity. The row's status is in SENTINEL_STATUSES, so resume's
    load_completed_cells will re-queue the cell.
    """
    ts_utc = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    promo_signals = extract_promo_signals(markdown)
    row: dict = {
        "property_id": property_id,
        "channel": channel_id,
        "arrival_date": arrival,
        "nights": los,
        "scraped_property_name": "",
        "scraped_marketing_name": "",
        "sub_description": "",
        "bed_config": "",
        "occupancy_max": None,
        "rate_plan_label": "",
        "rate_per_night_usd": None,
        "total_stay_usd": None,
        "refundable": None,
        "is_genius_member_rate": None,
        "includes_breakfast": None,
        "rate_plan_confidence": "",
        "availability_status": "unknown",
        "is_bar": False,
        "mapped_internal_code": "",
        "mapped_tier": "",
        "mapped_view": "",
        "mapped_bedrooms": None,
        "mapped_ada": None,
        "mapping_source": "",
        "status": "FAIL_ROOM_COUNT_SHORT",
        "missed_patterns": f"only {n_rooms}/{expected_n} rooms rendered",
        "scrape_timestamp_utc": ts_utc,
        "source_url": source_url,
    }
    row.update(NEW_COLUMN_DEFAULTS)
    row["rate_plan_canonical"] = "FAIL_ROOM_COUNT_SHORT"
    row["promo_banner_text"] = promo_signals["promo_banner_text"]
    row["promo_banner_pct"] = promo_signals["promo_banner_pct"]
    row["weekly_rate_disclosure"] = promo_signals["weekly_rate_disclosure"]
    row["member_rate_gated"] = promo_signals["member_rate_gated"]
    row["member_or_card_offer_text"] = promo_signals.get("member_or_card_offer_text")
    return row


def _make_cell_status_sentinel(
    *, property_id: str, channel_id: str, arrival: str, los: int,
    source_url: str, markdown: str, status: str, reason: str,
) -> dict:
    """Build a cell-level sentinel for a completed cell with no rate rows.

    Used for legitimate no-inventory pages. Broken URLs intentionally do not
    get this sentinel so resume keeps re-queuing them for operator attention.
    """
    ts_utc = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    promo_signals = extract_promo_signals(markdown)
    row: dict = {
        "property_id": property_id,
        "channel": channel_id,
        "arrival_date": arrival,
        "nights": los,
        "scraped_property_name": "",
        "scraped_marketing_name": "",
        "sub_description": "",
        "bed_config": "",
        "occupancy_max": None,
        "rate_plan_label": "",
        "rate_per_night_usd": None,
        "total_stay_usd": None,
        "refundable": None,
        "is_genius_member_rate": None,
        "includes_breakfast": None,
        "rate_plan_confidence": "",
        "availability_status": status,
        "is_bar": False,
        "mapped_internal_code": "",
        "mapped_tier": "",
        "mapped_view": "",
        "mapped_bedrooms": None,
        "mapped_ada": None,
        "mapping_source": "",
        "status": status,
        "missed_patterns": reason,
        "scrape_timestamp_utc": ts_utc,
        "source_url": source_url,
    }
    row.update(NEW_COLUMN_DEFAULTS)
    row["rate_plan_canonical"] = "NO_INVENTORY" if status == "no_inventory" else status
    row["promo_banner_text"] = promo_signals["promo_banner_text"]
    row["promo_banner_pct"] = promo_signals["promo_banner_pct"]
    row["weekly_rate_disclosure"] = promo_signals["weekly_rate_disclosure"]
    row["member_rate_gated"] = promo_signals["member_rate_gated"]
    row["member_or_card_offer_text"] = promo_signals.get("member_or_card_offer_text")
    return row


def summarize_picker_failures(rows: list[dict]) -> list[dict]:
    """Return per-room picker-failure summary derived from the rows produced
    by _flatten_rooms_to_rows. Used by scrape_cell to surface cell-level
    diagnostics into log_event (Phase A2.4 #2 verification step (c)).
    """
    out: list[dict] = []
    for r in rows or []:
        status = (r.get("status") or "")
        if status in ("FAIL_NO_BARE_NON_REF", "FAIL_NO_BAR_FLEX"):
            out.append({
                "kind": status,
                "marketing_name": r.get("scraped_marketing_name", ""),
                "room_canonical": r.get("room_type_canonical"),
            })
    return out


def scrape_cell(
    property_id: str, channel_id: str, arrival: str, los: int, *, verbose: bool = True,
) -> CellResult:
    try:
        url, channel = build_url(property_id, channel_id, arrival, los)
    except Exception as ex:
        return CellResult(property_id, channel_id, arrival, los, "", "no_url", str(ex))

    # Per-property stealth override (Akamai-protected sites need stealth even on "direct")
    prop_ref = next(p for p in CONFIG["properties"] if p["id"] == property_id)
    stealth = bool(channel.get("stealth"))
    if channel_id == "direct" and prop_ref.get("direct_stealth"):
        stealth = True

    # Channel-specific waitFor defaults. Phase A2.3 (2026-04-26): Hotels.com
    # waitFor reduced 45000→8000 to fit Firecrawl's hard cap of
    # `waitFor + sum(wait actions) <= 60s`. The multi-scroll action sequence
    # (see _build_hotels_actions) does the heavy lifting that the long waitFor
    # was previously meant to handle. 8s matches the Cowork live-Chrome
    # harness's post-navigate wait that successfully painted all rate tiers.
    if channel_id == "hotels_com":
        wait_for = 8000
    else:
        wait_for = int(channel.get("wait_for_ms") or 6000)

    # Hotels.com: 20 scroll-downs in two waves drive the per-room rate-tier
    # XHR expander (see _build_hotels_actions doc). On the gate-4 retry path
    # below, hotels_retry switches to the longer-settle variant.
    actions: Optional[list[dict]] = None
    hotels_com_timeout_ms = 0
    hotels_retry = False
    if channel_id == "hotels_com":
        actions = _build_hotels_actions(retry=False)
        # Pin 240s timeout — Phase A2.5b live smoke 2026-04-27 timed out at
        # 180s. Firecrawl emits a "stealthProxy not supported with actions"
        # warning and silently routes to a slower non-stealth engine; total
        # call latency (~120-180s) is dominated by that engine, not by our
        # action plan's wait math (~57s page time). 240s gives headroom for
        # extract LLM + retry-friendly Firecrawl edge cases.
        hotels_com_timeout_ms = 240000

    # CLI override for direct-channel waitFor (used by retry pass: --direct-wait-for-override 20000)
    if channel_id == "direct" and _OVERRIDE_DIRECT_WAIT_FOR_MS > 0:
        wait_for = _OVERRIDE_DIRECT_WAIT_FOR_MS
    # Global floor applied to every channel (used when OTAs lazy-load rates: --min-wait-for-ms 20000)
    if _OVERRIDE_MIN_WAIT_FOR_MS > 0 and wait_for < _OVERRIDE_MIN_WAIT_FOR_MS:
        wait_for = _OVERRIDE_MIN_WAIT_FOR_MS

    # Note: firecrawl_scrape_json auto-pairs the Firecrawl `timeout` to
    # 2 × wait_for when wait_for > 15s. The Hotels.com lazy-load retry
    # path can re-bump wait_for to 60s inside the inner loop; the auto-
    # pairing in firecrawl_scrape_json handles that without re-computing
    # here.

    # Per-property tokens for plausibility
    prop = next(p for p in CONFIG["properties"] if p["id"] == property_id)
    tokens = prop.get("expected_property_tokens") or [prop["name"].lower().split()[-1]]

    safe = f"{property_id}__{channel_id}__{arrival}__LOS{los}".replace("/", "_")
    t_start = time.time()
    total_credits = 0
    attempts = 0
    last_status = "ok"
    last_reason = ""
    md = ""
    extracted: Optional[dict] = None
    missed_patterns: list[str] = []
    # Phase A2.5b — populated by expand_tiers_from_markdown inside the
    # inner loop on Hotels.com cells; surfaced at log_event time below.
    hotels_tier_diagnostics: Optional[dict] = None

    # Phase A2.5 (2026-04-27) — Hotels.com routing flag. Vision path is
    # WIRED OPT-IN (default false). Live smoke 2026-04-27 found Hotels.com
    # add-on tiers are behind a click-to-expand widget, not statically
    # rendered — so the screenshot has no more data than markdown does;
    # see channel_quirks.md A2.5 entry. Vision path will activate once
    # A2.5b lands (Firecrawl click actions on each rate-tier expander).
    use_vision_path = (channel_id == "hotels_com"
                       and bool(CONFIG.get("use_vision_for_hotels_com", False)))

    # 2026-04-28 — Synxis API direct path. When the property has a `synxis`
    # config block (booking_url + hotel_id + chain_id) and the global
    # `use_synxis_api_for_direct` flag is on (default true), bypass
    # Firecrawl on direct cells and hit the Synxis CRS gateway directly
    # via fetch_synxis_direct. The function returns the same {success,
    # data: {markdown, json}} envelope as firecrawl_scrape_json, but
    # `markdown` is always "" — so the markdown-anchored gates below
    # (chain-page sentinel, bot-block, room-type anchor, variance, BAR
    # anchor) are SKIPPED for this path: data comes from a structured
    # Sabre API response, no LLM extraction, no hallucination surface to
    # defend, and nothing to anchor against. Plausibility (gate 2) is
    # also skipped because its core anti-hallucination Rule 7
    # (rate-anchor) requires markdown; structured-API + numeric-type
    # guarantees at parse time provide equivalent assurance.
    synxis_cfg = prop_ref.get("synxis")
    use_synxis_path = (
        channel_id == "direct"
        and bool(synxis_cfg)
        and bool(CONFIG.get("use_synxis_api_for_direct", True))
    )

    # ---- Bot-block retry outer loop ----
    for bb_attempt in range(BOT_BLOCK_MAX_ATTEMPTS):
        attempts += 1
        prompt_to_use = EXTRACT_PROMPT
        for ic_attempt in range(EXTRACT_INCOMPLETE_MAX_ATTEMPTS):
            t0 = time.time()
            if use_synxis_path:
                # Lazy import: synxis_api uses only `requests`, but keep the
                # import inside the branch so module load on systems that
                # never exercise the path stays minimal.
                from synxis_api import fetch_synxis_direct
                body = fetch_synxis_direct(
                    arrival, los,
                    base_url=synxis_cfg["booking_url"],
                    hotel_id=synxis_cfg["hotel_id"],
                    chain_id=synxis_cfg["chain_id"],
                    adults=int((CONFIG.get("guests") or {}).get("adults", 2)),
                    children=int((CONFIG.get("guests") or {}).get("children", 0)),
                    property_name_for_payload=prop_ref.get("name", property_id),
                )
                # Synxis path consumes 0 Firecrawl credits — direct CRS gateway.
                credits = 0
            elif use_vision_path:
                # Codex item 3 (2026-04-28): lazy import — hotels_vision pulls
                # in anthropic + PIL, neither of which need to be installed
                # when use_vision_for_hotels_com is False.
                from hotels_vision import firecrawl_scrape_via_vision
                body = firecrawl_scrape_via_vision(
                    url, stealth=stealth, wait_for_ms=wait_for,
                    schema=EXTRACT_SCHEMA, prompt=prompt_to_use,
                    actions=actions,
                    timeout_ms=hotels_com_timeout_ms,
                )
                credits = CREDITS_STEALTH_JSON if stealth else CREDITS_BASIC_JSON
            else:
                body = firecrawl_scrape_json(
                    url, stealth=stealth, wait_for_ms=wait_for,
                    schema=EXTRACT_SCHEMA, prompt=prompt_to_use,
                    actions=actions,
                    # Hotels.com pins explicit 180s to cover ~30s of multi-scroll
                    # action time + 45s waitFor. Other channels use the wrapper's
                    # auto-pairing (2 × waitFor when waitFor > 15s).
                    timeout_ms=hotels_com_timeout_ms,
                )
                credits = CREDITS_STEALTH_JSON if stealth else CREDITS_BASIC_JSON
            elapsed = time.time() - t0
            total_credits += credits

            (OUT_DIR / f"{safe}__attempt{attempts}_{ic_attempt+1}.json").write_text(
                json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")

            if not body.get("success"):
                err = body.get("error") or "unknown"
                last_status = "http_error"; last_reason = str(err)[:300]
                break  # don't retry http errors inside inner loop

            data = body.get("data") or {}
            md = data.get("markdown") or ""
            extracted = data.get("json")

            # Gate 0 (Phase A2.5c, 2026-04-27): Synxis chain-page soft fail.
            # When AKA's reservations engine fails to auto-route to the hotel-
            # specific rate card, it serves the all-hotels chain selector
            # (lists Alexandria, Washington Circle, etc.). The signature is
            # both AKA hotel names appearing together in the markdown — that
            # combination only renders on the chain page. Treat as soft fail:
            # short-circuit before the Hotels.com post-processor and gates
            # below, and route into the outer bot-block retry loop so we
            # pause and retry rather than fail the cell on a misleading
            # property_name token mismatch.
            # Skipped on Synxis API path: no markdown to inspect, and the
            # chain-page failure mode is specific to the SPA-render route.
            if (not use_synxis_path
                    and "Hotel AKA Alexandria" in md
                    and "Hotel AKA Washington Circle" in md):
                last_status = "FAIL_CHAIN_PAGE_SOFT"
                last_reason = "Synxis chain selector returned in lieu of hotel rate card"
                break

            # Phase A2.5b post-processor — Hotels.com only. Replace LLM tier
            # extraction with deterministic markdown-anchored tier rows for
            # any room whose cancellation block rendered. Rooms without a
            # rendered block keep the LLM's rate_plans unchanged so gate-4's
            # retry trigger can still see the thin-render signal.
            if (channel_id == "hotels_com" and isinstance(extracted, dict)):
                extracted, hotels_tier_diagnostics = expand_tiers_from_markdown(
                    extracted, md,
                )

            # Gate 1: bot block (markdown signature). Skipped on Synxis
            # API path — no markdown, and the API gateway has its own
            # Imperva-aware retry inside fetch_synxis_direct.
            if not use_synxis_path:
                sig = check_bot_block(md)
                if sig:
                    last_status = "bot_blocked"; last_reason = f"signature: {sig!r}"
                    break  # break inner loop to trigger outer bot-block retry

            # Gate 2: plausibility. Skipped on Synxis API path — Rule 7
            # (rate-anchor) requires markdown, and the structured-API
            # response carries its own type/numeric guarantees that make
            # the LLM-anti-hallucination guard moot.
            if extracted is None:
                last_status = "extract_failed"; last_reason = "no json returned"
                break
            if isinstance(extracted, dict) and not (extracted.get("rooms") or []):
                if use_synxis_path:
                    last_status = "no_inventory"
                    last_reason = "Synxis returned zero available product prices"
                    break
                empty_verdict = classify_empty_inventory_page(
                    md, expected_property_tokens=tokens,
                )
                if empty_verdict == "no_inventory":
                    last_status = "no_inventory"
                    last_reason = "source page explicitly reports no inventory for requested stay"
                    break
                if empty_verdict == "FAIL_URL_BROKEN":
                    last_status = "FAIL_URL_BROKEN"
                    last_reason = "empty extraction came from a broken or wrong-property page"
                    break
            if not use_synxis_path:
                passed, reason = check_plausibility(
                    extracted, expected_property_tokens=tokens,
                    expected_arrival_date=arrival, markdown=md,
                    channel=channel_id,
                )
                if not passed:
                    last_status = "extract_failed"; last_reason = reason
                    break

            # Gate 3: room-type anchored (v2 — E2 fix). Every marketing_name
            # must be verbatim in markdown. Retry once with sharper prompt;
            # final fail blocks the cell from raw_rates.csv.
            # Skipped on Synxis API path — marketing_names come straight
            # from ContentLists.RoomList, no LLM in the path to fabricate.
            if not use_synxis_path:
                anchor_ok, anchor_reason = check_room_type_anchored(extracted, md)
                if not anchor_ok:
                    if ic_attempt < EXTRACT_INCOMPLETE_MAX_ATTEMPTS - 1:
                        prompt_to_use = EXTRACT_PROMPT_SHARPER
                        last_status = "extract_incomplete"; last_reason = f"anchor: {anchor_reason}"
                        time.sleep(2)
                        continue
                    last_status = "extract_failed"
                    last_reason = f"FAIL_HALLUCINATED_ROOM_TYPE: {anchor_reason}"
                    break

            # Gate 4: Hotels.com lazy-load retry (E6 fix; rebuilt in Phase A2.3).
            # Trigger when NO room expanded its rate ladder — i.e., max plans
            # across all rooms is <2. An earlier `min < 3` formulation was
            # wrong because Hotels.com legitimately shows different tier-counts
            # per room (Penthouse: 2 tiers; standard rooms: 4 tiers per Chrome
            # verification 2026-04-26 line 83). Forcing retry on min<3 fired on
            # every cell that had a Penthouse, even when other rooms rendered
            # fully — and the retry path empirically OVERWROTE the better
            # attempt with the totals-headline degenerate response. New
            # threshold: `max < 2` fires only when truly no expansion happened.
            #
            # Switch from default action plan to retry variant (longer settles,
            # ~57s total). We can't bump waitFor — Firecrawl caps `waitFor +
            # sum(wait actions) <= 60s` and the action sequence eats most of
            # that budget; the retry path adds time *inside* the action plan.
            plans_per_room = [
                len(r.get("rate_plans") or [])
                for r in (extracted.get("rooms") or [])
                if isinstance(r, dict)
            ]
            max_plans_per_room = max(plans_per_room, default=0)
            min_plans_per_room = min(plans_per_room, default=0)
            # Phase A2.4 #7: also retry when n_rooms < expected_room_count.
            # Catches "1 room with 3 tiers" passing the per-room gate even
            # when 4 other expected rooms didn't render.
            # Phase A2.5b: prefer Hotels.com's own 'Showing X of Y rooms'
            # marker when present; otherwise fall back to canonical-max.
            _expected_for_retry = EXPECTED_ROOM_COUNT_BY_CHANNEL.get(channel_id, 0)
            if channel_id == "hotels_com":
                declared_inv = _parse_page_declared_inventory(md)
                if declared_inv is not None:
                    _expected_for_retry = declared_inv
            should_retry, retry_reason = should_trigger_hotels_com_retry(
                channel=channel_id,
                n_rooms=len(plans_per_room),
                max_plans_per_room=max_plans_per_room,
                min_plans_per_room=min_plans_per_room,
                expected_room_count=_expected_for_retry,
            )
            if (should_retry and not hotels_retry
                    and ic_attempt < EXTRACT_INCOMPLETE_MAX_ATTEMPTS - 1):
                hotels_retry = True
                actions = _build_hotels_actions(retry=True)
                last_status = "extract_incomplete"
                last_reason = f"hotels.com retry: {retry_reason}; longer-settle plan"
                time.sleep(2)
                continue

            # Gate 5: variance — AKA-specific completeness. Skipped on
            # Synxis API path: variance compares markdown $-tokens to
            # extracted rates, and there's no markdown.
            if not use_synxis_path:
                missed_patterns = check_variance(md, extracted.get("rooms") or [], property_id)
                if missed_patterns and ic_attempt < EXTRACT_INCOMPLETE_MAX_ATTEMPTS - 1:
                    # Retry once with sharper prompt
                    prompt_to_use = EXTRACT_PROMPT_SHARPER
                    last_status = "extract_incomplete"; last_reason = f"missed: {missed_patterns}"
                    time.sleep(2)
                    continue

            # If we reach here, this attempt succeeded (possibly with persistent missed patterns)
            if missed_patterns:
                last_status = "extract_incomplete"
                last_reason = f"missed after retry: {missed_patterns}"
            else:
                last_status = "ok"
                last_reason = ""
            break  # exit inner loop

        # ---- outer retry decision (bot-block + chain-page soft fail) ----
        if last_status in ("bot_blocked", "FAIL_CHAIN_PAGE_SOFT") and bb_attempt < BOT_BLOCK_MAX_ATTEMPTS - 1:
            # bot_blocked: stealth proxy pool rotates on reconnect after pause.
            # FAIL_CHAIN_PAGE_SOFT: Synxis may auto-route to hotel rate card on
            # subsequent attempt; pause lets any rate-limit window clear.
            time.sleep(BOT_BLOCK_PAUSE_SECONDS)
            continue
        break  # exit outer loop

    elapsed = time.time() - t_start

    # Phase A2.4b #2 — room-count fail-closed on retry exhaustion. Gate-4
    # (#7) flips to a longer-settle Hotels.com plan when n_rooms <
    # expected on the first pass; if the retry still returns fewer rooms,
    # control fell through to ok / extract_incomplete and partial data
    # was persisted. Now: write a single FAIL_ROOM_COUNT_SHORT sentinel
    # and skip _flatten_rooms_to_rows entirely. Runs BEFORE the unmapped
    # check (#1) and BAR anchor (#5) since those operate on the rooms we
    # DID get, and "we're missing rooms" is a more fundamental failure.
    #
    # 2026-04-28 — Synxis path exception: when the data comes from the
    # Synxis CRS API rather than a Firecrawl/LLM render, n_rooms <
    # expected means real sellout, not a broken extract. The CRS only
    # returns SKUs that are actually bookable. Treat as a soft signal:
    # status=`compression_short`, persist all rows we got, and let the
    # dashboard surface compression from the log. No sentinel row is
    # emitted; resume considers the cell complete via its ok rows.
    room_count_short = False
    n_rooms_extracted = 0
    expected_room_count_for_channel = 0
    if last_status in ("ok", "extract_incomplete"):
        room_count_short, n_rooms_extracted, expected_room_count_for_channel = (
            detect_room_count_short(
                extracted, channel=channel_id, markdown=md,
                property_id=property_id,
            )
        )
        if room_count_short:
            if use_synxis_path:
                last_status = "compression_short"
                last_reason = (
                    f"compression_short: {n_rooms_extracted} of "
                    f"{expected_room_count_for_channel} SKUs available (real sellout)"
                )
                # Disarm the sentinel-only persistence below so rows still flatten.
                room_count_short = False
            else:
                last_status = "FAIL_ROOM_COUNT_SHORT"
                last_reason = (
                    f"only {n_rooms_extracted} of expected "
                    f"{expected_room_count_for_channel} rooms rendered after retry exhaustion"
                )

    # Phase A2.4 #1 — AKA canonical-room coverage gate. Runs AFTER the inner
    # loop succeeds (extract passed plausibility, room-type anchor, variance).
    # If any extracted marketing_name doesn't map to a canonical SKU, the cell
    # FAILS CLOSED: no rows persisted, last_status=FAIL_UNKNOWN_ROOM_TYPE,
    # unmapped strings surfaced in last_reason → scrape_log. Resume logic
    # naturally re-queues because no rows land for the cell.
    unmapped_aka_rooms: list[str] = []
    if (last_status in ("ok", "extract_incomplete", "compression_short")
            and isinstance(extracted, dict)
            and property_id == "hr_embarcadero"):
        unmapped_aka_rooms = find_unmapped_aka_marketing_names(extracted)
        if unmapped_aka_rooms:
            sample = unmapped_aka_rooms[:5]
            more = f" (+{len(unmapped_aka_rooms)-5} more)" if len(unmapped_aka_rooms) > 5 else ""
            last_status = "FAIL_UNKNOWN_ROOM_TYPE"
            last_reason = f"unmapped marketing_names: {sample}{more}"

    # Phase A2.4 #5 — canonical BAR rate anchor. Every BAR_NON_REF and
    # BAR_FLEX picked rate must appear in the source markdown (with a
    # Hotels.com tier add-on exemption that requires base + literal "+$N"
    # to both appear). Catches partial hallucination on the canonical
    # plans, which Rule 7 misses.
    # Skipped on Synxis API path — markdown is empty by construction and
    # the rates come straight from the CRS gateway, not an LLM extraction.
    bar_anchor_warnings: list[dict] = []
    if (last_status in ("ok", "extract_incomplete")
            and isinstance(extracted, dict)
            and not use_synxis_path):
        anchor_ok, anchor_reason, bar_anchor_warnings = check_canonical_bar_anchored(extracted, md)
        if not anchor_ok:
            last_status = "extract_failed"
            last_reason = anchor_reason

    # ---- persist rows if we have good or partial data ----
    rows: list[dict] = []
    if last_status == "no_inventory":
        rows = [_make_cell_status_sentinel(
            property_id=property_id, channel_id=channel_id, arrival=arrival,
            los=los, source_url=url, markdown=md, status="no_inventory",
            reason=last_reason,
        )]
    elif room_count_short:
        # Phase A2.4b #2: cell-level sentinel only; no plan rows. The
        # sentinel re-queues the cell on resume via SENTINEL_STATUSES.
        # Note: Synxis path disarms `room_count_short` above so it lands
        # in the elif and persists rows under status=compression_short.
        rows = [_make_room_count_short_sentinel(
            property_id=property_id, channel_id=channel_id, arrival=arrival,
            los=los, source_url=url, markdown=md,
            n_rooms=n_rooms_extracted, expected_n=expected_room_count_for_channel,
        )]
    elif (last_status in ("ok", "extract_incomplete", "compression_short")
            and extracted is not None):
        rows = _flatten_rooms_to_rows(
            extracted, property_id, channel_id, arrival, los, url, missed_patterns,
            markdown=md,
        )

    # v2: track Hotels.com render success for the run log (E6 success metric).
    # Phase A2.3: switched from aggregate sum to per-room min so a 5-rooms-×-1-plan
    # cell reads as thin_1 (the actual problem) rather than ok_5.
    hotels_com_render_status = None
    if channel_id == "hotels_com":
        if isinstance(extracted, dict):
            ppr = [len(r.get("rate_plans") or []) for r in (extracted.get("rooms") or []) if isinstance(r, dict)]
            n_min = min(ppr, default=0)
            hotels_com_render_status = f"ok_min_{n_min}" if n_min >= 3 else f"thin_min_{n_min}"
        else:
            hotels_com_render_status = "thin_min_0"

    # Phase A2.4 #3 — log per-cell UNKNOWN-refundability count for diagnostics.
    # High counts signal that cancellation_phrase capture is failing or
    # rate-plan label tokens are drifting on a channel.
    unknown_refundability_n = (
        count_unknown_refundability(extracted) if isinstance(extracted, dict) else 0
    )

    # Phase A2.4 #2 — surface per-room picker failures at the cell level so
    # operators can see which rooms produced FAIL_NO_BARE_NON_REF / FAIL_NO_BAR_FLEX
    # without grepping raw_rates.csv. Cells where every room hit a picker
    # failure also won't have any ok/extract_incomplete/non_bar_rate row, so
    # resume re-queues them (load_completed_cells gates on row status).
    picker_failures = summarize_picker_failures(rows)

    # Phase A2.5b — Hotels.com tier-parser summary (rooms_replaced /
    # passthrough / tiers_total / no_lead_in). Compact 4-int form for
    # log readability; per-room detail stays in `hotels_tier_diagnostics`
    # within the inner loop scope but isn't surfaced here to keep events small.
    hotels_tier_summary = None
    if hotels_tier_diagnostics is not None:
        hotels_tier_summary = {
            "replaced": hotels_tier_diagnostics["rooms_replaced"],
            "passthrough": hotels_tier_diagnostics["rooms_passthrough"],
            "no_lead_in": hotels_tier_diagnostics["rooms_no_lead_in"],
            "tiers_total": hotels_tier_diagnostics["tiers_total"],
        }

    log_event({
        "kind": last_status, "property": property_id, "channel": channel_id,
        "arrival": arrival, "los": los, "attempts": attempts,
        "rooms_count": len(rows), "reason": last_reason[:300],
        "elapsed_s": elapsed, "credits_nominal": total_credits,
        "markdown_len": len(md),
        "wait_for_ms_final": wait_for,
        "hotels_com_render": hotels_com_render_status,
        "hotels_tier_parser": hotels_tier_summary,
        "unmapped_aka_rooms": unmapped_aka_rooms,
        "unknown_refundability_n": unknown_refundability_n,
        "picker_failures": picker_failures,
        "bar_anchor_warnings": bar_anchor_warnings,
    })

    if verbose:
        print(f"  [{last_status.upper():8}] {property_id}/{channel_id}/{arrival}/LOS{los}: "
              f"{len(rows)} rows, md={len(md)}c, {elapsed:.1f}s, ~{total_credits}cr, {attempts} attempts "
              f"{last_reason[:80]}")

    return CellResult(
        property_id, channel_id, arrival, los, url, last_status, last_reason,
        rows=rows, markdown_len=len(md), elapsed_s=elapsed, credits_nominal=total_credits,
        attempts=attempts,
    )


# =============================================================================
# CSV append (RAW_HEADER imported from schema.py — v2)
# =============================================================================
def append_rows_to_csv(rows: list[dict]) -> None:
    if not rows: return
    exists = RAW_CSV.exists()
    with open(RAW_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RAW_HEADER, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerows(rows)


# =============================================================================
# Resume helpers — skip cells that already landed in raw_rates.csv
# =============================================================================
def load_completed_cells() -> set[tuple[str, str, str, int]]:
    """Return set of (property_id, channel, arrival_date, nights) keys treated
    as 'done' for resume purposes.

    Phase A2.4b #1: a cell is complete iff it has at least one row with
    status `ok` or `extract_incomplete` AND zero rows with sentinel
    statuses (SENTINEL_STATUSES). Sentinel rows trump everything else —
    even a cell with 4 non_bar_rate rows from successful per-plan
    extraction must be re-queued if one of its rooms also produced a
    FAIL_NO_BARE_NON_REF / FAIL_NO_BAR_FLEX / FAIL_ROOM_COUNT_SHORT row.

    `non_bar_rate` rows alone are no longer enough to count as done: those
    rows are byproducts of a room where the picker found nothing (it emits
    a sentinel + non_bar_rate companions for the other plans), so trusting
    them as 'done' defeats the sentinel re-queue intent.
    """
    if not RAW_CSV.exists():
        return set()
    has_extracted: set[tuple[str, str, str, int]] = set()
    has_sentinel: set[tuple[str, str, str, int]] = set()
    with open(RAW_CSV, encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            try:
                key = (r["property_id"], r["channel"], r["arrival_date"], int(r["nights"]))
            except Exception:
                continue
            status = (r.get("status") or "").strip()
            # `compression_short` rows (Synxis path, real sellout) carry
            # the same flatten output as ok rows — count them as extracted
            # so resume doesn't re-queue genuine compression cells.
            if status.lower() in ("ok", "extract_incomplete", "compression_short", "no_inventory"):
                has_extracted.add(key)
            if status in SENTINEL_STATUSES:
                has_sentinel.add(key)
    return has_extracted - has_sentinel


def _parse_pause_at(s: str) -> datetime.time:
    """Parse HH:MM into a datetime.time for local-time comparison."""
    hh, mm = s.split(":")
    return datetime.time(int(hh), int(mm))


def _write_resume_state(
    completed_cells: int,
    total_cells: int,
    credits_spent: int,
    outcomes: dict,
    remaining_cells: list,
    skipped_cells: int,
    reason: str,
) -> None:
    state = {
        "paused_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "paused_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "reason": reason,
        "completed_cells_this_run": completed_cells,
        "skipped_as_already_done": skipped_cells,
        "total_cells_queued": total_cells,
        "credits_spent_this_run": credits_spent,
        "outcomes_this_run": dict(outcomes),
        "remaining_cells": [list(c) for c in remaining_cells],
    }
    RESUME_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


# =============================================================================
# Channel filtering — Option A: Expedia only for AKA
# =============================================================================
def is_cell_allowed(property_id: str, channel_id: str) -> bool:
    # Never run the deprecated Expedia channel
    if channel_id == "expedia":
        return False
    # Hotels.com is subject-only (SFOEM = hr_embarcadero); comps don't run hotels_com
    if channel_id == "hotels_com" and property_id != "hr_embarcadero":
        return False
    # Capital Hilton direct is Akamai-walled — 3-attempt stealth still timed out @ 288s/75cr
    # in pre-matrix verification. Captured via Booking.com only for CH.
    if property_id == "capital_hilton" and channel_id == "direct":
        return False
    # Willard direct: IHG's deep-link URLs don't work cleanly. /hoteldetail/rooms-rates
    # strips query params; /find-hotels/select-roomrate returns 404. Willard booking
    # has 91% coverage — sufficient for comp positioning. 17 direct cells already in
    # raw_rates from earlier run provide partial direct-channel signal.
    if property_id == "willard" and channel_id == "direct":
        return False
    return True


# =============================================================================
# CLI
# =============================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cells", nargs="+", help="prop:channel:arrival:los (repeatable)")
    ap.add_argument("--full", action="store_true", help="run full matrix from config with Option A channel filter")
    ap.add_argument("--resume", action="store_true", help="equivalent to --full but skip cells already in raw_rates.csv")
    ap.add_argument("--retry-failed", action="store_true", help="re-run only cells previously logged as http_error/extract_failed/bot_blocked")
    ap.add_argument("--dry-run", action="store_true", help="list cells, don't execute")
    ap.add_argument("--pace-seconds", type=float, default=3.0, help="sleep between cells")
    ap.add_argument("--credit-ceiling", type=int, default=CREDITS_CEILING)
    ap.add_argument("--pause-at", type=str, default="", help="Local time HH:MM at which to stop before next cell (e.g., '17:00'). Writes resume_state.json on stop.")
    ap.add_argument("--firecrawl-timeout-ms", type=int, default=0,
                    help="Override Firecrawl scrape timeout. 0 = use Firecrawl default (~30s). Use 120000 (2min) for retry pass on Synxis-like slow pages.")
    ap.add_argument("--direct-wait-for-override", type=int, default=0,
                    help="Override per-channel waitFor for direct cells. 0 = use channel default. Use 20000 (20s) for retry pass on Synxis pages that need extra render time.")
    ap.add_argument("--min-wait-for-ms", type=int, default=0,
                    help="Floor applied to every channel's waitFor. Use 20000 to ensure lazy-loading OTAs (Hotels.com) fully render before scrape.")
    args = ap.parse_args()

    # Apply CLI overrides to module-level globals (used inside firecrawl_scrape_json + scrape_cell)
    global _OVERRIDE_FIRECRAWL_TIMEOUT_MS, _OVERRIDE_DIRECT_WAIT_FOR_MS, _OVERRIDE_MIN_WAIT_FOR_MS
    _OVERRIDE_FIRECRAWL_TIMEOUT_MS = args.firecrawl_timeout_ms
    _OVERRIDE_DIRECT_WAIT_FOR_MS = args.direct_wait_for_override
    _OVERRIDE_MIN_WAIT_FOR_MS = args.min_wait_for_ms
    if _OVERRIDE_FIRECRAWL_TIMEOUT_MS:
        print(f"  Firecrawl timeout override: {_OVERRIDE_FIRECRAWL_TIMEOUT_MS} ms")
    if _OVERRIDE_DIRECT_WAIT_FOR_MS:
        print(f"  Direct channel waitFor override: {_OVERRIDE_DIRECT_WAIT_FOR_MS} ms")
    if _OVERRIDE_MIN_WAIT_FOR_MS:
        print(f"  Min waitFor floor (all channels): {_OVERRIDE_MIN_WAIT_FOR_MS} ms")

    # --resume implies --full
    if args.resume:
        args.full = True

    cells: list[tuple[str, str, str, int]] = []
    if args.cells:
        for spec in args.cells:
            parts = spec.split(":")
            if len(parts) != 4:
                print(f"skipping bad cell spec: {spec}")
                continue
            cells.append((parts[0], parts[1], parts[2], int(parts[3])))
    if args.full:
        for p in CONFIG["properties"]:
            for c in CONFIG["channels"]:
                if not is_cell_allowed(p["id"], c["id"]):
                    continue
                for d in CONFIG["date_matrix"]:
                    for L in CONFIG["length_of_stay_nights"]:
                        cells.append((p["id"], c["id"], d["arrival"], L))
    if args.retry_failed:
        # Parse scrape_log.txt for cells that previously failed; rebuild the queue
        failed: list[tuple[str, str, str, int]] = []
        seen: set[tuple[str, str, str, int]] = set()
        if LOG_PATH.exists():
            with open(LOG_PATH, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if e.get("kind") in ("http_error", "extract_failed", "bot_blocked"):
                        try:
                            key = (e["property"], e["channel"], e["arrival"], int(e["los"]))
                        except Exception:
                            continue
                        if key in seen:
                            continue
                        seen.add(key)
                        # Skip if cell was later successfully extracted (would be in raw_rates with status=ok)
                        failed.append(key)
        # Drop any that are now successfully in raw_rates (already-fixed cells)
        already_done = load_completed_cells()
        failed = [c for c in failed if c not in already_done]
        # Drop cells that are now disallowed (e.g. Willard direct dropped after URL bug)
        failed = [c for c in failed if is_cell_allowed(c[0], c[1])]
        cells.extend(failed)
        print(f"  Retry-failed: {len(failed)} cells loaded from scrape_log.txt (after deduping cells now in raw_rates)")
    if not cells:
        print("no cells — use --cells, --full, or --retry-failed"); return 1

    # Resume support: pre-filter completed cells out of the queue
    already_done = load_completed_cells() if (args.resume or args.full) else set()
    if already_done:
        before = len(cells)
        cells = [c for c in cells if c not in already_done]
        print(f"  Resume: skipping {before - len(cells)} cells already in raw_rates.csv")

    pause_time: Optional[datetime.time] = None
    if args.pause_at:
        pause_time = _parse_pause_at(args.pause_at)
        print(f"  Pause-at: {args.pause_at} local time (will stop gracefully before next cell once reached)")

    print(f"Queued {len(cells)} cells.")
    if args.dry_run:
        for c in cells[:40]: print("  ", c)
        if len(cells) > 40: print(f"  ... and {len(cells)-40} more")
        # Project cost
        stealth_count = sum(1 for (p, c, _, _) in cells
                            if next(x for x in CONFIG["channels"] if x["id"] == c).get("stealth"))
        basic_count = len(cells) - stealth_count
        print(f"  Projected nominal credits: {stealth_count * CREDITS_STEALTH_JSON + basic_count * CREDITS_BASIC_JSON}")
        return 0

    total_credits = 0
    total_rows = 0
    outcomes = {"ok": 0, "bot_blocked": 0, "extract_failed": 0,
                "extract_incomplete": 0, "http_error": 0, "no_url": 0,
                "no_inventory": 0, "FAIL_URL_BROKEN": 0}
    t_start = time.time()
    stop_reason = ""
    completed_this_run = 0
    idx = 0
    for idx, (p, c, a, L) in enumerate(cells, 1):
        # --- Pre-cell guards ---
        if total_credits >= args.credit_ceiling:
            stop_reason = f"credit ceiling {args.credit_ceiling} reached"
            print(f"\nCREDIT CEILING {args.credit_ceiling} REACHED after {idx-1} cells. Stopping.")
            log_event({"kind": "ceiling_reached", "credits": total_credits, "cells_completed": idx-1})
            break
        if pause_time is not None:
            now_local = datetime.datetime.now().time()
            if now_local >= pause_time:
                stop_reason = f"pause-at {args.pause_at} reached (local time {now_local.strftime('%H:%M:%S')})"
                print(f"\n⏸  PAUSE-AT {args.pause_at} REACHED (now {now_local.strftime('%H:%M:%S')}). Stopping before cell {idx}.")
                log_event({"kind": "paused", "credits": total_credits, "cells_completed": idx-1,
                           "pause_time": args.pause_at, "now": now_local.strftime("%H:%M:%S")})
                # Back up one — cell idx never started, it goes back in the remaining queue
                idx -= 1
                break
        print(f"\n[{idx}/{len(cells)}] credits={total_credits}/{args.credit_ceiling}")
        result = scrape_cell(p, c, a, L)
        completed_this_run += 1
        total_credits += result.credits_nominal
        total_rows += len(result.rows)
        outcomes[result.status] = outcomes.get(result.status, 0) + 1
        append_rows_to_csv(result.rows)
        # Every 50 cells: burn-rate & outcomes checkpoint
        if idx % 50 == 0:
            elapsed_m = (time.time() - t_start) / 60
            print(f"\n--- CHECKPOINT @ cell {idx} ---")
            print(f"  credits: {total_credits}/{args.credit_ceiling} ({100*total_credits/args.credit_ceiling:.0f}%)")
            print(f"  rows extracted: {total_rows}")
            print(f"  bot_blocked: {outcomes['bot_blocked']}")
            print(f"  extract_incomplete: {outcomes['extract_incomplete']}")
            print(f"  extract_failed: {outcomes['extract_failed']}")
            print(f"  ok: {outcomes['ok']}")
            print(f"  elapsed: {elapsed_m:.1f} min, projected-to-finish: {elapsed_m * len(cells) / idx:.1f} min")
            log_event({"kind": "checkpoint", "cell": idx, "total_cells": len(cells),
                       "credits": total_credits, "credit_ceiling": args.credit_ceiling,
                       "rows": total_rows, "outcomes": dict(outcomes), "elapsed_m": elapsed_m})
        time.sleep(args.pace_seconds)

    elapsed = time.time() - t_start
    print(f"\n{'='*60}\nSUMMARY")
    print(f"  Cells completed this run: {completed_this_run}")
    print(f"  Already-done cells skipped: {len(already_done)}")
    print(f"  Credits this run: {total_credits} / {args.credit_ceiling}")
    print(f"  Elapsed: {elapsed/60:.1f} min")
    print("  Outcomes:")
    for k, v in outcomes.items():
        print(f"    {k}: {v}")

    # If we broke early, write resume_state.json
    if stop_reason:
        remaining = cells[idx:]  # cells that have not yet been attempted
        _write_resume_state(
            completed_cells=completed_this_run,
            total_cells=len(cells) + len(already_done),
            credits_spent=total_credits,
            outcomes=outcomes,
            remaining_cells=remaining,
            skipped_cells=len(already_done),
            reason=stop_reason,
        )
        direct_ok = outcomes.get("ok", 0) + outcomes.get("extract_incomplete", 0)
        print(f"\n⏸  PAUSED: {stop_reason}")
        print(f"    Resume with: py scrape.py --resume --pause-at HH:MM")
        print(f"    Remaining cells queued: {len(remaining)}")
        print(f"    resume_state.json written at: {RESUME_STATE_PATH}")

    log_event({"kind": "run_end", "credits": total_credits, "elapsed_s": elapsed,
               "outcomes": outcomes, "completed_this_run": completed_this_run,
               "stop_reason": stop_reason})
    return 0


if __name__ == "__main__":
    sys.exit(main())
