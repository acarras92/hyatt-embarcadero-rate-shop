# Analysis 1 — AKA Full Rate Matrix

Source: `raw_rates.csv`, filtered to property=aka_white_house, LOS=1, is_bar=True, rate>0.
Rooms with multiple matched rows per cell collapsed by selecting the lowest-priced non-ADA BAR.

## Coverage by (channel × room category)

| Room | Direct | Hotels.com | Booking | Total |
|---|---:|---:|---:|---:|
| CMP_0BR_tier_1_entry (CMP_0BR_tier_1_entry) | 7/7 | 0/7 | 0/7 | 7/21 |
| CMP_0BR_tier_2_view (CMP_0BR_tier_2_view) | 7/7 | 0/7 | 0/7 | 7/21 |
| CMP_0BR_tier_4_premier (CMP_0BR_tier_4_premier) | 7/7 | 0/7 | 0/7 | 7/21 |
| CMP_0BR_tier_5_penthouse (CMP_0BR_tier_5_penthouse) | 5/7 | 0/7 | 0/7 | 5/21 |

## Per-room rate-card range (across all dates × channels)

| Room | n cells | min | max | mean | median |
|---|---:|---:|---:|---:|---:|
| CMP_0BR_tier_1_entry (CMP_0BR_tier_1_entry) | 7 | $289 | $319 | $293 | $289 |
| CMP_0BR_tier_2_view (CMP_0BR_tier_2_view) | 7 | $309 | $369 | $318 | $309 |
| CMP_0BR_tier_4_premier (CMP_0BR_tier_4_premier) | 7 | $569 | $739 | $663 | $639 |
| CMP_0BR_tier_5_penthouse (CMP_0BR_tier_5_penthouse) | 5 | $1,069 | $1,239 | $1,133 | $1,109 |

## Direct channel — BAR by date × room

| Room | 2026-05-17 | 2026-05-24 | 2026-05-31 | 2026-06-07 | 2026-06-21 | 2026-06-28 | 2026-07-05 |
|---|---:|---:|---:|---:|---:|---:|---:|
| CMP_0BR_tier_1_entry (CMP_0BR_tier_1_entry) | $289 | $289 | $289 | $289 | $289 | $289 | $319 |
| CMP_0BR_tier_2_view (CMP_0BR_tier_2_view) | $309 | $309 | $309 | $309 | $311 | $309 | $369 |
| CMP_0BR_tier_4_premier (CMP_0BR_tier_4_premier) | $609 | $609 | $639 | $739 | $739 | $739 | $569 |
| CMP_0BR_tier_5_penthouse (CMP_0BR_tier_5_penthouse) | $1,109 | $1,109 | $1,139 | — | $1,239 | — | $1,069 |

## Hotels.com channel — BAR by date × room

| Room | 2026-05-17 | 2026-05-24 | 2026-05-31 | 2026-06-07 | 2026-06-21 | 2026-06-28 | 2026-07-05 |
|---|---:|---:|---:|---:|---:|---:|---:|
| CMP_0BR_tier_1_entry (CMP_0BR_tier_1_entry) | — | — | — | — | — | — | — |
| CMP_0BR_tier_2_view (CMP_0BR_tier_2_view) | — | — | — | — | — | — | — |
| CMP_0BR_tier_4_premier (CMP_0BR_tier_4_premier) | — | — | — | — | — | — | — |
| CMP_0BR_tier_5_penthouse (CMP_0BR_tier_5_penthouse) | — | — | — | — | — | — | — |

## Booking channel — BAR by date × room

| Room | 2026-05-17 | 2026-05-24 | 2026-05-31 | 2026-06-07 | 2026-06-21 | 2026-06-28 | 2026-07-05 |
|---|---:|---:|---:|---:|---:|---:|---:|
| CMP_0BR_tier_1_entry (CMP_0BR_tier_1_entry) | — | — | — | — | — | — | — |
| CMP_0BR_tier_2_view (CMP_0BR_tier_2_view) | — | — | — | — | — | — | — |
| CMP_0BR_tier_4_premier (CMP_0BR_tier_4_premier) | — | — | — | — | — | — | — |
| CMP_0BR_tier_5_penthouse (CMP_0BR_tier_5_penthouse) | — | — | — | — | — | — | — |

## Mean BAR per date (across channels with data)

| Room | 2026-05-17 | 2026-05-24 | 2026-05-31 | 2026-06-07 | 2026-06-21 | 2026-06-28 | 2026-07-05 |
|---|---:|---:|---:|---:|---:|---:|---:|
| CMP_0BR_tier_1_entry (CMP_0BR_tier_1_entry) | $289 | $289 | $289 | $289 | $289 | $289 | $319 |
| CMP_0BR_tier_2_view (CMP_0BR_tier_2_view) | $309 | $309 | $309 | $309 | $311 | $309 | $369 |
| CMP_0BR_tier_4_premier (CMP_0BR_tier_4_premier) | $609 | $609 | $639 | $739 | $739 | $739 | $569 |
| CMP_0BR_tier_5_penthouse (CMP_0BR_tier_5_penthouse) | $1,109 | $1,109 | $1,139 | — | $1,239 | — | $1,069 |

## Long-format raw export (first 50 rows shown)

| channel | arrival_date | room_code | room_name | rate_usd |
|---|---|---|---|---:|
| direct | 2026-05-17 | CMP_0BR_tier_1_entry | CMP_0BR_tier_1_entry | $289 |
| direct | 2026-05-17 | CMP_0BR_tier_2_view | CMP_0BR_tier_2_view | $309 |
| direct | 2026-05-17 | CMP_0BR_tier_4_premier | CMP_0BR_tier_4_premier | $609 |
| direct | 2026-05-17 | CMP_0BR_tier_5_penthouse | CMP_0BR_tier_5_penthouse | $1,109 |
| direct | 2026-05-24 | CMP_0BR_tier_1_entry | CMP_0BR_tier_1_entry | $289 |
| direct | 2026-05-24 | CMP_0BR_tier_2_view | CMP_0BR_tier_2_view | $309 |
| direct | 2026-05-24 | CMP_0BR_tier_4_premier | CMP_0BR_tier_4_premier | $609 |
| direct | 2026-05-24 | CMP_0BR_tier_5_penthouse | CMP_0BR_tier_5_penthouse | $1,109 |
| direct | 2026-05-31 | CMP_0BR_tier_1_entry | CMP_0BR_tier_1_entry | $289 |
| direct | 2026-05-31 | CMP_0BR_tier_2_view | CMP_0BR_tier_2_view | $309 |
| direct | 2026-05-31 | CMP_0BR_tier_4_premier | CMP_0BR_tier_4_premier | $639 |
| direct | 2026-05-31 | CMP_0BR_tier_5_penthouse | CMP_0BR_tier_5_penthouse | $1,139 |
| direct | 2026-06-07 | CMP_0BR_tier_1_entry | CMP_0BR_tier_1_entry | $289 |
| direct | 2026-06-07 | CMP_0BR_tier_2_view | CMP_0BR_tier_2_view | $309 |
| direct | 2026-06-07 | CMP_0BR_tier_4_premier | CMP_0BR_tier_4_premier | $739 |
| direct | 2026-06-21 | CMP_0BR_tier_1_entry | CMP_0BR_tier_1_entry | $289 |
| direct | 2026-06-21 | CMP_0BR_tier_2_view | CMP_0BR_tier_2_view | $311 |
| direct | 2026-06-21 | CMP_0BR_tier_4_premier | CMP_0BR_tier_4_premier | $739 |
| direct | 2026-06-21 | CMP_0BR_tier_5_penthouse | CMP_0BR_tier_5_penthouse | $1,239 |
| direct | 2026-06-28 | CMP_0BR_tier_1_entry | CMP_0BR_tier_1_entry | $289 |
| direct | 2026-06-28 | CMP_0BR_tier_2_view | CMP_0BR_tier_2_view | $309 |
| direct | 2026-06-28 | CMP_0BR_tier_4_premier | CMP_0BR_tier_4_premier | $739 |
| direct | 2026-07-05 | CMP_0BR_tier_1_entry | CMP_0BR_tier_1_entry | $319 |
| direct | 2026-07-05 | CMP_0BR_tier_2_view | CMP_0BR_tier_2_view | $369 |
| direct | 2026-07-05 | CMP_0BR_tier_4_premier | CMP_0BR_tier_4_premier | $569 |
| direct | 2026-07-05 | CMP_0BR_tier_5_penthouse | CMP_0BR_tier_5_penthouse | $1,069 |

Total long-format rows: **26**.
