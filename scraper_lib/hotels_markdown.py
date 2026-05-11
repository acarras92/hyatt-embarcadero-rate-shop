r"""Hotels.com cancellation-tier deterministic parser (Phase A2.5b, 2026-04-27).

Why this exists
---------------
Phase A2.3's extraction prompt told the LLM how to interpret the per-room
'Cancellation policy' radio-button block ('+ $N is an add-on over the base
nightly rate'). In production the LLM ignored that math half the time:
the 2026-09-26 LOS3 cell's stored JSON has
    rate_plan_label="Fully refundable before Sep 23", rate_per_night_usd=163
where the real nightly rate is $348 (lead-in) + $163 = $511, NOT $163.

Firecrawl's markdown DOES preserve the structure verbatim (escaped as
'\+ $163Reserve now, pay deposit'). So we replace the LLM's tier extraction
with a deterministic markdown parser when the structure is present.

Markdown shape (one room block)
-------------------------------
    ### Suite, 1 Bedroom (Platinum)

    - 820 sq ft
    ...

    #### Cancellation policy

    per stay

    More details on all policy options

    Cancellation policy

    Non-Refundable
    \\+ $0Reserve now, pay depositReserve now, pay deposit

    Fully refundable before Sep 23
    \\+ $163Reserve now, pay depositReserve now, pay deposit

    Non-Refundable
    \\+ $242Reserve now, pay laterReserve now, pay later

    Fully refundable before Sep 23
    \\+ $407Reserve now, pay laterReserve now, pay later

    #### Extras
    ...

    $348 nightly

    The current price is $1,257 total

The four tier rows decompose as:
    refundability (Non-Refundable | Fully refundable[ before <date>])
    add_on_usd ('\\+ $N' immediately following the refundability label)
    pay_timing  ('Reserve now, pay deposit' | 'Reserve now, pay later')

Lead-in nightly rate is the '$N nightly' that follows the cancellation block,
matching the cheapest tier (typically Non-Refundable + pay-deposit + $0).

Output contract
---------------
expand_tiers_from_markdown(extracted, markdown) returns the extracted dict
with rooms[i].rate_plans REPLACED for any room whose markdown block contains
a parseable cancellation structure. Rooms whose markdown block is missing
or has no tier structure are left untouched (LLM output preserved). The
function is idempotent and side-effect-free on its inputs.
"""
from __future__ import annotations
import re
from typing import Optional

# Matches a single cancellation-tier row. Captures:
#   1: refundability label (e.g. "Non-Refundable" / "Fully refundable before Sep 23")
#   2: add-on amount in dollars (integer or comma-grouped)
#   3: pay-timing label ("Reserve now, pay deposit" or "Reserve now, pay later")
# The pay-timing label appears twice in source ("Reserve now, pay deposit
# Reserve now, pay deposit") — once for the visible label and once for ARIA.
# We anchor on the first occurrence and the duplicate is consumed by the
# greedy match later when slicing.
_TIER_ROW_RE = re.compile(
    r"(Non-Refundable|Fully refundable(?:\s+before\s+[A-Za-z0-9, ]+?)?)\s*\n"
    r"\\\+\s*\$([\d,]+)"
    r"(Reserve now, pay (?:deposit|later))",
    re.IGNORECASE,
)

# Lead-in nightly rate: '$348 nightly' anywhere in the room block.
_LEAD_IN_NIGHTLY_RE = re.compile(r"\$([\d,]+)\s*nightly", re.IGNORECASE)

# Per-stay total fallback (used when nightly isn't present / is rounded).
_TOTAL_STAY_RE = re.compile(
    r"(?:current price is|price is)\s+\$([\d,]+)\s*total",
    re.IGNORECASE,
)


def _money_to_int(s: str) -> int:
    return int(s.replace(",", ""))


def _normalize_room_heading(name: str) -> str:
    """Lower-case + collapse whitespace for fuzzy room-block matching."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def find_room_block(markdown: str, marketing_name: str) -> Optional[str]:
    """Slice markdown to the per-room block matching marketing_name.

    Hotels.com renders each room as TWO '###' headings: 'View all photos
    for <NAME>' (gallery anchor) followed by '### <NAME>' (the actual room
    block with cancellation policy + nightly rate). We anchor on the
    second heading and slice until the next 'View all photos for' (or the
    'Have a question?' marker that signals the end of the room list) or
    end-of-document.

    Returns None if the marketing_name's block is not present.
    """
    if not markdown or not marketing_name:
        return None
    target = _normalize_room_heading(marketing_name)
    # Find every '### <something>' heading and its position.
    headings: list[tuple[int, str]] = [
        (m.start(), m.group(1).strip())
        for m in re.finditer(r"^###\s+(.+)$", markdown, re.MULTILINE)
    ]
    # The room body heading is the one whose normalized text equals target
    # AND is NOT prefixed with 'View all photos for'.
    body_start: Optional[int] = None
    body_idx_in_list: Optional[int] = None
    for i, (pos, text) in enumerate(headings):
        if text.lower().startswith("view all photos for"):
            continue
        if _normalize_room_heading(text) == target:
            body_start = pos
            body_idx_in_list = i
            break
    if body_start is None:
        return None
    # Block ends at the NEXT heading (any '###'), or at 'Have a question?'
    # / 'Similar properties to' / EOF, whichever comes first.
    end_candidates: list[int] = [len(markdown)]
    if body_idx_in_list is not None and body_idx_in_list + 1 < len(headings):
        end_candidates.append(headings[body_idx_in_list + 1][0])
    for marker_re in (r"^##\s+Have a question\?", r"^##\s+Similar properties"):
        m = re.search(marker_re, markdown[body_start:], re.MULTILINE)
        if m:
            end_candidates.append(body_start + m.start())
    block_end = min(end_candidates)
    return markdown[body_start:block_end]


def parse_cancellation_tiers(block: str) -> list[dict]:
    """Extract the per-room cancellation-tier rows from one room block.

    Returns a list of dicts, one per radio button:
        {"refundability": "Non-Refundable" | "Fully refundable...",
         "pay_timing":    "deposit" | "later",
         "add_on_usd":    int}
    Empty list if no tiers are present (room is unavailable, or the block
    didn't render fully).
    """
    if not block:
        return []
    out: list[dict] = []
    for m in _TIER_ROW_RE.finditer(block):
        refund_label = m.group(1).strip()
        add_on = _money_to_int(m.group(2))
        timing_raw = m.group(3).lower()
        pay_timing = "deposit" if "deposit" in timing_raw else "later"
        out.append({
            "refundability": refund_label,
            "pay_timing": pay_timing,
            "add_on_usd": add_on,
        })
    return out


def parse_lead_in_nightly(block: str) -> Optional[int]:
    """Pull the '$N nightly' anchor rate from the room block. Returns None
    if the block doesn't contain one (e.g., room sold out)."""
    if not block:
        return None
    m = _LEAD_IN_NIGHTLY_RE.search(block)
    return _money_to_int(m.group(1)) if m else None


def parse_total_stay(block: str) -> Optional[int]:
    """Pull the 'current price is $N total' anchor — used to back-compute
    nightly rate when '$N nightly' isn't present."""
    if not block:
        return None
    m = _TOTAL_STAY_RE.search(block)
    return _money_to_int(m.group(1)) if m else None


def _build_rate_plan_from_tier(tier: dict, lead_in_nightly: int,
                               nights: int) -> dict:
    """Construct one rate_plan dict from a parsed tier + the lead-in.

    Convention (matches Phase A2.3 prompt and downstream picker semantics):
    rate_per_night_usd = lead_in + add_on. The add-on is treated as a
    per-night premium relative to the lead-in tier. total_stay_usd is the
    bare-rate stay total (no fees/taxes); downstream BAR anchor only checks
    that the per-night value is verbatim in markdown for non-zero add-ons,
    and the lead-in tier matches '$N nightly' verbatim.
    """
    add_on = tier["add_on_usd"]
    refund_label = tier["refundability"]
    pay_timing = tier["pay_timing"]
    is_refundable = refund_label.lower().startswith("fully refundable")
    nightly = lead_in_nightly + add_on
    label_suffix = f"+ ${add_on}" if add_on > 0 else "+ $0"
    timing_suffix = "pay deposit" if pay_timing == "deposit" else "pay later"
    return {
        "rate_plan_label": f"{refund_label} {label_suffix} ({timing_suffix})",
        "rate_per_night_usd": nightly,
        "total_stay_usd": nightly * max(1, nights),
        "refundable": is_refundable,
        "is_genius_member_rate": False,
        "includes_breakfast": False,
        "rate_plan_confidence": "high",
        "availability_status": "available",
        "cancellation_phrase": refund_label,
    }


def expand_tiers_from_markdown(extracted: dict,
                                markdown: str) -> tuple[dict, dict]:
    """Replace LLM-derived rate_plans with deterministic per-room tiers
    parsed from markdown.

    Returns (extracted_with_replaced_plans, diagnostics) where diagnostics
    is a dict shaped:
        {"rooms_replaced": int,
         "rooms_passthrough": int,    # had no tier structure in markdown
         "rooms_no_lead_in": int,     # had tiers but no '$N nightly' anchor
         "tiers_total": int,
         "per_room": [
             {"marketing_name": ..., "n_tiers": ..., "lead_in": ...,
              "replaced": bool, "skip_reason": str|None},
             ...]}

    Pass-through is never an error: rooms that can't be augmented keep the
    LLM's rate_plans unchanged so the existing picker logic still runs.
    """
    diagnostics = {
        "rooms_replaced": 0, "rooms_passthrough": 0, "rooms_no_lead_in": 0,
        "tiers_total": 0, "per_room": [],
    }
    if not isinstance(extracted, dict) or not markdown:
        return extracted, diagnostics
    rooms = extracted.get("rooms")
    if not isinstance(rooms, list):
        return extracted, diagnostics
    nights = extracted.get("nights") or 1
    try:
        nights_int = int(nights)
    except (TypeError, ValueError):
        nights_int = 1

    new_rooms: list[dict] = []
    for room in rooms:
        if not isinstance(room, dict):
            new_rooms.append(room)
            continue
        marketing_name = (room.get("marketing_name") or "").strip()
        block = find_room_block(markdown, marketing_name)
        per_room_diag = {
            "marketing_name": marketing_name, "n_tiers": 0,
            "lead_in": None, "replaced": False, "skip_reason": None,
        }
        if not block:
            per_room_diag["skip_reason"] = "no_room_block"
            diagnostics["rooms_passthrough"] += 1
            diagnostics["per_room"].append(per_room_diag)
            new_rooms.append(room)
            continue
        tiers = parse_cancellation_tiers(block)
        per_room_diag["n_tiers"] = len(tiers)
        if not tiers:
            per_room_diag["skip_reason"] = "no_tiers_in_block"
            diagnostics["rooms_passthrough"] += 1
            diagnostics["per_room"].append(per_room_diag)
            new_rooms.append(room)
            continue
        lead_in = parse_lead_in_nightly(block)
        if lead_in is None:
            # Fallback: back-compute from total / nights.
            stay_total = parse_total_stay(block)
            if stay_total is not None and nights_int > 0:
                lead_in = stay_total // nights_int
        if lead_in is None:
            per_room_diag["skip_reason"] = "no_lead_in_anchor"
            diagnostics["rooms_no_lead_in"] += 1
            diagnostics["per_room"].append(per_room_diag)
            new_rooms.append(room)
            continue
        per_room_diag["lead_in"] = lead_in
        new_room = dict(room)
        new_room["rate_plans"] = [
            _build_rate_plan_from_tier(t, lead_in, nights_int) for t in tiers
        ]
        per_room_diag["replaced"] = True
        diagnostics["rooms_replaced"] += 1
        diagnostics["tiers_total"] += len(tiers)
        diagnostics["per_room"].append(per_room_diag)
        new_rooms.append(new_room)

    new_extracted = dict(extracted)
    new_extracted["rooms"] = new_rooms
    return new_extracted, diagnostics
