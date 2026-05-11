"""Canonical room-type and rate-plan identity for Hyatt Regency San Francisco Embarcadero.

Why this file exists
--------------------
Cross-channel comparison requires joining on the SAME product across each
channel. Each channel labels the same SKU differently; without a canonical
map, "channel parity" comparisons silently mismatch and dashboards lie.

What lives here
---------------
ROOM_TYPE_CANONICAL — string (verbatim from page) -> canonical room ID.
                      Any extracted string NOT in this map is treated as
                      FAIL_UNKNOWN_ROOM_TYPE by the validator. Never silently
                      passed through. This file ships EMPTY at scaffold time;
                      Phase 2 fills it via the FAIL_UNKNOWN_ROOM_TYPE harvest
                      loop (scrape with empty map -> every cell hits
                      FAIL_UNKNOWN_ROOM_TYPE -> human reads captured marketing
                      names -> fills the canonical map -> re-scrape clean).
                      The friction is the safety mechanism that catches LLM
                      hallucination — do not skip it.

NON_BAR_LABEL_TOKENS — rate-plan labels that disqualify a row from being
                       picked as the canonical BAR. Used by normalize.py.
"""

from __future__ import annotations
from typing import Optional


# =============================================================================
# Room-type canonical map
# =============================================================================
# Source: SFOEM rate-page captures via Claude in Chrome, 2026-05-10.
# Subject: hr_embarcadero (Hyatt Regency SF Embarcadero, hotel_code sfors).
# Channels covered: direct, booking, hotels_com.
#
# 43 verbatim marketing-string mappings -> 18 canonical SKUs:
#   hr_king, hr_2queen                                  (standard)
#   hr_king_bay, hr_2queen_bay,
#   hr_king_bay_balcony, hr_king_bay_balcony_high,
#   hr_2queen_city_balcony, hr_king_water               (view)
#   hr_king_club, hr_2double_club                       (club)
#   hr_ada_king_tub, hr_ada_king_shower,
#   hr_ada_2queen_tub, hr_ada_2queen_shower             (ADA)
#   hr_suite_bay_studio, hr_suite_balcony,
#   hr_suite_luxury, hr_suite_presidential              (suite)
#
# Notes:
#   * Water View / Ferry View resolve to the same canonical (hr_king_water).
#     Hotels.com markets this SKU as "Ferry View"; Hyatt + Booking call it
#     "Water View". Same physical inventory.
#   * 3 suite SKUs (hr_suite_balcony, hr_suite_luxury, hr_suite_presidential)
#     only surface on Hyatt Direct. Booking + Hotels.com don't list them.
#     Hyatt-only data for those SKUs is acceptable per F41 framework.
#   * Club room marketing on Hotels.com appends "(Regency Club Breakfast)"
#     as part of the room name (not a rate-plan suffix). Captured verbatim
#     so lookup_canonical_room() resolves without normalization.
ROOM_TYPE_CANONICAL: dict[str, str] = {
    # ---- Hyatt Direct (channel: direct) ----
    "1 King Bed":                                           "hr_king",
    "2 Queen Beds":                                         "hr_2queen",
    "1 King Bed Bay View":                                  "hr_king_bay",
    "2 Queen Beds Bay View":                                "hr_2queen_bay",
    "1 King Bed Bay View Balcony":                          "hr_king_bay_balcony",
    "1 King Bay View High Floor Balcony":                   "hr_king_bay_balcony_high",
    "2 Queen Beds City View Balcony":                       "hr_2queen_city_balcony",
    "1 King Water View":                                    "hr_king_water",
    "1 King Bed Club Access":                               "hr_king_club",
    "2 Double Beds Club Access":                            "hr_2double_club",
    "Accessible 1 King Bed with Tub":                       "hr_ada_king_tub",
    "Accessible 1 King Bed with Shower":                    "hr_ada_king_shower",
    "Accessible 2 Queen Beds with Tub":                     "hr_ada_2queen_tub",
    "Accessible 2 Queen Beds with Shower":                  "hr_ada_2queen_shower",
    "Bay View Studio Suite":                                "hr_suite_bay_studio",
    "Balcony Suite":                                        "hr_suite_balcony",
    "Luxury Suite":                                         "hr_suite_luxury",
    "Presidential Suite":                                   "hr_suite_presidential",
    # ---- Booking.com (channel: booking) ----
    "King Room":                                            "hr_king",
    "Queen Room with Two Queen Beds":                       "hr_2queen",
    "King Room with Bay View":                              "hr_king_bay",
    "King Room with Balcony and Bay View":                  "hr_king_bay_balcony",
    "King Room with Balcony and Bay View - High Floor":    "hr_king_bay_balcony_high",
    "Two Queens Room with Balcony and City View":           "hr_2queen_city_balcony",
    "King Room with Water View":                            "hr_king_water",
    "King Room - Club Access":                              "hr_king_club",
    "Double Room with Two Double Beds - Club Access":       "hr_2double_club",
    "King Room with Accessible Tub - Disability Access":    "hr_ada_king_tub",
    "Queen Room with Two Queen Beds and Accessible Tub":    "hr_ada_2queen_tub",
    "Queen Room with Two Queen Beds and Accessible Shower": "hr_ada_2queen_shower",
    # ---- Hotels.com (channel: hotels_com) ----
    "Room, 1 King Bed":                                     "hr_king",
    "Room, 2 Queen Beds":                                   "hr_2queen",
    "Room, 1 King Bed, Bay View":                           "hr_king_bay",
    "Room, 1 King Bed, Balcony, Bay View":                  "hr_king_bay_balcony",
    "Room, 1 King Bed, Balcony, Bay View (High Floor)":     "hr_king_bay_balcony_high",
    "Room, 2 Queen Beds, Balcony, City View":               "hr_2queen_city_balcony",
    "Room, 1 King Bed (Ferry View)":                        "hr_king_water",
    "Club Room, 1 King Bed (Regency Club Breakfast)":       "hr_king_club",
    "Club Room, 2 Double Beds (Regency Club Breakfast)":    "hr_2double_club",
    "Room, 1 King Bed, Accessible, Bathtub":                "hr_ada_king_tub",
    "Room, 2 Queen Beds, Accessible, Bathtub":              "hr_ada_2queen_tub",
    "Room, 2 Queen Beds, Accessible (Shower)":              "hr_ada_2queen_shower",
    "Studio, Bay View":                                     "hr_suite_bay_studio",
}


KNOWN_HALLUCINATED_LABELS: tuple[str, ...] = ()


def lookup_canonical_room(marketing_name: Optional[str]) -> Optional[str]:
    """Return canonical SKU ID for `marketing_name`, or None if unmapped.

    None means FAIL_UNKNOWN_ROOM_TYPE — caller is responsible for surfacing
    the failure (do NOT silently pass through). Comparison is verbatim
    against the dict; no normalization is applied because the validator's
    job is to catch label drift, and silently normalizing defeats it.
    """
    if not marketing_name:
        return None
    return ROOM_TYPE_CANONICAL.get(marketing_name.strip())


# =============================================================================
# Rate-plan canonical taxonomy
# =============================================================================
RATE_PLAN_CANONICAL_VALUES: tuple[str, ...] = (
    "BAR_NON_REF",
    "BAR_FLEX",
    "BUNDLED_PARKING_NON_REF",
    "BUNDLED_PARKING_FLEX",
    "MEMBER_OR_CARD_OFFER",
)


NON_BAR_LABEL_TOKENS: tuple[str, ...] = (
    "non-refund", "non refund", "nonrefund",
    "prepay", "pay now",
    "advance purchase",
    "member-only", "member only", "genius",
    "marriott bonvoy", "hilton honors", "world of hyatt",
    "visa offer", "mastercard offer", "amex offer",
    "mobile-only", "mobile only", "mobile app", "app-only", "app only", "app deal",
    "aaa ", "aaa rate", "aarp", "senior ", "government rate", "gov rate",
    "family pack", "package", "opaque",
)
