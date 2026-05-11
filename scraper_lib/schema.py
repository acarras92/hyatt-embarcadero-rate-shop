"""Canonical raw_rates.csv schema (v2, landed 2026-04-26).

Schema versioning
-----------------
v1 — original 28-column header captured by scrape.py through 2026-04-25.
v2 — adds 9 columns that:
       (a) canonicalize room-type and rate-plan identity for cross-channel join
       (b) separate the three discount sources (strikethrough, promo banner,
           weekly disclosure) into their own columns

       Migration of legacy v1 rows: new columns blank. New scrapes populate.

Key invariant
-------------
The discount-taxonomy columns must NEVER be collapsed into a single
`max_discount_stack` field. The 2026-04-26 Chrome verification proved that
conflating premium-spread (cancellation/pay-timing) with public discount
banners produces phantom findings ("Hotels.com is the deeper-discount
channel" was a measurement artifact). Every discount source gets its own
column so cross-channel parity can verify like-for-like.
"""

from __future__ import annotations
from typing import Any

# -----------------------------------------------------------------------------
# v1 columns — legacy; still emitted by every row
# -----------------------------------------------------------------------------
LEGACY_COLUMNS: list[str] = [
    "property_id", "channel", "arrival_date", "nights",
    "scraped_property_name", "scraped_marketing_name", "sub_description",
    "bed_config", "occupancy_max",
    "rate_plan_label", "rate_per_night_usd", "total_stay_usd",
    "refundable", "is_genius_member_rate", "includes_breakfast",
    "rate_plan_confidence", "availability_status",
    "is_bar", "mapped_internal_code", "mapped_tier", "mapped_view",
    "mapped_bedrooms", "mapped_ada", "mapping_source",
    "status", "missed_patterns", "scrape_timestamp_utc", "source_url",
]

# -----------------------------------------------------------------------------
# v2 NEW columns
# -----------------------------------------------------------------------------
NEW_COLUMNS: list[str] = [
    # ---- canonicalization ----
    "room_type_canonical",      # str  e.g. "1BR_PLATINUM_HSV"; null if scraped_marketing_name not in canonical_maps.ROOM_TYPE_CANONICAL → triggers FAIL_UNKNOWN_ROOM_TYPE
    "rate_plan_canonical",      # str  "BAR_NON_REF" | "BAR_FLEX" | "BUNDLED_PARKING_NON_REF" | "BUNDLED_PARKING_FLEX" | "MEMBER_OR_CARD_OFFER" | null

    # ---- discount taxonomy (one column per source-of-discount) ----
    "strikethrough_orig_rate",  # decimal  pre-discount rate when shown struck (null = no strikethrough)
    "strikethrough_pct",        # decimal  (orig - shown) / orig
    "promo_banner_text",        # str  verbatim banner text (e.g. "15% off", "Pay Now and Save up to 15pct")
    "promo_banner_pct",         # decimal  parsed % from banner (null when banner is non-numeric, e.g. "reduced weekly rate")
    "weekly_rate_disclosure",   # str  Booking-style "Weekly rate - $X. You're getting a reduced weekly rate..." copy when present
    "member_rate_gated",        # bool true when a "sign in to save X%" gate is shown but the gated rate is not visible

    # ---- card-issuer / member offer copy (Phase A2.4 #6, 2026-04-26) ----
    # Verbatim text of any Visa / Mastercard / Amex / sign-in-gated offer
    # in the markdown. Held separately from promo_banner_text so the
    # canonical posted promo (Pay Now and Save / generic % off) is never
    # contaminated by card-issuer copy. If multiple card-offer fragments
    # appear, the first match wins.
    "member_or_card_offer_text",

    # ---- bundle disclosure (separated from rate plan so BAR_BARE can be picked deterministically) ----
    "bundle_inclusions",        # str  comma-list e.g. "parking", "parking,wifi"; null = bare (no inclusions)

    # ---- penthouse variant (mapped_internal_code stays "PH" so existing
    # ROOM_ORDER aggregations keep working; this column carries the
    # 1BR/2BR split for future analyses that want it). null for non-PH rows.
    "penthouse_variant",
]

RAW_HEADER: list[str] = LEGACY_COLUMNS + NEW_COLUMNS

# Empty defaults emitted by writers when a new column has no value.
# csv.DictWriter renders None as empty string — match that everywhere.
NEW_COLUMN_DEFAULTS: dict[str, Any] = {col: None for col in NEW_COLUMNS}

# Schema version stamp. Bump on any column change. Validators / migration
# scripts can branch on this when they need to know what shape a CSV is in.
SCHEMA_VERSION: str = "v2"
