"""
analytics_lighthouse.py — Pure analytics over the Lighthouse long-format DF.

Consumed by build_dashboard.py to produce the `lighthouse` and `derived`
namespaces in window.DASHBOARD_DATA.

Contract:
- All functions are pure: take a pandas DataFrame, return JSON-serializable
  dicts/lists. No mutation of inputs, no I/O.
- Park Central is asserted absent at the call site (build_dashboard.py)
  before any function here runs.
- Each verdict-rule function emits n_total, n_used, exclusions{...}, and
  low_power flag — never silently drop rows.

Verdict math (revised 2026-05-05 after Codex review #1.3):
- DYNAMIC requires all 3 rules passing with measurable data (no skips).
- PARTIALLY_DYNAMIC if 1 <= n_passed < 3 OR any rule is n/a.
- STATIC if n_passed == 0.
- Rationale: Rule 3 (compression lead-time) is the most diagnostic of RM
  per the Gopu transcript; skipping it cannot yield a DYNAMIC verdict.
- Rule 3 remains n/a until a multi-pull stack of Lighthouse exports lands.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUBJECT_PROPERTY = "hr_embarcadero"
LH_COMP_SET = ["hr_embarcadero", "hr_soma", "clancy",
               "hilton_us", "grand_hyatt", "palace", "marquis", "ic_sf", "st_francis"]

# Display-name map mirrored in dashboard.js COMPSET_PROP_LABEL — kept in sync
# manually. Used by compute_lighthouse_rates_explorer to project the raw
# property slugs into IC-readable labels for the raw data explorer (Section 6
# in the current dashboard layout; renumbered through 8 → 7 → 6 across the
# 2026-05-07 channel-parity-section + Penthouse-section cleanup passes).
LH_PROPERTY_DISPLAY = {
    "hr_embarcadero": "Hyatt Regency SF Embarcadero",
    "hr_soma": "Hyatt Regency SF SoMa",
    "clancy": "The Clancy",
    "hilton_us": "Hilton SF Union Square",
    "grand_hyatt": "Grand Hyatt SF",
    "palace": "Palace Hotel",
    "marquis": "Marriott Marquis SF",
    "ic_sf": "InterContinental SF",
    "st_francis": "Westin St. Francis",
}

# Verdict rule thresholds.
# Rule 1 (demand_response) is gated on two sub-rules:
#   1a — Pearson r of (rate level, Market demand) on 60-day forward window.
#   1b — Pearson r of (Δrate, ΔOTB) on consecutive-day pairs across 365 days.
# 1b is FIRST-DIFFERENCES, which controls for the shared seasonality / trend
# artifact that contaminates raw-level OTB correlation. Tests Gopu's
# "reactive vs proactive" thesis honestly.
#
# 1b_levels (raw-level OTB correlation, what 1b previously was) is retained
# as INFORMATIONAL — surfaced in the verdict object but NOT gated. If 1b_levels
# is high while 1b first-differences is low, that's evidence of trend-following
# without tactical yielding (e.g., a property that price-anchors to a seasonal
# pattern but doesn't tactically respond to inventory moves).
DEMAND_CORRELATION_1A_THRESHOLD = 0.30
DEMAND_CORRELATION_1B_THRESHOLD = 0.10        # first-differences — noisier than levels
DEMAND_CORRELATION_1B_LEVELS_THRESHOLD = 0.20  # informational only; not gated
DEMAND_CORRELATION_1A_LOW_POWER_N = 30   # only ~60 dates have market_demand_frac populated
DEMAND_CORRELATION_1B_LOW_POWER_N = 100  # 365 dates available for OTB; consecutive-day pairs ~ ≤364
DOW_NORMALIZED_RANGE_THRESHOLD = 0.10
COMPRESSION_LEAD_TIME_THRESHOLD = 14
LOW_POWER_WEEKS = 30

HIGH_DEMAND_THRESHOLD = 0.90  # market_demand_frac for "days_high_demand_count" KPI

# GM tour reference number — view-premium claim that the dashboard tests against.
VIEW_PREMIUM_GM_CLAIMED_USD = 0.0

# Lighthouse-export filter constants (Finding 34, SFOEM retemplate 2026-05-12).
# AKA's analytics module hardcoded source='brandcom' and a subject/comp tier
# asymmetry (subject=suite, comp=any). SFOEM's Lighthouse export uses a single
# channel (booking) for all properties at one room_tier ('any'), so all 4
# constants collapse to the same value. The subject/comp split is preserved
# here so a future deal with AKA-style asymmetry can re-introduce it without
# another retemplate. Skill back-port should make these per-deal config.
LH_SUBJECT_SOURCE = "booking"
LH_SUBJECT_ROOM_TIER = "any"
LH_COMP_SOURCE = "booking"
LH_COMP_ROOM_TIER = "any"


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------

def _aka_brandcom_suite(lh: pd.DataFrame) -> pd.DataFrame:
    """All AKA Brand.com Suite rows (any availability)."""
    return lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"] == LH_SUBJECT_ROOM_TIER)
        & (lh["property"] == SUBJECT_PROPERTY)
    ].copy()


def _exclusions_breakdown(df: pd.DataFrame) -> Dict[str, int]:
    """Count rows in each non-available status. Helpful for low-power diagnostics."""
    if df.empty:
        return {"sold_out": 0, "los_restricted": 0, "room_na": 0,
                "blank": 0, "not_loaded": 0, "no_flex": 0,
                "third_party_only": 0, "one_guest_only": 0}
    s = df["availability_status"]
    return {
        "sold_out": int((s == "sold_out").sum()),
        "los_restricted": int((s == "los_restricted").sum()),
        "room_na": int((s == "room_na").sum()),
        "blank": int((s == "blank").sum()),
        "not_loaded": int((s == "not_loaded").sum()),
        "no_flex": int((s == "no_flex").sum()),
        "third_party_only": int((s == "third_party_only").sum()),
        "one_guest_only": int((s == "one_guest_only").sum()),
    }


# ---------------------------------------------------------------------------
# Verdict rule computations (used by compute_rm_verdict + headline KPIs)
# ---------------------------------------------------------------------------

def _compute_correlation_subrule(
    lh: pd.DataFrame,
    subject: str,
    metric_col: str,
    threshold: float,
    low_power_n: int,
    label: str,
    horizon_note: str,
) -> Dict:
    """Helper for a single Pearson-r sub-rule of (subject Brand.com Suite rate, metric_col).

    Filter: source='brandcom' AND room_tier='suite' AND property=subject AND
    availability_status='available' AND `metric_col` is not null AND
    rate_usd is not null. (rate_usd guard prevents NaN-r and n_used over-count
    when an `available` row lacks a price — defensive.)
    """
    df = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"] == LH_SUBJECT_ROOM_TIER)
        & (lh["property"] == subject)
    ].copy()
    n_total = len(df)
    excl = _exclusions_breakdown(df)
    df = df[
        (df["availability_status"] == "available")
        & df[metric_col].notna()
        & df["rate_usd"].notna()
    ]
    n_used = len(df)

    if n_used < 2 or df["rate_usd"].std() == 0 or df[metric_col].std() == 0:
        return {
            "label": label, "horizon_note": horizon_note,
            "value": None, "threshold": threshold,
            "pass": False, "n_total": n_total, "n_used": n_used,
            "exclusions": excl, "low_power": True,
            "note": f"insufficient variance or n_used={n_used}<2",
        }

    r = float(df["rate_usd"].corr(df[metric_col]))
    return {
        "label": label, "horizon_note": horizon_note,
        "value": r,
        "threshold": threshold,
        "pass": bool(r > threshold),
        "n_total": n_total, "n_used": n_used,
        "exclusions": excl,
        "low_power": bool(n_used < low_power_n),
        "note": (f"low effective n (autocorrelation guard, threshold {low_power_n})"
                 if n_used < low_power_n else ""),
    }


def _compute_first_differences_subrule(
    lh: pd.DataFrame,
    subject: str,
    metric_col: str,
    threshold: float,
    low_power_n: int,
    label: str,
    horizon_note: str,
) -> Dict:
    """Pearson r of (Δsubject_rate, Δmetric) over consecutive-day pairs only.

    Filter: source='brandcom' AND room_tier='suite' AND property=subject AND
    availability_status='available' AND `metric_col` is not null AND
    rate_usd is not null. Pairs are formed only when day i and day i+1 are both
    in the filtered set (no gaps). First-differences control for shared
    seasonality / trend artifact.
    """
    df = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"] == LH_SUBJECT_ROOM_TIER)
        & (lh["property"] == subject)
    ].copy()
    n_total = len(df)
    excl = _exclusions_breakdown(df)
    df = df[
        (df["availability_status"] == "available")
        & df[metric_col].notna()
        & df["rate_usd"].notna()
    ].copy()
    if df.empty:
        return {
            "label": label, "horizon_note": horizon_note,
            "value": None, "threshold": threshold,
            "pass": False, "n_total": n_total, "n_used": 0,
            "exclusions": excl, "low_power": True,
            "note": "no available rows after filter",
        }
    df["arrival_date"] = pd.to_datetime(df["arrival_date"])
    df = df.sort_values("arrival_date").reset_index(drop=True)
    df["delta_days"] = df["arrival_date"].diff().dt.days
    df["d_rate"] = df["rate_usd"].diff()
    df["d_metric"] = df[metric_col].diff()
    pairs = df[df["delta_days"] == 1].copy()
    n_used = len(pairs)
    if n_used < 2 or pairs["d_rate"].std() == 0 or pairs["d_metric"].std() == 0:
        return {
            "label": label, "horizon_note": horizon_note,
            "value": None, "threshold": threshold,
            "pass": False, "n_total": n_total, "n_used": n_used,
            "exclusions": excl, "low_power": True,
            "note": f"insufficient variance or n_used={n_used}<2",
        }
    r = float(pairs["d_rate"].corr(pairs["d_metric"]))
    return {
        "label": label, "horizon_note": horizon_note,
        "value": r, "threshold": threshold,
        "pass": bool(r > threshold),
        "n_total": n_total, "n_used": n_used,
        "exclusions": excl,
        "low_power": bool(n_used < low_power_n),
        "note": (f"low effective n (threshold {low_power_n})"
                 if n_used < low_power_n else ""),
    }


def _compute_demand_response(lh: pd.DataFrame, subject: str = SUBJECT_PROPERTY) -> Dict:
    """Rule 1 — demand response. Gated on (1a AND 1b first-differences).

    Three sub-rules surface in the verdict object:
      - 1a: rate level vs Market demand on 60-day forward window (gated).
      - 1b: Δrate vs ΔOTB on consecutive-day pairs across 365 days (gated;
            controls for trend / seasonality artifact).
      - 1b_levels: raw-level OTB correlation (informational only; NOT gated).
        High 1b_levels with low 1b first-differences flags trend-following
        without tactical yielding.
    """
    sub_1a = _compute_correlation_subrule(
        lh, subject, metric_col="market_demand_frac",
        threshold=DEMAND_CORRELATION_1A_THRESHOLD,
        low_power_n=DEMAND_CORRELATION_1A_LOW_POWER_N,
        label="1a_demand_correlation_60d",
        horizon_note="60-day forward window (Lighthouse Market demand populated only ~60d ahead)",
    )
    sub_1b = _compute_first_differences_subrule(
        lh, subject, metric_col="market_otb_frac",
        threshold=DEMAND_CORRELATION_1B_THRESHOLD,
        low_power_n=DEMAND_CORRELATION_1B_LOW_POWER_N,
        label="1b_otb_first_differences_365d",
        horizon_note=("365-day forward window, first-differences "
                      "(Δrate vs ΔOTB on consecutive-day pairs; controls for trend)"),
    )
    sub_1b_levels = _compute_correlation_subrule(
        lh, subject, metric_col="market_otb_frac",
        threshold=DEMAND_CORRELATION_1B_LEVELS_THRESHOLD,
        low_power_n=DEMAND_CORRELATION_1B_LOW_POWER_N,
        label="1b_levels_otb_correlation_365d",
        horizon_note=("365-day forward window, raw levels (informational; "
                      "not gated; high here + low 1b ⇒ trend-following without tactical yield)"),
    )
    rule_pass = bool(sub_1a["pass"] and sub_1b["pass"])
    return {
        "sub_rules": {"1a": sub_1a, "1b": sub_1b, "1b_levels": sub_1b_levels},
        "pass": rule_pass,
        "rationale": (
            "Rule 1 = pass requires BOTH 1a and 1b. 1a tests reactive demand "
            "response on the 60-day window; 1b uses first-differences to test "
            "proactive OTB response while controlling for shared seasonality. "
            "1b_levels (raw-level OTB correlation) is informational only — "
            "high 1b_levels with low 1b indicates calendar trend-following "
            "rather than tactical inventory-driven yielding."
        ),
    }


def _compute_dow_normalized_range(lh: pd.DataFrame, subject: str = SUBJECT_PROPERTY) -> Dict:
    """Rule 2 — DOW normalized range.

    Filter: source='brandcom' AND room_tier='suite' AND property=subject AND
    availability_status='available' AND rate_usd is not null.
    For each ISO week: range = max(rate)-min(rate); ratio = range/mean(rate).
    Apply 4-week rolling mean of ratio; report median.
    """
    df = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"] == LH_SUBJECT_ROOM_TIER)
        & (lh["property"] == subject)
    ].copy()
    n_total = len(df)
    excl = _exclusions_breakdown(df)
    df = df[(df["availability_status"] == "available") & df["rate_usd"].notna()]
    n_used = len(df)

    if n_used == 0:
        return {
            "value": None, "threshold": DOW_NORMALIZED_RANGE_THRESHOLD,
            "pass": False, "n_total": n_total, "n_used": n_used,
            "exclusions": excl, "low_power": True,
            "note": "no available rows",
        }

    df["arrival_date"] = pd.to_datetime(df["arrival_date"])
    iso = df["arrival_date"].dt.isocalendar()
    df["iso_year"] = iso["year"]
    df["iso_week"] = iso["week"]

    weekly = df.groupby(["iso_year", "iso_week"]).agg(
        n_dow=("dow", "nunique"),
        rate_max=("rate_usd", "max"),
        rate_min=("rate_usd", "min"),
        rate_mean=("rate_usd", "mean"),
    ).reset_index()
    weekly = weekly.sort_values(["iso_year", "iso_week"]).reset_index(drop=True)
    weekly["range"] = weekly["rate_max"] - weekly["rate_min"]
    weekly["ratio"] = weekly.apply(
        lambda r: (r["range"] / r["rate_mean"]) if r["rate_mean"] > 0 else math.nan,
        axis=1,
    )
    weeks_with_full_dow = int((weekly["n_dow"] >= 5).sum())

    rolling = weekly["ratio"].rolling(window=4, min_periods=2).mean()
    if rolling.dropna().empty:
        return {
            "value": None, "threshold": DOW_NORMALIZED_RANGE_THRESHOLD,
            "pass": False, "n_total": n_total, "n_used": n_used,
            "exclusions": excl, "low_power": True,
            "note": "no weeks with ≥2 observations for 4-week rolling mean",
        }
    value = float(rolling.median())

    return {
        "value": value,
        "threshold": DOW_NORMALIZED_RANGE_THRESHOLD,
        "pass": bool(value > DOW_NORMALIZED_RANGE_THRESHOLD),
        "n_total": n_total, "n_used": n_used,
        "exclusions": excl,
        "low_power": bool(weeks_with_full_dow < LOW_POWER_WEEKS),
        "note": (f"low coverage ({weeks_with_full_dow} weeks have ≥5 DOW; "
                 f"threshold {LOW_POWER_WEEKS})"
                 if weeks_with_full_dow < LOW_POWER_WEEKS else ""),
        "weeks_with_full_dow": weeks_with_full_dow,
    }


# ---------------------------------------------------------------------------
# compute_rm_verdict — Section 2 headline
# ---------------------------------------------------------------------------

def compute_rm_verdict(lh: pd.DataFrame, subject: str = SUBJECT_PROPERTY) -> Dict[str, Any]:
    """Build the STATIC / PARTIALLY_DYNAMIC / DYNAMIC verdict for `subject`.

    Rule 3 (compression lead-time) is permanently n/a in this build until a
    multi-pull stack of Lighthouse exports lands (vs. Yesterday / 3-days /
    7-days sheets are deferred — see lighthouse_parser.py header).

    Verdict math: DYNAMIC requires all 3 rules passing with measurable data;
    any n/a caps the verdict at PARTIALLY_DYNAMIC.
    """
    rule1 = _compute_demand_response(lh, subject=subject)
    rule2 = _compute_dow_normalized_range(lh, subject=subject)
    rule3 = {
        "value": None,
        "threshold": COMPRESSION_LEAD_TIME_THRESHOLD,
        "pass": None,
        "n_total": 0, "n_used": 0, "exclusions": {},
        "low_power": True,
        "note": "n/a — booking-window sample insufficient (multi-pull stack required)",
    }

    rules = {
        "demand_response": rule1,
        "dow_normalized_range": rule2,
        "compression_lead_time": rule3,
    }

    n_skipped = sum(1 for r in rules.values() if r["pass"] is None)
    n_observed = 3 - n_skipped
    n_passed = sum(1 for r in rules.values() if r["pass"] is True)

    if n_passed == 3 and n_skipped == 0:
        classification = "DYNAMIC"
    elif n_passed == 0:
        classification = "STATIC"
    else:
        classification = "PARTIALLY_DYNAMIC"

    skip_note = ""
    if rules["compression_lead_time"]["pass"] is None:
        skip_note = "lead-time n/a"
    display_label = (
        f"{classification} ({n_passed} of {n_observed} observed; {n_skipped} skipped"
        + (f"; {skip_note}" if skip_note else "")
        + ")"
    )

    return {
        "classification": classification,
        "n_rules_passed": n_passed,
        "n_rules_observed": n_observed,
        "n_rules_skipped": n_skipped,
        "display_label": display_label,
        "subject": subject,
        "rules": rules,
    }


# ---------------------------------------------------------------------------
# Comp-set verdict comparison — same three rules across all 6 Lighthouse comps
# ---------------------------------------------------------------------------

def compute_comp_set_verdicts(lh: pd.DataFrame) -> Dict[str, Any]:
    """Run compute_rm_verdict for each of the 6 Lighthouse comps and rank by composite.

    Composite = sub_1a + sub_1b + dow_range (raw sum; nulls treated as 0).
    Higher composite ⇒ more yield-disciplined across the three observable axes.

    Returns:
      {
        rows: [{property, classification, display_label,
                sub_1a, sub_1b, sub_1b_levels, dow_range, composite, rank,
                rule1_pass, rule2_pass}, ...]   # sorted by rank ascending
        median_dow_range: float or None,
        composite_formula: str,
      }
    """
    rows: List[Dict[str, Any]] = []
    for prop in LH_COMP_SET:
        v = compute_rm_verdict(lh, subject=prop)
        sub_1a = v["rules"]["demand_response"]["sub_rules"]["1a"].get("value")
        sub_1b = v["rules"]["demand_response"]["sub_rules"]["1b"].get("value")
        sub_1bl = v["rules"]["demand_response"]["sub_rules"]["1b_levels"].get("value")
        dow_range = v["rules"]["dow_normalized_range"].get("value")
        composite = (sub_1a or 0.0) + (sub_1b or 0.0) + (dow_range or 0.0)
        rows.append({
            "property": prop,
            "classification": v["classification"],
            "display_label": v["display_label"],
            "sub_1a": sub_1a,
            "sub_1b": sub_1b,
            "sub_1b_levels": sub_1bl,
            "dow_range": dow_range,
            "composite": composite,
            "rule1_pass": v["rules"]["demand_response"]["pass"],
            "rule2_pass": v["rules"]["dow_normalized_range"]["pass"],
        })
    rows_sorted = sorted(rows, key=lambda r: r["composite"], reverse=True)
    for i, r in enumerate(rows_sorted, 1):
        r["rank"] = i

    dow_values = [r["dow_range"] for r in rows if r["dow_range"] is not None]
    median_dow = float(statistics.median(dow_values)) if dow_values else None

    return {
        "rows": rows_sorted,
        "median_dow_range": median_dow,
        "composite_formula": (
            "1a + 1b first-diff + DOW range (raw sum; nulls treated as 0; "
            "higher = more yield-disciplined across the three observable axes)"
        ),
    }


# ---------------------------------------------------------------------------
# Section 1 — Forward demand environment
# ---------------------------------------------------------------------------

def compute_market_demand_heatmap(lh: pd.DataFrame) -> List[Dict[str, Any]]:
    """365d × Market demand for Section 1.

    Pulls one canonical (source, room_tier) — Brand.com any — to avoid duplicating
    the demand series. Returns one row per arrival_date with demand and OTB.
    """
    df = lh[(lh["source"] == LH_SUBJECT_SOURCE) & (lh["room_tier"] == LH_COMP_ROOM_TIER)].copy()
    out = (df.drop_duplicates("arrival_date")
             .sort_values("arrival_date")
             [["arrival_date", "dow", "market_demand_frac", "market_otb_frac"]]
             .to_dict(orient="records"))
    for r in out:
        r["arrival_date"] = str(r["arrival_date"])
    return out


# ---------------------------------------------------------------------------
# Section 2 sub-panels
# ---------------------------------------------------------------------------

def compute_dow_pattern(lh: pd.DataFrame, subject: str = SUBJECT_PROPERTY) -> List[Dict[str, Any]]:
    """Weeks × DOW × subject Brand.com Suite available rate.

    Filter: source='brandcom', room_tier='suite', property=subject, available only,
    rate_usd not null (defensive — prevents NaN leak into the JSON payload).
    """
    df = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"] == LH_SUBJECT_ROOM_TIER)
        & (lh["property"] == subject)
        & (lh["availability_status"] == "available")
        & lh["rate_usd"].notna()
    ].copy()
    if df.empty:
        return []
    df["arrival_date"] = pd.to_datetime(df["arrival_date"])
    iso = df["arrival_date"].dt.isocalendar()
    df["iso_year"] = iso["year"].astype(int)
    df["iso_week"] = iso["week"].astype(int)
    rows = (df.sort_values(["iso_year", "iso_week", "arrival_date"])
              [["iso_year", "iso_week", "arrival_date", "dow", "rate_usd"]]
              .to_dict(orient="records"))
    for r in rows:
        r["arrival_date"] = str(r["arrival_date"].date())
        rv = r["rate_usd"]
        r["rate_usd"] = float(rv) if pd.notna(rv) and math.isfinite(float(rv)) else None
    return rows


def compute_demand_correlation_scatter(
    lh: pd.DataFrame, subject: str = SUBJECT_PROPERTY,
) -> List[Dict[str, Any]]:
    """(market_demand_frac, subject_rate) per arrival_date — Brand.com Suite available."""
    df = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"] == LH_SUBJECT_ROOM_TIER)
        & (lh["property"] == subject)
        & (lh["availability_status"] == "available")
        & lh["market_demand_frac"].notna()
        & lh["rate_usd"].notna()
    ].copy()
    out = []
    for _, r in df.iterrows():
        out.append({
            "arrival_date": str(r["arrival_date"]),
            "demand_frac": float(r["market_demand_frac"]),
            "rate_usd": float(r["rate_usd"]),
        })
    return out


# ---------------------------------------------------------------------------
# Shared dual-baseline architecture — single source of truth for the Δ% used by
# both compute_compression_response_with_comp_baseline (Section 3 compression-
# event table) and compute_yielding_intensity_series (Section 1 yielding-
# intensity line chart). Unifying here means the table and the chart speak the
# same Δ% language.
#
# Replaced the prior 30-day trailing MEAN baseline (2026-05-06) after
# diagnosis: AKA's BAR series is structured as flat plateaus that flip between
# distinct rate-plan price levels (e.g. $176 long-stay-LOS plateau ending →
# $356 plateau starting). A trailing-30 mean reads each plateau transition as
# an +80% "yielding spike" — that's a rate-plan-availability artifact, not
# yielding behavior. The trailing window is also DOW-contaminated (Saturdays
# read positive, Tuesdays read negative regardless of yielding) and
# compression-contaminated (Memorial Day weekend in the trailing window
# depresses the apparent magnitude of July 4 yielding).
#
# Two-part fix:
#   Part 1: DOW-stratified annual MEDIAN baseline. For each DOW, baseline_dow
#           = median rate across all available forward dates of that DOW.
#           Median (not mean) → robust to compression-night outliers.
#   Part 2: Plateau-aware override. When a property is sitting on a detected
#           plateau (≥10 consecutive days within 5% day-over-day), Δ% is
#           forced to 0.0 because the property is anchored, not yielding.
#
# Same plateau threshold powers the flat-stretch annotations on the quarterly
# panels (compute_compset_flat_stretches) — one source of truth for "what is
# a plateau" across the dashboard.
# ---------------------------------------------------------------------------
PLATEAU_MIN_LENGTH_DAYS = 10
PLATEAU_MAX_DOD_PCT = 0.05


def detect_plateaus(
    rate_series_by_date: pd.Series,
    min_length_days: int = PLATEAU_MIN_LENGTH_DAYS,
    max_dod_pct: float = PLATEAU_MAX_DOD_PCT,
) -> List[Tuple[pd.Timestamp, pd.Timestamp, float]]:
    """Detect contiguous plateau windows on a date-indexed rate series.

    A plateau is ≥min_length_days consecutive arrival dates where the
    day-over-day fractional rate change stays under max_dod_pct (5% by
    default). Calendar gaps within the run (sold_out / not_loaded days where
    the rate is missing for that property) do NOT break the plateau — those
    are missing observations of the same plateau, not evidence of a different
    rate. A run qualifies when BOTH the calendar span AND the
    available-observation count meet min_length_days.

    Mirrors the threshold and run-detection logic of compute_compset_flat_stretches
    so the quarterly-panel flat-stretch annotations and the yielding-intensity +
    compression-table baseline override share one definition of "plateau."

    Returns: [(start_date, end_date, plateau_median_usd), ...] in chronological
    order. Empty list if input is empty or has no qualifying runs.
    """
    if rate_series_by_date is None or rate_series_by_date.empty:
        return []
    s = rate_series_by_date.dropna().sort_index()
    if s.empty:
        return []
    rates = s.values
    dates = s.index
    n = len(rates)
    out: List[Tuple[pd.Timestamp, pd.Timestamp, float]] = []
    i = 0
    while i < n:
        j = i + 1
        while j < n:
            prev_rate = rates[j - 1]
            curr_rate = rates[j]
            if prev_rate == 0:
                break
            if abs(curr_rate - prev_rate) / abs(prev_rate) >= max_dod_pct:
                break
            j += 1
        n_obs = j - i
        first_date = dates[i]
        last_date = dates[j - 1]
        days_span = (last_date - first_date).days + 1
        if n_obs >= min_length_days and days_span >= min_length_days:
            median_rate = float(pd.Series(rates[i:j]).median())
            out.append((first_date, last_date, median_rate))
        i = max(j, i + 1)
    return out


def _build_dual_baseline_frame(
    rates: pd.Series,
    smoothing_window_days: int = 7,
    plateau_min_length_days: int = PLATEAU_MIN_LENGTH_DAYS,
    plateau_max_dod_pct: float = PLATEAU_MAX_DOD_PCT,
) -> pd.DataFrame:
    """Build dual-baseline Δ% frame for a single property's date-indexed rate
    series.

    Columns: rate, baseline, dow, on_plateau, pct, pct_smoothed.

    baseline = DOW-stratified annual median rate for the property.
    on_plateau = True for dates inside any detect_plateaus() run.
    pct = (rate − baseline) / baseline, forced to 0.0 where on_plateau is True.
    pct_smoothed = `smoothing_window_days`-day rolling mean of pct (min_periods=1
    so leading edges still render). Pass smoothing_window_days=1 to get
    unsmoothed pct (compression table reports per-date Δ% without smoothing).
    """
    cols = ["rate", "baseline", "dow", "on_plateau", "pct", "pct_smoothed"]
    if rates is None or rates.empty:
        return pd.DataFrame(columns=cols)
    s = rates.dropna().astype(float).sort_index()
    if s.empty:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame({"rate": s})
    df["dow"] = df.index.dayofweek
    dow_baselines = df.groupby("dow")["rate"].median().to_dict()
    df["baseline"] = df["dow"].map(dow_baselines).astype(float)

    plateaus = detect_plateaus(
        s,
        min_length_days=plateau_min_length_days,
        max_dod_pct=plateau_max_dod_pct,
    )
    on_plateau = pd.Series(False, index=df.index)
    for start, end, _med in plateaus:
        on_plateau.loc[start:end] = True
    df["on_plateau"] = on_plateau

    safe_baseline = df["baseline"].replace(0, math.nan)
    raw_pct = (df["rate"] - safe_baseline) / safe_baseline
    df["pct"] = raw_pct.where(~df["on_plateau"], 0.0)
    df["pct_smoothed"] = df["pct"].rolling(
        window=smoothing_window_days, min_periods=1,
    ).mean()
    return df[cols]


def compute_compression_response_with_comp_baseline(
    lh: pd.DataFrame, top_n: int = 60, subject: str = SUBJECT_PROPERTY,
    plateau_min_length_days: int = PLATEAU_MIN_LENGTH_DAYS,
    plateau_max_dod_pct: float = PLATEAU_MAX_DOD_PCT,
) -> List[Dict[str, Any]]:
    """Top forward dates by Market demand, with AKA AND comp-set dual-baseline Δ%.

    Baseline architecture (revised 2026-05-06): DOW-stratified annual median +
    plateau-aware override. Identical to compute_yielding_intensity_series so
    the Section 1 chart and this Section 3 table speak the same Δ% language.
    Replaces the prior 30-day trailing MEAN baseline; see the dual-baseline
    block above for diagnosis.

    AKA series: source=brandcom, room_tier='suite', property=subject, available.
    Comp-set series: per-date median across source=brandcom, room_tier='any',
    property in 5-comp set, available — treated as a single time series for
    the dual-baseline computation. (Comps don't all have suite-tier rows so
    'any' is the apples-to-apples cross-property tier.)

    Per-date Δ% in this table is NOT 7-day-smoothed by design — the IC reads
    individual compression dates from this table and shouldn't have neighbor
    days blurring the read. Section 1's chart uses smoothed Δ% by design
    (weekly-cadence visual). Same baseline definition; different smoothing.

    Returns top_n rows sorted by market_demand_frac DESC.
    """
    aka = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"] == LH_SUBJECT_ROOM_TIER)
        & (lh["property"] == subject)
        & (lh["availability_status"] == "available")
        & lh["rate_usd"].notna()
    ].copy()
    if aka.empty:
        return []
    aka["arrival_date"] = pd.to_datetime(aka["arrival_date"])
    aka_series = (aka.drop_duplicates("arrival_date")
                     .set_index("arrival_date")["rate_usd"]
                     .sort_index())
    aka_frame = _build_dual_baseline_frame(
        aka_series,
        smoothing_window_days=1,  # per-date Δ%; no smoothing in the table
        plateau_min_length_days=plateau_min_length_days,
        plateau_max_dod_pct=plateau_max_dod_pct,
    )

    comps = [p for p in LH_COMP_SET if p != subject]
    comp = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"] == LH_COMP_ROOM_TIER)
        & (lh["property"].isin(comps))
        & (lh["availability_status"] == "available")
        & lh["rate_usd"].notna()
    ][["arrival_date", "property", "rate_usd"]].copy()
    comp["arrival_date"] = pd.to_datetime(comp["arrival_date"])
    # Per-date median across the 5 comps; this becomes the comp-set "rate series".
    comp_by_date = (comp.groupby("arrival_date")["rate_usd"]
                        .median()
                        .sort_index())
    comp_frame = _build_dual_baseline_frame(
        comp_by_date,
        smoothing_window_days=1,
        plateau_min_length_days=plateau_min_length_days,
        plateau_max_dod_pct=plateau_max_dod_pct,
    )

    demand = (lh[(lh["source"] == LH_SUBJECT_SOURCE) & (lh["room_tier"] == LH_COMP_ROOM_TIER)]
              .drop_duplicates("arrival_date")[["arrival_date", "market_demand_frac"]]
              .copy())
    demand["arrival_date"] = pd.to_datetime(demand["arrival_date"])
    demand = demand[demand["market_demand_frac"].notna()]
    demand = demand.sort_values("market_demand_frac", ascending=False).head(top_n)

    def _val(frame: pd.DataFrame, d: pd.Timestamp, col: str) -> Optional[float]:
        if d not in frame.index:
            return None
        v = frame.loc[d, col]
        return float(v) if pd.notna(v) else None

    out: List[Dict[str, Any]] = []
    for _, row in demand.iterrows():
        d = row["arrival_date"]
        aka_on_plateau = (bool(aka_frame.loc[d, "on_plateau"])
                          if d in aka_frame.index else False)
        comp_on_plateau = (bool(comp_frame.loc[d, "on_plateau"])
                           if d in comp_frame.index else False)
        out.append({
            "arrival_date": str(d.date()),
            "demand_frac": float(row["market_demand_frac"]),
            "aka_rate_on_date_usd": _val(aka_frame, d, "rate"),
            "aka_baseline_usd": _val(aka_frame, d, "baseline"),
            "aka_pct_above_baseline": _val(aka_frame, d, "pct"),
            "aka_on_plateau": aka_on_plateau,
            "comp_median_on_date_usd": _val(comp_frame, d, "rate"),
            "comp_median_baseline_usd": _val(comp_frame, d, "baseline"),
            "comp_median_pct_above_baseline": _val(comp_frame, d, "pct"),
            "comp_median_on_plateau": comp_on_plateau,
        })
    return out


def compute_yielding_intensity_series(
    lh: pd.DataFrame,
    smoothing_window_days: int = 7,
    plateau_min_length_days: int = PLATEAU_MIN_LENGTH_DAYS,
    plateau_max_dod_pct: float = PLATEAU_MAX_DOD_PCT,
    subject: str = SUBJECT_PROPERTY,
) -> Dict[str, Any]:
    """Per-(property × forward-date) Δ% via TWO-PART baseline + 7-day rolling smooth.

    Two-part baseline (replaces the trailing 30-day MEAN baseline used through
    2026-05-05; see the dual-baseline block above for the diagnosis):
      Part 1: DOW-stratified annual MEDIAN. For each DOW, baseline_dow = median
              rate across all available forward dates of that DOW.
      Part 2: Plateau-aware override. When a property is sitting on a detected
              plateau (≥10 consecutive days within 5% day-over-day, the same
              threshold powering the flat-stretch annotations on the quarterly
              panels), Δ% is forced to 0.0 because the property is anchored,
              not yielding.

    Why this matters: AKA operates in plateaus that flip between rate plans
    (transient, long-stay-LOS, advance-purchase, member). A 30-day trailing
    MEAN baseline reads each plateau transition as a +80% "yielding spike"
    that's actually a rate-plan-availability artifact. The DOW-stratified
    median + plateau override isolates true date-level yielding from rate-plan
    plateau structure. Comps yield within rate plans (smooth Δ% curves); AKA
    flips between discrete rate-plan plateaus (Δ% = 0 across plateau periods,
    with rare excursions on genuine yielding dates).

    BAR-only filter (belt-and-suspenders for non-plateau dates):
      - AKA uses room_tier='suite' (its BAR product).
      - Comps use room_tier='any' (their cheapest displayed BAR; comps don't
        run AKA-depth discount programs).
    Same pattern as compute_compression_response_with_comp_baseline.

    7-day rolling-mean smoothing is applied to each property's daily Δ% series
    AFTER the plateau override so the weekly-cadence (DOW + holiday) yielding
    pattern dominates the visual rather than daily-grain jitter. min_periods=1
    so leading edges still render.

    Returns two payloads:
      - 'per_property': one row per (property, arrival_date), retained for
        tooltip drill-down. Carries raw rate_usd, baseline_usd, on_plateau
        flag, smoothed pct.
      - 'composite': one row per arrival_date with aka_pct + comp_median_pct +
        comp_min_pct + comp_max_pct (across the 5 comps). Drives the Section 1
        yielding-intensity chart's 3-element rendering.
    """
    aka_rates = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"] == LH_SUBJECT_ROOM_TIER)
        & (lh["property"] == subject)
        & (lh["availability_status"] == "available")
        & lh["rate_usd"].notna()
    ][["arrival_date", "rate_usd"]].copy()
    aka_rates["arrival_date"] = pd.to_datetime(aka_rates["arrival_date"])
    aka_series = (aka_rates.drop_duplicates("arrival_date")
                          .set_index("arrival_date")["rate_usd"]
                          .sort_index())

    comps = [p for p in LH_COMP_SET if p != subject]
    comp_rates = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"] == LH_COMP_ROOM_TIER)
        & (lh["property"].isin(comps))
        & (lh["availability_status"] == "available")
        & lh["rate_usd"].notna()
    ][["property", "arrival_date", "rate_usd"]].copy()
    comp_rates["arrival_date"] = pd.to_datetime(comp_rates["arrival_date"])

    aka_frame = _build_dual_baseline_frame(
        aka_series,
        smoothing_window_days=smoothing_window_days,
        plateau_min_length_days=plateau_min_length_days,
        plateau_max_dod_pct=plateau_max_dod_pct,
    )
    comp_frames: Dict[str, pd.DataFrame] = {}
    for p in comps:
        rates = (comp_rates[comp_rates["property"] == p]
                          .drop_duplicates("arrival_date")
                          .set_index("arrival_date")["rate_usd"]
                          .sort_index())
        comp_frames[p] = _build_dual_baseline_frame(
            rates,
            smoothing_window_days=smoothing_window_days,
            plateau_min_length_days=plateau_min_length_days,
            plateau_max_dod_pct=plateau_max_dod_pct,
        )

    def _row(p: str, d: pd.Timestamp, frame: pd.DataFrame) -> Dict[str, Any]:
        baseline = frame.loc[d, "baseline"]
        return {
            "arrival_date": str(d.date()),
            "property": p,
            "rate_usd": float(frame.loc[d, "rate"]),
            "baseline_usd": float(baseline) if pd.notna(baseline) else None,
            "on_plateau": bool(frame.loc[d, "on_plateau"]),
            "pct_above_baseline_smoothed": float(frame.loc[d, "pct_smoothed"]),
        }

    per_property: List[Dict[str, Any]] = []
    for d in aka_frame.index:
        per_property.append(_row(subject, d, aka_frame))
    for p, frame in comp_frames.items():
        for d in frame.index:
            per_property.append(_row(p, d, frame))

    all_dates = sorted({d for d in aka_frame.index}
                       | {d for f in comp_frames.values() for d in f.index})
    composite: List[Dict[str, Any]] = []
    for d in all_dates:
        aka_pct = (float(aka_frame.loc[d, "pct_smoothed"])
                   if d in aka_frame.index else None)
        comp_vals = [float(f.loc[d, "pct_smoothed"])
                     for f in comp_frames.values() if d in f.index]
        if comp_vals:
            comp_median_pct = float(statistics.median(comp_vals))
            comp_min_pct = float(min(comp_vals))
            comp_max_pct = float(max(comp_vals))
        else:
            comp_median_pct = comp_min_pct = comp_max_pct = None
        composite.append({
            "arrival_date": str(d.date()),
            "aka_pct": aka_pct,
            "comp_median_pct": comp_median_pct,
            "comp_min_pct": comp_min_pct,
            "comp_max_pct": comp_max_pct,
            "n_comps": len(comp_vals),
        })

    return {"per_property": per_property, "composite": composite}


# ---------------------------------------------------------------------------
# Section 3 — Tier-spread architecture
# ---------------------------------------------------------------------------

def compute_tier_spread(lh: pd.DataFrame) -> Dict[str, Any]:
    """Brand.com Standard vs Premium spread over time, comp-set-wide.

    Returns per-arrival_date median rates for std + premium tiers across the
    Lighthouse comp set (excluding subject AKA — its all-suites positioning
    means Standard/Premium have no rates for AKA on Brand.com).
    """
    comps = [p for p in LH_COMP_SET if p != SUBJECT_PROPERTY]
    df = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"].isin(["standard", "premium"]))
        & (lh["property"].isin(comps))
        & (lh["availability_status"] == "available")
    ].copy()
    if df.empty:
        return {"rows": []}
    grouped = (df.groupby(["arrival_date", "room_tier"])["rate_usd"]
                 .median()
                 .unstack("room_tier")
                 .reset_index())
    grouped["arrival_date"] = grouped["arrival_date"].astype(str)
    rows: List[Dict[str, Any]] = []
    for _, r in grouped.iterrows():
        std = r.get("standard")
        prem = r.get("premium")
        rows.append({
            "arrival_date": r["arrival_date"],
            "standard_median_usd": float(std) if pd.notna(std) else None,
            "premium_median_usd": float(prem) if pd.notna(prem) else None,
            "spread_usd": (float(prem) - float(std))
                          if pd.notna(prem) and pd.notna(std) else None,
        })
    return {"rows": rows, "gm_claimed_view_premium_usd": VIEW_PREMIUM_GM_CLAIMED_USD}


# ---------------------------------------------------------------------------
# Section 4 — Channel parity
# ---------------------------------------------------------------------------

def compute_channel_parity_lines(
    lh: pd.DataFrame, subject: str = SUBJECT_PROPERTY,
) -> Dict[str, Any]:
    """AKA Brand.com vs Booking.com vs Expedia rate-by-date.

    IMPORTANT: filters on `availability_status == 'available'`, NOT just on
    non-null rate_usd. Expedia/Priceline files mark ~75% of forward dates as
    `not_loaded` (rate not displayed on that channel) — those dates must NOT
    appear in parity charts as zero/null rates, which would misrepresent the
    channel as having a rate gap. Only `available` rows are real comparison
    points.

    Uses room_tier='any' across all three channels (AKA's Suite-only inventory
    surfaces in Brand.com 'any' as well).
    """
    df = lh[
        (lh["source"].isin(["brandcom", "bookingcom", "expedia"]))
        & (lh["room_tier"] == LH_COMP_ROOM_TIER)
        & (lh["property"] == subject)
        & (lh["availability_status"] == "available")
        & lh["rate_usd"].notna()
    ].copy()
    out = {ch: [] for ch in ["brandcom", "bookingcom", "expedia"]}
    for _, r in df.iterrows():
        rv = r["rate_usd"]
        if not (pd.notna(rv) and math.isfinite(float(rv))):
            continue
        out[r["source"]].append({
            "arrival_date": str(r["arrival_date"]),
            "rate_usd": float(rv),
        })
    for ch in out:
        out[ch].sort(key=lambda x: x["arrival_date"])
    return {
        "lines": out,
        "filter_note": ("availability_status=='available' (excludes not_loaded — "
                        "Expedia/Priceline mark ~75% of forward dates as not_loaded)"),
    }


# ---------------------------------------------------------------------------
# Section 5 — Comp-set tier positioning
# ---------------------------------------------------------------------------

def compute_compset_rate_lines(lh: pd.DataFrame) -> Dict[str, List[Dict[str, Any]]]:
    """Brand.com Any-tier rate-by-date for all 6 Lighthouse comps.

    Filter: source='brandcom' AND room_tier='any' AND availability_status='available'
    AND rate_usd is not null. spanGaps=False is enforced at the render layer so
    not_loaded / sold_out gaps surface visually.

    Returns: {property_slug: [{arrival_date, rate_usd}, ...] sorted by arrival_date}
    """
    df = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"] == LH_COMP_ROOM_TIER)
        & (lh["availability_status"] == "available")
        & lh["rate_usd"].notna()
    ][["property", "arrival_date", "rate_usd"]].copy()
    df = df.sort_values(["property", "arrival_date"])
    df["arrival_date"] = df["arrival_date"].astype(str)
    df["rate_usd"] = df["rate_usd"].astype(float)
    out: Dict[str, List[Dict[str, Any]]] = {p: [] for p in LH_COMP_SET}
    for prop, group in df.groupby("property"):
        if prop in out:
            out[prop] = group[["arrival_date", "rate_usd"]].to_dict(orient="records")
    return out


# Quarterly slicing of the comp-set rate series so each panel auto-scales
# without summer compression collapsing the y-axis on shoulder-season flatness.
QUARTER_RANGES = [
    ("Q1", "2026-05-09", "2026-08-08", "TODO Phase 2 anchor-validate narrative slot"),
    ("Q2", "2026-08-09", "2026-11-08", "TODO Phase 2 anchor-validate narrative slot"),
    ("Q3", "2026-11-09", "2027-02-08", "TODO Phase 2 anchor-validate narrative slot"),
    ("Q4", "2027-02-09", "2027-05-08", "TODO Phase 2 anchor-validate narrative slot"),
]


def compute_compset_rate_lines_by_quarter(lh: pd.DataFrame) -> Dict[str, Any]:
    """Slice compset rate lines into 4 quarterly panels.

    Returns: {quarter_label: {start, end, observation,
                              lines: {property: [{arrival_date, rate_usd}, ...]}}}
    """
    base = compute_compset_rate_lines(lh)
    out: Dict[str, Any] = {}
    for label, start, end, observation in QUARTER_RANGES:
        out[label] = {
            "start": start,
            "end": end,
            "observation": observation,
            "lines": {
                p: [pt for pt in pts if start <= pt["arrival_date"] <= end]
                for p, pts in base.items()
            },
        }
    return out


# Flat-stretch detector — picks runs of >=10 consecutive available days where
# day-over-day rate change stays under 5%. Used by the quarterly panels to
# annotate AKA's IC-readable flat plateaus directly on the chart.
FLAT_STRETCH_MIN_DAYS = 10
FLAT_STRETCH_DOD_THRESHOLD = 0.05  # day-over-day fractional change


def compute_compset_flat_stretches(
    lh: pd.DataFrame, subject: str = SUBJECT_PROPERTY,
) -> List[Dict[str, Any]]:
    """Detect subject flat stretches and report comp-set median over the same range.

    Filter (subject): source='brandcom' AND room_tier='any' AND property=subject
    AND availability_status='available' AND rate_usd is not null.

    Stretch criterion: among the AVAILABLE rows (sorted by arrival_date),
    consecutive rows must have fractional rate change strictly less than
    FLAT_STRETCH_DOD_THRESHOLD (5%). Calendar gaps within the run (sold_out /
    not_loaded / los_restricted) do NOT break the stretch — those are missing
    observations of the same rate plateau, not evidence of a different rate.
    A run qualifies when both the calendar span AND the available-observation
    count meet FLAT_STRETCH_MIN_DAYS (10).

    Returns: [{property, rate (median during stretch, rounded int),
               start_date, end_date (ISO strings),
               days (int, calendar span inclusive),
               n_observed (int, available rows in the run),
               comp_median_during (float | None)}, ...]
    """
    df = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"] == LH_COMP_ROOM_TIER)
        & (lh["property"] == subject)
        & (lh["availability_status"] == "available")
        & lh["rate_usd"].notna()
    ][["arrival_date", "rate_usd"]].copy()
    df["arrival_date"] = pd.to_datetime(df["arrival_date"])
    # Dedupe per-date — same fix as compute_flatness_scorecard. Multi-LOS
    # rows would otherwise inflate the per-row stretch walker (each LOS
    # row treated as a separate calendar step). F37/F38 retemplate pattern:
    # groupby arrival_date → median across LOS before per-row detection.
    df = (
        df.groupby("arrival_date", as_index=False)["rate_usd"].median()
          .sort_values("arrival_date").reset_index(drop=True)
    )
    if df.empty:
        return []

    stretches: List[Dict[str, Any]] = []
    n = len(df)
    i = 0
    while i < n:
        j = i + 1
        while j < n:
            prev_rate = df.iloc[j - 1]["rate_usd"]
            curr_rate = df.iloc[j]["rate_usd"]
            if prev_rate == 0:
                break
            if abs(curr_rate - prev_rate) / abs(prev_rate) >= FLAT_STRETCH_DOD_THRESHOLD:
                break
            j += 1
        n_obs = j - i
        first_date = df.iloc[i]["arrival_date"]
        last_date = df.iloc[j - 1]["arrival_date"]
        days_span = (last_date - first_date).days + 1
        if n_obs >= FLAT_STRETCH_MIN_DAYS and days_span >= FLAT_STRETCH_MIN_DAYS:
            window = df.iloc[i:j]
            stretches.append({
                "property": subject,
                "rate": int(round(float(window["rate_usd"].median()))),
                "start_date": str(first_date.date()),
                "end_date": str(last_date.date()),
                "days": int(days_span),
                "n_observed": int(n_obs),
                "comp_median_during": None,  # filled in below
            })
        i = max(j, i + 1)

    # Comp-set median over each stretch's date range — for IC context.
    comps = [p for p in LH_COMP_SET if p != subject]
    comp_df = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"] == LH_COMP_ROOM_TIER)
        & (lh["property"].isin(comps))
        & (lh["availability_status"] == "available")
        & lh["rate_usd"].notna()
    ][["arrival_date", "rate_usd"]].copy()
    comp_df["arrival_date"] = pd.to_datetime(comp_df["arrival_date"])
    for s in stretches:
        sd = pd.to_datetime(s["start_date"])
        ed = pd.to_datetime(s["end_date"])
        in_range = comp_df[(comp_df["arrival_date"] >= sd) & (comp_df["arrival_date"] <= ed)]
        if not in_range.empty:
            s["comp_median_during"] = float(in_range["rate_usd"].median())

    return stretches


# ---------------------------------------------------------------------------
# Subject-vs-comp-median spread (Section 3 metric — replaces the prior
# DOW-stratified annual median + plateau-override baseline rejected by
# Kerry Mack on the 2026-05-07 review). The construct she proposed and
# this function implements:
#
#   For each date in the populated demand window (Lighthouse market_demand_frac
#   non-null, ~60 forward days), compute:
#     aka_rate          = subject Brand.com Suite "any" tier, available only
#     comp_median_rate  = median of the 5 comps' Brand.com Suite "any" tier on
#                         the same date, available rows only
#     daily_delta       = aka_rate − comp_median_rate (USD; sign matters)
#
#   Bucket each date by market_demand_frac:
#     normal    < 0.50
#     shoulder  [0.50, 0.80)
#     high      >= 0.80
#
#   Headline numbers:
#     delta_typical_usd     = median(daily_delta) over normal bucket
#     delta_high_usd        = median(daily_delta) over high bucket
#     spread_movement_usd   = delta_high - delta_typical
#     n_high_days           = count(high)
#
#   Verdict (reuses the existing verdict-classification CSS classes):
#     YIELDS_WITH_MARKET           if spread_movement >= +$50 AND n_high >= 5
#     ANCHORED                     if abs(spread_movement) < $25
#     INSUFFICIENT_DEMAND_WINDOW   if n_high < 5 (gated before threshold checks)
#     ANTI_YIELDS                  if spread_movement <= −$25
#     PARTIALLY_DYNAMIC            otherwise (between |25| and 50, n_high >= 5)
#
# Threshold rationale: $25 is below the day-to-day noise floor we observe on
# this Lighthouse pull (median absolute consecutive-date delta_delta ~ $20-$30
# on AKA's any-tier series — a $25 spread move can be produced by a single
# noisy day pair). $50 is roughly 2× the noise floor and matches the
# magnitude of a real RM yielding move on this comp set.
# ---------------------------------------------------------------------------

SUBJECT_VS_COMP_BUCKET_SHOULDER = 0.50
SUBJECT_VS_COMP_BUCKET_HIGH = 0.80
SUBJECT_VS_COMP_NOISE_FLOOR_USD = 25.0
SUBJECT_VS_COMP_YIELDS_USD = 50.0
SUBJECT_VS_COMP_MIN_HIGH_N = 5


def _classify_subject_vs_comp_verdict(
    spread_movement_usd: Optional[float], n_high_days: int,
) -> str:
    if n_high_days < SUBJECT_VS_COMP_MIN_HIGH_N:
        return "INSUFFICIENT_DEMAND_WINDOW"
    if spread_movement_usd is None:
        return "INSUFFICIENT_DEMAND_WINDOW"
    if spread_movement_usd >= SUBJECT_VS_COMP_YIELDS_USD:
        return "YIELDS_WITH_MARKET"
    if abs(spread_movement_usd) < SUBJECT_VS_COMP_NOISE_FLOOR_USD:
        return "ANCHORED"
    if spread_movement_usd <= -SUBJECT_VS_COMP_NOISE_FLOOR_USD:
        return "ANTI_YIELDS"
    return "PARTIALLY_DYNAMIC"


def compute_subject_vs_comp_median_spread(
    lh: pd.DataFrame,
    subject: str = SUBJECT_PROPERTY,
    comp_set: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Daily AKA-vs-comp-median spread, bucketed by Lighthouse demand intensity.

    Subject-agnostic: pass `subject` and `comp_set` for the next deal property.
    Defaults to AKA + the standard 5-comp Lighthouse comp set.

    Filter: source='brandcom' AND room_tier='any' AND availability_status='available'.
    `room_tier='any'` is the cheapest available rate per (property × date) — same
    rate plotted on the Section 1 quarterly panels.

    Returns shape:
        {
          "headlines": {
              "delta_typical_usd", "delta_high_usd", "spread_movement_usd",
              "n_normal", "n_shoulder", "n_high",
          },
          "verdict": {"classification", "label", "rationale"},
          "rows": [
              {arrival_date, aka_rate_usd, comp_median_rate_usd, daily_delta_usd,
               market_demand_frac, bucket, n_comps_available},
              ...
          ],
          "comp_set": [...],  # property slugs used for comp_median (not subject)
          "thresholds": {noise_floor_usd, yields_usd, min_high_n,
                         shoulder_threshold, high_threshold},
        }
    """
    if comp_set is None:
        comp_set = [p for p in LH_COMP_SET if p != subject]

    df_subj = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"] == LH_COMP_ROOM_TIER)
        & (lh["property"] == subject)
        & (lh["availability_status"] == "available")
        & lh["rate_usd"].notna()
    ][["arrival_date", "rate_usd", "market_demand_frac"]].copy()
    df_comp = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"] == LH_COMP_ROOM_TIER)
        & (lh["property"].isin(comp_set))
        & (lh["availability_status"] == "available")
        & lh["rate_usd"].notna()
    ][["arrival_date", "property", "rate_usd"]].copy()

    # Universe: dates where Lighthouse populates market_demand_frac. Pull from
    # the subject side (one row per date there) to avoid duplicating per-comp
    # demand readings. If the subject has no row on a date but comps do, that
    # date is skipped — daily_delta is undefined without a subject rate.
    df_subj["arrival_date"] = pd.to_datetime(df_subj["arrival_date"])
    df_comp["arrival_date"] = pd.to_datetime(df_comp["arrival_date"])
    df_subj = df_subj[df_subj["market_demand_frac"].notna()].copy()
    df_subj = df_subj.sort_values("arrival_date")

    rows: List[Dict[str, Any]] = []
    bucket_deltas: Dict[str, List[float]] = {"normal": [], "shoulder": [], "high": []}

    for _, sr in df_subj.iterrows():
        d = sr["arrival_date"]
        aka_rate = float(sr["rate_usd"])
        demand = float(sr["market_demand_frac"])
        comps_on_date = df_comp[df_comp["arrival_date"] == d]
        n_comps = int(len(comps_on_date))
        if n_comps == 0:
            # Surface — do NOT impute zero. Emit row with comp_median=None so
            # the caller can flag the data-quality gap.
            comp_median = None
            daily_delta = None
        else:
            comp_median = float(comps_on_date["rate_usd"].median())
            daily_delta = aka_rate - comp_median

        if demand < SUBJECT_VS_COMP_BUCKET_SHOULDER:
            bucket = "normal"
        elif demand < SUBJECT_VS_COMP_BUCKET_HIGH:
            bucket = "shoulder"
        else:
            bucket = "high"

        if daily_delta is not None:
            bucket_deltas[bucket].append(daily_delta)

        rows.append({
            "arrival_date": str(d.date()),
            "aka_rate_usd": aka_rate,
            "comp_median_rate_usd": comp_median,
            "daily_delta_usd": daily_delta,
            "market_demand_frac": demand,
            "bucket": bucket,
            "n_comps_available": n_comps,
        })

    def _med(xs: List[float]) -> Optional[float]:
        return float(statistics.median(xs)) if xs else None

    delta_typical = _med(bucket_deltas["normal"])
    delta_high = _med(bucket_deltas["high"])
    spread_movement = (delta_high - delta_typical) if (delta_typical is not None and delta_high is not None) else None
    n_high = len(bucket_deltas["high"])
    n_normal = len(bucket_deltas["normal"])
    n_shoulder = len(bucket_deltas["shoulder"])

    classification = _classify_subject_vs_comp_verdict(spread_movement, n_high)
    label_map = {
        "YIELDS_WITH_MARKET": "Yields with market",
        "PARTIALLY_DYNAMIC": "Partially yields with market",
        "ANCHORED": "Anchored — flat vs comp set across demand buckets",
        "ANTI_YIELDS": "Anti-yields — discount widens on compression",
        "INSUFFICIENT_DEMAND_WINDOW": "Insufficient high-demand sample (n < 5)",
    }
    rationale_parts = []
    if delta_typical is not None:
        rationale_parts.append(
            f"normal-day median delta: {'+' if delta_typical >= 0 else ''}${delta_typical:,.0f}"
        )
    if delta_high is not None:
        rationale_parts.append(
            f"high-demand median delta: {'+' if delta_high >= 0 else ''}${delta_high:,.0f}"
        )
    if spread_movement is not None:
        rationale_parts.append(
            f"spread movement: {'+' if spread_movement >= 0 else ''}${spread_movement:,.0f}"
        )
    rationale_parts.append(f"n_high={n_high}, n_normal={n_normal}, n_shoulder={n_shoulder}")

    return {
        "headlines": {
            "delta_typical_usd": delta_typical,
            "delta_high_usd": delta_high,
            "spread_movement_usd": spread_movement,
            "n_normal": n_normal,
            "n_shoulder": n_shoulder,
            "n_high": n_high,
        },
        "verdict": {
            "classification": classification,
            "label": label_map[classification],
            "rationale": "; ".join(rationale_parts),
        },
        "rows": rows,
        "comp_set": list(comp_set),
        "thresholds": {
            "noise_floor_usd": SUBJECT_VS_COMP_NOISE_FLOOR_USD,
            "yields_usd": SUBJECT_VS_COMP_YIELDS_USD,
            "min_high_n": SUBJECT_VS_COMP_MIN_HIGH_N,
            "shoulder_threshold": SUBJECT_VS_COMP_BUCKET_SHOULDER,
            "high_threshold": SUBJECT_VS_COMP_BUCKET_HIGH,
        },
    }


def compute_subject_vs_comp_high_bucket_audit(
    lh: pd.DataFrame,
    spread_payload: Dict[str, Any],
    subject: str = SUBJECT_PROPERTY,
    comp_set: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Per-date comp-availability audit + sensitivity for the high bucket.

    Surfaces the panel-size distribution, the breakdown of why each excluded
    comp dropped out (sold_out / not_loaded / los_restricted / blank / room_na /
    no_flex / etc. — verbatim from availability_status), and a robustness check
    on delta_high_usd: as-is vs restricted to dates with comp_n >= 4. The
    methodology details panel in dashboard.js renders these numbers so the IC
    reader can audit the high-bucket sample without leaving the page.
    """
    if comp_set is None:
        comp_set = [p for p in LH_COMP_SET if p != subject]

    rows = spread_payload.get("rows", [])
    high_rows = [r for r in rows if r.get("bucket") == "high"]

    lh_any = lh[(lh["source"] == LH_SUBJECT_SOURCE) & (lh["room_tier"] == LH_COMP_ROOM_TIER)].copy()

    panel_size_distribution: Dict[int, int] = {}
    excluded_status_tally: Dict[str, int] = {}
    per_date: List[Dict[str, Any]] = []

    for r in high_rows:
        d = r["arrival_date"]
        included: List[Tuple[str, float]] = []
        excluded: List[Tuple[str, str]] = []
        for prop in comp_set:
            sub = lh_any[(lh_any["property"] == prop) & (lh_any["arrival_date"] == d)]
            if sub.empty:
                excluded.append((prop, "absent_row"))
                excluded_status_tally["absent_row"] = excluded_status_tally.get("absent_row", 0) + 1
                continue
            avail = sub[sub["availability_status"] == "available"]
            if not avail.empty:
                included.append((prop, float(avail.iloc[0]["rate_usd"])))
            else:
                status = str(sub.iloc[0]["availability_status"])
                excluded.append((prop, status))
                excluded_status_tally[status] = excluded_status_tally.get(status, 0) + 1
        comp_n = len(included)
        panel_size_distribution[comp_n] = panel_size_distribution.get(comp_n, 0) + 1
        per_date.append({
            "arrival_date": d,
            "market_demand_frac": r.get("market_demand_frac"),
            "aka_rate_usd": r.get("aka_rate_usd"),
            "comp_median_rate_usd": r.get("comp_median_rate_usd"),
            "daily_delta_usd": r.get("daily_delta_usd"),
            "comp_n": comp_n,
            "included": [{"property": p, "rate_usd": rt} for p, rt in sorted(included)],
            "excluded": [{"property": p, "availability_status": st} for p, st in sorted(excluded)],
        })

    # Sensitivity: delta_high under the comp_n>=4 cut
    deltas_asis = [r["daily_delta_usd"] for r in high_rows if r.get("daily_delta_usd") is not None]
    deltas_full = [d["daily_delta_usd"] for d in per_date
                   if d["comp_n"] >= 4 and d["daily_delta_usd"] is not None]
    delta_high_asis = float(statistics.median(deltas_asis)) if deltas_asis else None
    delta_high_full = float(statistics.median(deltas_full)) if deltas_full else None

    return {
        "per_date": per_date,
        "panel_size_distribution": panel_size_distribution,
        "excluded_status_tally": excluded_status_tally,
        "excluded_total": sum(excluded_status_tally.values()),
        "sensitivity": {
            "delta_high_asis": delta_high_asis,
            "delta_high_full": delta_high_full,
            "n_asis": len(deltas_asis),
            "n_full": len(deltas_full),
            "shift_usd": (delta_high_full - delta_high_asis)
                         if (delta_high_full is not None and delta_high_asis is not None) else None,
        },
    }


def compute_flatness_scorecard(lh: pd.DataFrame) -> Dict[str, Any]:
    """Per-property flatness diagnostic on Brand.com any-tier BAR.

    For each LH_COMP_SET property, runs detect_plateaus() on the date-indexed
    available BAR series and reports:
      - n_observed       (rows where rate_usd is not null + status='available')
      - n_on_plateau     (sum of available rows inside any detected plateau)
      - pct_flat         (n_on_plateau / n_observed; None if n_observed==0)
      - longest_plateau_days  (calendar-span days of the longest plateau)
      - plateau_values_usd    (median rate of each plateau, sorted by days desc)

    Plateau threshold matches detect_plateaus()'s defaults (10 days, 5% DoD)
    so flatness in the scorecard is the same definition as the quarterly-panel
    annotations and the dual-baseline override — one plateau, one definition.

    Subject-agnostic: rows are keyed by `property`, render copy lives in
    dashboard.js. This function is callable as-is for the next deal property.
    """
    rows: List[Dict[str, Any]] = []
    for prop in LH_COMP_SET:
        sub = lh[
            (lh["source"] == LH_SUBJECT_SOURCE)
            & (lh["room_tier"] == LH_COMP_ROOM_TIER)
            & (lh["property"] == prop)
            & (lh["availability_status"] == "available")
            & lh["rate_usd"].notna()
        ][["arrival_date", "rate_usd"]].copy()
        if sub.empty:
            rows.append({
                "property": prop,
                "n_observed": 0,
                "n_on_plateau": 0,
                "pct_flat": None,
                "longest_plateau_days": 0,
                "plateau_values_usd": [],
            })
            continue
        sub["arrival_date"] = pd.to_datetime(sub["arrival_date"])
        # Dedupe per-date — Lighthouse multi-LOS exports carry one row per
        # (date, LOS=1/3/7), so set_index would produce a duplicated index
        # and the per-step plateau walker would treat each LOS row as a
        # separate calendar step, inflating run lengths 2-3×. Median across
        # LOS gives the per-date rate the plateau detector should see.
        # F37/F38 retemplate pattern: groupby arrival_date → median across LOS.
        rate_series = (
            sub.groupby("arrival_date")["rate_usd"].median().astype(float).sort_index()
        )

        plateaus = detect_plateaus(rate_series)

        # n_on_plateau: count of available observations whose date falls inside
        # any plateau range (inclusive).
        observed_dates = rate_series.index
        on_plateau = pd.Series(False, index=observed_dates)
        plateau_days_each: List[int] = []
        for start, end, _med in plateaus:
            on_plateau.loc[start:end] = True
            plateau_days_each.append(int((end - start).days) + 1)

        n_observed = int(len(observed_dates))
        n_on_plateau = int(on_plateau.sum())
        pct_flat = (n_on_plateau / n_observed) if n_observed else None
        longest_days = max(plateau_days_each) if plateau_days_each else 0

        # Plateau medians sorted by plateau length (days) descending — surfaces
        # the load-bearing flat structures (longest plateaus first).
        plats_with_days = [
            (int((end - start).days) + 1, float(med))
            for start, end, med in plateaus
        ]
        plats_with_days.sort(key=lambda t: t[0], reverse=True)
        plateau_values_usd = [med for _, med in plats_with_days]

        rows.append({
            "property": prop,
            "n_observed": n_observed,
            "n_on_plateau": n_on_plateau,
            "pct_flat": pct_flat,
            "longest_plateau_days": longest_days,
            "plateau_values_usd": plateau_values_usd,
        })
    return {"rows": rows}


def compute_comp_set_tier_positioning(lh: pd.DataFrame) -> Dict[str, Any]:
    """Per-property median rate by Brand.com tier (standard / premium / suite)."""
    df = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"].isin(["standard", "premium", "suite"]))
        & (lh["availability_status"] == "available")
    ].copy()
    grouped = (df.groupby(["property", "room_tier"])["rate_usd"]
                 .agg(["median", "mean", "count"])
                 .reset_index())
    matrix: Dict[str, Dict[str, Dict[str, Optional[float]]]] = {}
    for _, r in grouped.iterrows():
        prop = r["property"]
        tier = r["room_tier"]
        matrix.setdefault(prop, {})[tier] = {
            "median_usd": float(r["median"]) if pd.notna(r["median"]) else None,
            "mean_usd": float(r["mean"]) if pd.notna(r["mean"]) else None,
            "n": int(r["count"]),
        }
    return {"properties": LH_COMP_SET, "matrix": matrix}


# ---------------------------------------------------------------------------
# Section 6 — per-property rate explorer feed (renumbered through
# 8 → 7 → 6 across the 2026-05-07 channel-parity-section + Penthouse-section
# cleanup passes).
# ---------------------------------------------------------------------------

def compute_lighthouse_rates_explorer(lh: pd.DataFrame) -> List[Dict[str, Any]]:
    """Per-property × per-date Brand.com rate cells that drive Section 1's
    flatness scorecard / quarterly panels and Section 3's subject-vs-comp-
    median spread. Same filter as the spread metric (subject + comps on
    any-tier, available only) so an IC reviewer can drill from a Section 3
    bucket value to the raw rate cell behind it. Property is projected to
    its IC-readable display label (LH_PROPERTY_DISPLAY).

    Channel is rendered as 'direct' rather than the underlying 'brandcom'
    source token — Section 6 is laymen-readable, source name is a Lighthouse
    implementation detail.
    """
    aka = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["property"] == SUBJECT_PROPERTY)
        & (lh["room_tier"] == LH_SUBJECT_ROOM_TIER)
        & (lh["availability_status"] == "available")
        & lh["rate_usd"].notna()
    ][["arrival_date", "dow", "property", "rate_usd", "room_tier"]].copy()

    comps = [p for p in LH_COMP_SET if p != SUBJECT_PROPERTY]
    comp = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["property"].isin(comps))
        & (lh["room_tier"] == LH_COMP_ROOM_TIER)
        & (lh["availability_status"] == "available")
        & lh["rate_usd"].notna()
    ][["arrival_date", "dow", "property", "rate_usd", "room_tier"]].copy()

    df = pd.concat([aka, comp], ignore_index=True)
    df = df.sort_values(["arrival_date", "property"]).reset_index(drop=True)

    out: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        out.append({
            "arrival_date": str(r["arrival_date"]),
            "dow": str(r["dow"]),
            "property": LH_PROPERTY_DISPLAY.get(r["property"], r["property"]),
            "rate_usd": float(r["rate_usd"]),
            "room_tier": str(r["room_tier"]),
            "channel": "direct",
        })
    return out


# ---------------------------------------------------------------------------
# Section 5 — LOS-restriction trigger (renumbered from original Section 7
# through 6 → 5 across the 2026-05-07 channel-parity-section + Penthouse-
# section cleanup passes).
# ---------------------------------------------------------------------------

def compute_los_restrictions_grid(lh: pd.DataFrame) -> List[Dict[str, Any]]:
    """arrival_date × property × LOS restriction value (where present).

    Pulls Brand.com any-tier rows with availability_status='los_restricted'.
    """
    df = lh[
        (lh["source"] == LH_SUBJECT_SOURCE)
        & (lh["room_tier"] == LH_COMP_ROOM_TIER)
        & (lh["availability_status"] == "los_restricted")
    ].copy()
    out: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        out.append({
            "arrival_date": str(r["arrival_date"]),
            "property": r["property"],
            "los_restriction": int(r["los_restriction"])
                                if pd.notna(r["los_restriction"]) else None,
        })
    return out


# ---------------------------------------------------------------------------
# Headline KPIs
# ---------------------------------------------------------------------------

def _brand_vs_booking_gap_median_pct(lh: pd.DataFrame, subject: str = SUBJECT_PROPERTY) -> Tuple[Optional[float], int]:
    """Median |brandcom-bookingcom| / min(brandcom,bookingcom) for AKA, available-only."""
    df = lh[
        (lh["source"].isin(["brandcom", "bookingcom"]))
        & (lh["room_tier"] == LH_COMP_ROOM_TIER)
        & (lh["property"] == subject)
        & (lh["availability_status"] == "available")
    ].copy()
    if df.empty:
        return None, 0
    pivot = (df.pivot_table(index="arrival_date", columns="source",
                            values="rate_usd", aggfunc="first")
               .dropna(subset=["brandcom", "bookingcom"]))
    if pivot.empty:
        return None, 0
    gaps = ((pivot["brandcom"] - pivot["bookingcom"]).abs()
            / pivot[["brandcom", "bookingcom"]].min(axis=1))
    return float(gaps.median()), int(len(gaps))


def compute_headline_kpis(
    lh: pd.DataFrame,
    firecrawl_payload: Dict[str, Any],
    rm_verdict: Dict[str, Any],
) -> Dict[str, Any]:
    """6 headline KPIs. Each emits {value, unit, source, n_used} for kpi_snapshot."""
    # 1. Verdict display label
    verdict_label = rm_verdict.get("display_label")

    # 2. Days at market demand > 0.90
    demand = (lh[(lh["source"] == LH_SUBJECT_SOURCE) & (lh["room_tier"] == LH_COMP_ROOM_TIER)]
              .drop_duplicates("arrival_date"))
    n_high = int((demand["market_demand_frac"] > HIGH_DEMAND_THRESHOLD).sum())

    # 3. Demand response (from rm_verdict). Three sub-rule values, all surfaced.
    sub_1a = rm_verdict["rules"]["demand_response"]["sub_rules"]["1a"]
    sub_1b = rm_verdict["rules"]["demand_response"]["sub_rules"]["1b"]
    sub_1b_levels = rm_verdict["rules"]["demand_response"]["sub_rules"]["1b_levels"]

    # 4. DOW normalized range %
    dow_value = rm_verdict["rules"]["dow_normalized_range"].get("value")
    dow_n = rm_verdict["rules"]["dow_normalized_range"].get("n_used", 0)

    # 5. Brand vs Booking parity gap median %
    parity_gap, parity_n = _brand_vs_booking_gap_median_pct(lh)

    # 6. View premium GM-claimed vs observed (from Firecrawl headline summary).
    headline_vp = firecrawl_payload.get("headline_view_premium", {}) or {}
    vp_observed = None
    vp_n = 0
    if "1BR Platinum" in headline_vp:
        vp_observed = headline_vp["1BR Platinum"].get("median_delta")
        vp_n = headline_vp["1BR Platinum"].get("n", 0)

    return {
        "verdict_label": {
            "value": verdict_label, "unit": "categorical",
            "source": "lighthouse + verdict logic",
            "n_used": rm_verdict.get("n_rules_observed"),
        },
        "days_high_demand_count": {
            "value": n_high, "unit": "count",
            "source": "lighthouse brandcom_any market_demand_frac>0.90",
            "n_used": int(len(demand)),
        },
        "demand_response_r": {
            "value": {
                "1a_demand_60d": sub_1a.get("value"),
                "1b_otb_first_diff_365d": sub_1b.get("value"),
                "1b_levels_otb_365d": sub_1b_levels.get("value"),
            },
            "unit": "pearson_r",
            "source": ("lighthouse brandcom_suite (1a vs market_demand_frac 60d; "
                       "1b first-differences vs market_otb_frac; "
                       "1b_levels raw-level vs market_otb_frac, informational)"),
            "n_used": {
                "1a": sub_1a.get("n_used", 0),
                "1b": sub_1b.get("n_used", 0),
                "1b_levels": sub_1b_levels.get("n_used", 0),
            },
            "low_power": {
                "1a": sub_1a.get("low_power"),
                "1b": sub_1b.get("low_power"),
                "1b_levels": sub_1b_levels.get("low_power"),
            },
            "rule_pass": rm_verdict["rules"]["demand_response"]["pass"],
            "gated_sub_rules": ["1a", "1b"],
        },
        "dow_normalized_range_pct": {
            "value": (dow_value * 100) if dow_value is not None else None,
            "unit": "pct",
            "source": "lighthouse brandcom_suite",
            "n_used": dow_n,
        },
        "brand_vs_booking_gap_median_pct": {
            "value": (parity_gap * 100) if parity_gap is not None else None,
            "unit": "pct",
            "source": "lighthouse subject brandcom_any vs bookingcom_any",
            "n_used": parity_n,
        },
        "view_premium_gm_claimed_vs_observed_usd": {
            "value": {
                "gm_claimed_usd": VIEW_PREMIUM_GM_CLAIMED_USD,
                "observed_median_usd": float(vp_observed) if vp_observed is not None else None,
            },
            "unit": "usd",
            "source": "gm tour reference vs firecrawl 1BR Platinum view-premium engine",
            "n_used": vp_n,
        },
    }
