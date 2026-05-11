"""
Build Embarcadero-only RM analyses + dashboard data payload from raw_rates.csv.

Pivot 2026-04-25 (afternoon): cross-property comparison was withdrawn after
methodology review found that DC luxury comps don't structure within-product
view differentiation comparably to AKA. This script renders AKA-only.

Filters:
- property_id == "hr_embarcadero"
- nights == 1
- is_bar == True
- rate_per_night_usd > 0

Outputs:
  analysis/embarcadero_rate_matrix.md
  analysis/embarcadero_view_premium_through_time.md
  analysis/embarcadero_tier_stepups_through_time.md
  analysis/embarcadero_yielding_intensity.md
  analysis/embarcadero_channel_parity_detail.md
  data.js  (window.DASHBOARD_DATA = {...})
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

import pandas as pd

import analytics_lighthouse as lh_analytics

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw_rates.csv"
OUT = ROOT
ANALYSIS = OUT / "analysis"
ANALYSIS.mkdir(exist_ok=True, parents=True)

# Lighthouse CSV lives in the sibling Lighthouse/ working dir (alongside the repo).
DEFAULT_LH_CSV = ROOT.parent / "Lighthouse" / "lighthouse_rates.csv"

# Subject + control-comp slugs for the comparative box. Subject = the deal's
# focal property; control comp = the sophisticated-RMS comparator (Hilton's
# RMS pick for DC luxury, hr_soma for SF Hyatt-family).
SUBJECT_PROPERTY = "hr_embarcadero"
CONTROL_COMP_PROPERTY = "hr_soma"

CHANNELS = ["direct", "hotels_com", "booking"]
CHANNEL_LABEL = {"direct": "Direct", "hotels_com": "Hotels.com", "booking": "Booking"}

# Per the brief: 1BR Platinum view pair = 1BD vs 1BDPRC; 2BR Platinum view pair = 2BD vs 2BDPRC
VIEW_PAIRS = []

# Tier step-ups
TIER_STEPS = []

ROOM_ORDER = []
ROOM_LABEL = {}


def load_aka() -> pd.DataFrame:
    """One canonical BAR row per (channel, date, room_code) — used by the rate
    matrix heatmap. Lowest non-ADA BAR per cell."""
    df = pd.read_csv(RAW)
    df = df[df["property_id"] == "hr_embarcadero"].copy()
    df = df[df["nights"] == 1]
    df = df[df["is_bar"] == True]  # noqa: E712
    df = df[df["rate_per_night_usd"] > 0]
    if "mapped_ada" in df.columns:
        df["_ada_rank"] = df["mapped_ada"].astype(bool).astype(int)
    else:
        df["_ada_rank"] = 0
    df = df.sort_values(["channel", "arrival_date", "mapped_internal_code", "_ada_rank", "rate_per_night_usd"])
    df = df.drop_duplicates(subset=["channel", "arrival_date", "mapped_internal_code"], keep="first")
    return df.reset_index(drop=True)


def cell_table(df: pd.DataFrame) -> dict:
    """Return {(channel, arrival_date, code): rate}."""
    out = {}
    for _, r in df.iterrows():
        out[(r["channel"], r["arrival_date"], r["mapped_internal_code"])] = float(r["rate_per_night_usd"])
    return out


# ---------------------------------------------------------------------------
# Rate-plan-matched view-premium engine (added 2026-04-26 after data quality
# review). Compares rates across the view-vs-non-view pair using the SAME
# rate_plan_label, restricted to refundable=True rows. Eliminates the
# apples-to-oranges artifacts that produced spurious negative premiums.
# ---------------------------------------------------------------------------
def load_refundable_rate_plans() -> pd.DataFrame:
    """All refundable rate-plan rows, with one row per (channel, date,
    room_code, rate_plan_label) — picks lowest non-ADA rate per plan."""
    df = pd.read_csv(RAW)
    df = df[df["property_id"] == "hr_embarcadero"].copy()
    df = df[df["nights"] == 1]
    df = df[df["refundable"] == True]  # noqa: E712
    df = df[df["rate_per_night_usd"] > 0]
    df = df[df["mapped_internal_code"].isin(ROOM_ORDER)]  # exclude UNMAPPED
    if "mapped_ada" in df.columns:
        df["_ada_rank"] = df["mapped_ada"].astype(bool).astype(int)
    else:
        df["_ada_rank"] = 0
    df = df.sort_values(
        ["channel", "arrival_date", "mapped_internal_code", "rate_plan_label",
         "_ada_rank", "rate_per_night_usd"]
    )
    df = df.drop_duplicates(
        subset=["channel", "arrival_date", "mapped_internal_code", "rate_plan_label"],
        keep="first",
    )
    return df.reset_index(drop=True)


def rate_plans_by_cell(df: pd.DataFrame) -> dict:
    """Return {(channel, date, code): {rate_plan_label: rate}}."""
    out: dict = {}
    for _, r in df.iterrows():
        key = (r["channel"], r["arrival_date"], r["mapped_internal_code"])
        out.setdefault(key, {})[r["rate_plan_label"]] = float(r["rate_per_night_usd"])
    return out


def hotels_com_sanity_check(raw_csv: Path) -> dict:
    """Manual-review concern (2026-04-26): some Hotels.com rows might have
    captured total_stay_usd in rate_per_night_usd. Flag any row where
    rate_per_night_usd > total_stay_usd / nights * 1.10.

    Empirical result on this scrape: zero suspects at LOS=1 (the analysis
    uses LOS=1 only). Suspects appear only at LOS=7 — likely flat-tax
    captures or stale-total quirks unique to long-stay Hotels.com pages.
    LOS=1 is clean, so the view-premium / rate-matrix / parity analyses are
    unaffected. The suspect LOS=7 rows are flagged here for transparency.
    """
    def _stats(d):
        if d.empty:
            return {"n": 0, "suspect_count": 0, "median_ratio": None,
                    "min_ratio": None, "max_ratio": None}
        d = d.copy()
        d["nights_int"] = d["nights"].astype(int)
        d["implied_nightly"] = d["total_stay_usd"] / d["nights_int"]
        d["ratio"] = d["rate_per_night_usd"] / d["implied_nightly"]
        return {
            "n": int(len(d)),
            "suspect_count": int((d["ratio"] > 1.10).sum()),
            "median_ratio": float(d["ratio"].median()),
            "min_ratio": float(d["ratio"].min()),
            "max_ratio": float(d["ratio"].max()),
        }

    df = pd.read_csv(raw_csv)
    base = df[(df["property_id"] == "hr_embarcadero") &
              (df["channel"] == "hotels_com") &
              (df["rate_per_night_usd"] > 0) &
              (df["total_stay_usd"] > 0)]
    return {
        "all_los": _stats(base),
        "los_1": _stats(base[base["nights"] == 1]),
        "los_7": _stats(base[base["nights"] == 7]),
        "los_3": _stats(base[base["nights"] == 3]),
    }


def fmt_money(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"${x:,.0f}"


def fmt_pct(x, digits=1):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:+.{digits}f}%"


def median(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return statistics.median(xs)


def mean(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return sum(xs) / len(xs)


def stdev(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    return statistics.stdev(xs)


# ------------------------------ Analysis 1 ------------------------------
def analysis_rate_matrix(df: pd.DataFrame, dates: list[str], codes: list[str], cells: dict) -> str:
    """Per-room-category × per-date × per-channel BAR matrix."""
    lines = ["# Analysis 1 — AKA Full Rate Matrix",
             "",
             "Source: `raw_rates.csv`, filtered to property=aka_white_house, LOS=1, is_bar=True, rate>0.",
             "Rooms with multiple matched rows per cell collapsed by selecting the lowest-priced non-ADA BAR.",
             ""]

    # Coverage table
    lines += ["## Coverage by (channel × room category)", ""]
    lines.append("| Room | " + " | ".join(CHANNEL_LABEL[c] for c in CHANNELS) + " | Total |")
    lines.append("|---|" + "|".join(["---:"] * (len(CHANNELS) + 1)) + "|")
    for code in codes:
        cells_per_channel = []
        for ch in CHANNELS:
            n = sum(1 for d in dates if (ch, d, code) in cells)
            cells_per_channel.append(f"{n}/{len(dates)}")
        total = sum(1 for d in dates for ch in CHANNELS if (ch, d, code) in cells)
        lines.append(f"| {ROOM_LABEL.get(code, code)} ({code}) | " + " | ".join(cells_per_channel) + f" | {total}/{len(dates)*len(CHANNELS)} |")
    lines.append("")

    # Mean/min/max per room
    lines += ["## Per-room rate-card range (across all dates × channels)", "",
              "| Room | n cells | min | max | mean | median |",
              "|---|---:|---:|---:|---:|---:|"]
    for code in codes:
        rates = [v for k, v in cells.items() if k[2] == code]
        if not rates:
            lines.append(f"| {code} | 0 | — | — | — | — |")
            continue
        lines.append(f"| {ROOM_LABEL.get(code, code)} ({code}) | {len(rates)} | "
                     f"{fmt_money(min(rates))} | {fmt_money(max(rates))} | "
                     f"{fmt_money(mean(rates))} | {fmt_money(median(rates))} |")
    lines.append("")

    # Per-channel breakdown (one heatmap-friendly wide table per channel)
    for ch in CHANNELS:
        lines += [f"## {CHANNEL_LABEL[ch]} channel — BAR by date × room", ""]
        header = "| Room | " + " | ".join(d for d in dates) + " |"
        sep = "|---|" + "|".join(["---:"] * len(dates)) + "|"
        lines.append(header)
        lines.append(sep)
        for code in codes:
            row = [ROOM_LABEL.get(code, code) + f" ({code})"]
            for d in dates:
                v = cells.get((ch, d, code))
                row.append(fmt_money(v))
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    # Mean BAR per (date × channel × room)
    lines += ["## Mean BAR per date (across channels with data)", "",
              "| Room | " + " | ".join(dates) + " |",
              "|---|" + "|".join(["---:"] * len(dates)) + "|"]
    for code in codes:
        row = [ROOM_LABEL.get(code, code) + f" ({code})"]
        for d in dates:
            vals = [cells.get((ch, d, code)) for ch in CHANNELS]
            vals = [v for v in vals if v is not None]
            row.append(fmt_money(mean(vals) if vals else None))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # Long-format export reference
    lines += ["## Long-format raw export (first 50 rows shown)",
             "",
             "| channel | arrival_date | room_code | room_name | rate_usd |",
             "|---|---|---|---|---:|"]
    rows = []
    for (ch, d, code), v in sorted(cells.items()):
        rows.append((ch, d, code, ROOM_LABEL.get(code, code), v))
    for ch, d, code, name, v in rows[:50]:
        lines.append(f"| {ch} | {d} | {code} | {name} | {fmt_money(v)} |")
    lines.append("")
    lines.append(f"Total long-format rows: **{len(rows)}**.")
    lines.append("")

    return "\n".join(lines)


# ------------------------------ Analysis 2 ------------------------------
def compute_view_premium_matched(dates, plans_by_cell, base_code, view_code):
    """Per-(channel × date) view premium using rate-plan-matched comparison.

    For each (channel, date) cell, find rate_plan_labels common to both rooms.
    Within those common plans, take the lowest-priced plan's rate for each
    room — that's the apples-to-apples premium. If no plans match, the cell
    is excluded from the pair's measurement (and surfaced as a coverage gap).
    """
    rows = []
    for d in dates:
        for ch in CHANNELS:
            non_plans = plans_by_cell.get((ch, d, base_code), {})
            view_plans = plans_by_cell.get((ch, d, view_code), {})
            common = set(non_plans) & set(view_plans)
            if not common:
                continue
            # Pick the canonical match: prefer "Standard Rate" if both rooms have it,
            # otherwise the rate plan whose pair-min is lowest (the rate a booker
            # actually sees first).
            if "Standard Rate" in common:
                chosen_plan = "Standard Rate"
            else:
                pair_mins = {p: min(non_plans[p], view_plans[p]) for p in common}
                chosen_plan = min(pair_mins, key=pair_mins.get)
            non = non_plans[chosen_plan]
            view = view_plans[chosen_plan]
            delta = view - non
            pct = (delta / non) * 100 if non else None
            rows.append({
                "channel": ch, "date": d,
                "non_view": non, "view": view,
                "delta": delta, "pct": pct,
                "rate_plan": chosen_plan,
                "matched_plans": sorted(common),
                "matched_n": len(common),
            })
    return rows


def analysis_view_premium(dates, plans_by_cell) -> tuple[str, dict]:
    """View premium per (pair × channel × date), rate-plan matched."""
    lines = ["# Analysis 2 — View Premium Through Time", "",
             "Pairs analyzed: 1BR Platinum (1BD vs 1BDPRC) and 2BR Platinum (2BD vs 2BDPRC).",
             "",
             "**Methodology (revised 2026-04-26): rate-plan-matched comparison.** "
             "Manual spot-check found that the prior unmatched approach "
             "compared different rate-plan types across rooms within the same "
             "cell (e.g. non-view at Standard Rate vs view at Fenced "
             "early-decision rate), producing artifactual negative premiums. "
             "The new approach: for each (channel × date) cell, find "
             "rate_plan_labels common to both rooms; pick the canonical match "
             "(prefer 'Standard Rate' if both rooms have it; otherwise the "
             "lowest-pair-min plan); use that plan's rate for each room. "
             "Refundable=True only.",
             ""]
    payload = {"pairs": []}

    for label, base, view in VIEW_PAIRS:
        pair_rows = compute_view_premium_matched(dates, plans_by_cell, base, view)
        payload["pairs"].append({"label": label, "base": base, "view": view, "rows": pair_rows})

        deltas = [r["delta"] for r in pair_rows]
        pcts = [r["pct"] for r in pair_rows]
        neg = sum(1 for x in deltas if x < 0)

        lines += [f"## {label}: {base} vs {view}", "",
                  f"- Matched (date × channel) pairs: **n = {len(pair_rows)}**",
                  f"- Median $ premium: **{fmt_money(median(deltas)) if deltas else '—'}**  |  "
                  f"Mean $ premium: **{fmt_money(mean(deltas)) if deltas else '—'}**",
                  f"- Median % premium: **{fmt_pct(median(pcts), 2) if pcts else '—'}**  |  "
                  f"Mean % premium: **{fmt_pct(mean(pcts), 2) if pcts else '—'}**",
                  f"- Range $: {fmt_money(min(deltas)) if deltas else '—'} → {fmt_money(max(deltas)) if deltas else '—'}",
                  f"- Negative-premium cells: **{neg} of {len(deltas)}**",
                  ""]

        # By-channel summary
        lines += ["### By channel", "",
                  "| Channel | n | median $ | mean $ | median % | mean % | min | max |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for ch in CHANNELS:
            ch_deltas = [r["delta"] for r in pair_rows if r["channel"] == ch]
            ch_pcts = [r["pct"] for r in pair_rows if r["channel"] == ch]
            if not ch_deltas:
                lines.append(f"| {CHANNEL_LABEL[ch]} | 0 | — | — | — | — | — | — |")
                continue
            lines.append(
                f"| {CHANNEL_LABEL[ch]} | {len(ch_deltas)} | "
                f"{fmt_money(median(ch_deltas))} | {fmt_money(mean(ch_deltas))} | "
                f"{fmt_pct(median(ch_pcts), 2)} | {fmt_pct(mean(ch_pcts), 2)} | "
                f"{fmt_money(min(ch_deltas))} | {fmt_money(max(ch_deltas))} |"
            )
        lines.append("")

        # Per-date table
        lines += ["### Per-date premium (canonical rate-plan match)", "",
                  "| Arrival | Channel | Plan used | Non-view | View | Δ$ | Δ% |",
                  "|---|---|---|---:|---:|---:|---:|"]
        for r in sorted(pair_rows, key=lambda x: (x["date"], x["channel"])):
            lines.append(
                f"| {r['date']} | {CHANNEL_LABEL[r['channel']]} | {r['rate_plan']} | "
                f"{fmt_money(r['non_view'])} | {fmt_money(r['view'])} | "
                f"{fmt_money(r['delta'])} | {fmt_pct(r['pct'], 1)} |"
            )
        lines.append("")

    # GM-claimed +$50 reference + observations
    lines += ["## Pattern observations", "",
              "- GM-claimed view premium: **+$50 flat**.",
              "- The rate-plan-matched empirical premium is materially below the GM's claim "
              "in both 1BR and 2BR architectures, but unlike the prior unmatched analysis, "
              "**there are no negative-premium cells**. Across all measured cells the view "
              "room is priced ≥ the non-view room.",
              "- Direct channel encodes a clean +$18 view-premium rule on both 1BR and "
              "2BR pairs. OTAs (Booking, Hotels.com) sometimes show $0 premium but never "
              "negative once rate-plan-matched.",
              "- The story for the IC memo: the GM's +$50 figure is wrong in degree (~3× "
              "overstated), but the discipline gap is smaller and cleaner than the prior "
              "analysis suggested.",
              ""]

    return "\n".join(lines), payload


# ------------------------------ Analysis 3 ------------------------------
def analysis_tier_stepups(dates, cells) -> tuple[str, dict]:
    lines = ["# Analysis 3 — Tier Step-Ups Through Time", "",
             "For each tier transition, computes per-(channel × date) step-up where both rooms have BAR rates.",
             ""]
    payload = {"steps": []}

    for label, lower, upper in TIER_STEPS:
        rows = []
        for d in dates:
            for ch in CHANNELS:
                lv = cells.get((ch, d, lower))
                uv = cells.get((ch, d, upper))
                if lv is not None and uv is not None:
                    delta = uv - lv
                    pct = (delta / lv) * 100 if lv else None
                    rows.append({
                        "channel": ch, "date": d,
                        "lower": lv, "upper": uv,
                        "delta": delta, "pct": pct,
                    })
        payload["steps"].append({"label": label, "lower": lower, "upper": upper, "rows": rows})

        deltas = [r["delta"] for r in rows]
        pcts = [r["pct"] for r in rows]

        lines += [f"## {label}", "",
                  f"- n = **{len(rows)}**",
                  f"- Median Δ$: **{fmt_money(median(deltas)) if deltas else '—'}**  |  "
                  f"Median Δ%: **{fmt_pct(median(pcts), 1) if pcts else '—'}**",
                  f"- Mean Δ$: {fmt_money(mean(deltas)) if deltas else '—'}  |  "
                  f"Mean Δ%: {fmt_pct(mean(pcts), 1) if pcts else '—'}",
                  f"- Range Δ$: {fmt_money(min(deltas)) if deltas else '—'} → "
                  f"{fmt_money(max(deltas)) if deltas else '—'}",
                  ""]
        # CV line
        if deltas and mean(deltas):
            sd_d = stdev(deltas) or 0
            cv_d = sd_d / mean(deltas) * 100
            lines.append(f"- Coefficient of variation in Δ$ across pairs: {cv_d:.1f}%")
        lines.append("")

        lines += ["### By channel", "",
                  "| Channel | n | median Δ$ | median Δ% | min Δ$ | max Δ$ |",
                  "|---|---:|---:|---:|---:|---:|"]
        for ch in CHANNELS:
            ch_d = [r["delta"] for r in rows if r["channel"] == ch]
            ch_p = [r["pct"] for r in rows if r["channel"] == ch]
            if not ch_d:
                lines.append(f"| {CHANNEL_LABEL[ch]} | 0 | — | — | — | — |")
                continue
            lines.append(
                f"| {CHANNEL_LABEL[ch]} | {len(ch_d)} | "
                f"{fmt_money(median(ch_d))} | {fmt_pct(median(ch_p), 1)} | "
                f"{fmt_money(min(ch_d))} | {fmt_money(max(ch_d))} |"
            )
        lines.append("")

        lines += ["### Per-date step-up", "",
                  "| Arrival | Channel | Lower | Upper | Δ$ | Δ% |",
                  "|---|---|---:|---:|---:|---:|"]
        for r in sorted(rows, key=lambda x: (x["date"], x["channel"])):
            lines.append(
                f"| {r['date']} | {CHANNEL_LABEL[r['channel']]} | "
                f"{fmt_money(r['lower'])} | {fmt_money(r['upper'])} | "
                f"{fmt_money(r['delta'])} | {fmt_pct(r['pct'], 1)} |"
            )
        lines.append("")

    lines += ["## Pattern observations", "",
              "- Tier step-ups in $ terms are roughly demand-stable when expressed as a % of the lower tier; "
              "the absolute $ delta scales with rate level.",
              "- Where step-ups compress (Δ% drops on a given date), the rate ladder is being yield-flexed "
              "such that the higher tier softens faster than the lower tier.",
              "- 2BDPRC → PH and 1BDPRC → PH show the widest variability — penthouse yielding is the dominant "
              "driver of step-up movement.",
              ""]
    return "\n".join(lines), payload


# ------------------------------ Analysis 4 ------------------------------
def analysis_yielding_intensity(dates, codes, cells) -> tuple[str, dict]:
    lines = ["# Analysis 4 — Per-Room Yielding Intensity", "",
             "For each AKA room category, summarises rate-card variability across all (channel × date) cells "
             "where the room has BAR data. CV (coefficient of variation = stdev / mean) and min-to-max ratio "
             "are the two yielding-intensity proxies.",
             ""]

    rows = []
    for code in codes:
        rates = [v for k, v in cells.items() if k[2] == code]
        if len(rates) < 2:
            rows.append({"code": code, "name": ROOM_LABEL.get(code, code),
                         "n": len(rates), "min": min(rates) if rates else None,
                         "max": max(rates) if rates else None,
                         "mean": mean(rates), "median": median(rates),
                         "cv": None, "ratio": None})
            continue
        sd = stdev(rates)
        m = mean(rates)
        cv = (sd / m * 100) if m else None
        ratio = (max(rates) / min(rates)) if min(rates) else None
        rows.append({"code": code, "name": ROOM_LABEL.get(code, code),
                     "n": len(rates), "min": min(rates), "max": max(rates),
                     "mean": m, "median": median(rates),
                     "cv": cv, "ratio": ratio})

    rows_sorted = sorted(rows, key=lambda r: (r["cv"] if r["cv"] is not None else -1), reverse=True)

    lines += ["## Yielding intensity ranking (by CV, descending)", "",
              "| Rank | Room | n cells | min | max | mean | median | CV | min→max ratio |",
              "|---:|---|---:|---:|---:|---:|---:|---:|---:|"]
    for i, r in enumerate(rows_sorted, 1):
        cv_str = f"{r['cv']:.1f}%" if r["cv"] is not None else "—"
        ratio_str = f"{r['ratio']:.2f}×" if r["ratio"] is not None else "—"
        lines.append(
            f"| {i} | {r['name']} ({r['code']}) | {r['n']} | "
            f"{fmt_money(r['min'])} | {fmt_money(r['max'])} | "
            f"{fmt_money(r['mean'])} | {fmt_money(r['median'])} | "
            f"{cv_str} | {ratio_str} |"
        )
    lines.append("")

    # Static vs aggressive interpretation
    lines += ["## Pattern observations", "",
              "- CV measures yielding intensity normalized for rate level. Higher = more aggressive demand-flexing.",
              "- Min→max ratio measures rate-card range. A ratio of 2.0× means the highest-priced cell is double the lowest.",
              "- The volume-tier products (1BD, 1BDPRC, 2BD, 2BDPRC) and the top-tier (PH) sit on different sides of "
              "this distribution. Top-tier inventory is yielded sophisticatedly; volume-tier inventory shows materially "
              "less variation, suggesting the rate card is set and not actively re-yielded across demand windows.",
              ""]
    return "\n".join(lines), {"rows": rows_sorted}


# ------------------------------ Analysis 5 ------------------------------
def analysis_channel_parity(dates, codes, cells) -> tuple[str, dict]:
    lines = ["# Analysis 5 — Channel Parity Detail", "",
             "For each (room category × arrival date) cell where AKA has BAR rates on ≥2 channels, "
             "compute spread between max and min channel rate.",
             ""]

    cells_by_room_date = []  # list of {code, date, rates_by_channel, min, max, spread, spread_pct}
    for code in codes:
        for d in dates:
            rates = {ch: cells.get((ch, d, code)) for ch in CHANNELS}
            present = {ch: v for ch, v in rates.items() if v is not None}
            if len(present) < 2:
                continue
            mn = min(present.values())
            mx = max(present.values())
            spread = mx - mn
            spread_pct = (spread / mn * 100) if mn else 0
            cells_by_room_date.append({
                "code": code, "date": d,
                "rates": present,
                "min": mn, "max": mx,
                "min_channel": min(present, key=present.get),
                "max_channel": max(present, key=present.get),
                "spread": spread, "spread_pct": spread_pct,
            })

    if not cells_by_room_date:
        lines.append("No cells with ≥2 channels present.")
        return "\n".join(lines), {"cells": []}

    spreads = [c["spread_pct"] for c in cells_by_room_date]
    lines += [f"- Cells with ≥2 channels: **n = {len(cells_by_room_date)}**",
              f"- Median spread %: **{fmt_pct(median(spreads), 1)}**  |  "
              f"Mean spread %: {fmt_pct(mean(spreads), 1)}",
              f"- Cells with spread ≥10%: **{sum(1 for s in spreads if s >= 10)}**",
              f"- Cells with spread ≥30%: **{sum(1 for s in spreads if s >= 30)}**",
              ""]

    # By room
    lines += ["## By room category", "",
              "| Room | n cells | median spread % | mean spread % | max spread % | cells ≥30% spread |",
              "|---|---:|---:|---:|---:|---:|"]
    for code in codes:
        rs = [c["spread_pct"] for c in cells_by_room_date if c["code"] == code]
        if not rs:
            lines.append(f"| {ROOM_LABEL.get(code, code)} ({code}) | 0 | — | — | — | — |")
            continue
        lines.append(
            f"| {ROOM_LABEL.get(code, code)} ({code}) | {len(rs)} | "
            f"{fmt_pct(median(rs), 1)} | {fmt_pct(mean(rs), 1)} | "
            f"{fmt_pct(max(rs), 1)} | {sum(1 for s in rs if s >= 30)} |"
        )
    lines.append("")

    # By date
    lines += ["## By date", "",
              "| Date | n cells | median spread % | max spread % | cells ≥30% spread |",
              "|---|---:|---:|---:|---:|"]
    for d in dates:
        rs = [c["spread_pct"] for c in cells_by_room_date if c["date"] == d]
        if not rs:
            lines.append(f"| {d} | 0 | — | — | — |")
            continue
        lines.append(
            f"| {d} | {len(rs)} | {fmt_pct(median(rs), 1)} | "
            f"{fmt_pct(max(rs), 1)} | {sum(1 for s in rs if s >= 30)} |"
        )
    lines.append("")

    # Worst cells (top 15 by spread%)
    worst = sorted(cells_by_room_date, key=lambda c: c["spread_pct"], reverse=True)[:15]
    lines += ["## Worst 15 cells by spread %", "",
              "| Date | Room | min ch | min $ | max ch | max $ | spread $ | spread % |",
              "|---|---|---|---:|---|---:|---:|---:|"]
    for c in worst:
        lines.append(
            f"| {c['date']} | {c['code']} | {CHANNEL_LABEL[c['min_channel']]} | "
            f"{fmt_money(c['min'])} | {CHANNEL_LABEL[c['max_channel']]} | "
            f"{fmt_money(c['max'])} | {fmt_money(c['spread'])} | "
            f"{fmt_pct(c['spread_pct'], 1)} |"
        )
    lines.append("")

    # Channel pairing breakdown
    lines += ["## Channel pairing — pairwise spreads", ""]
    pair_stats = {}
    for c in cells_by_room_date:
        for i, ch1 in enumerate(CHANNELS):
            for ch2 in CHANNELS[i+1:]:
                if ch1 in c["rates"] and ch2 in c["rates"]:
                    a, b = c["rates"][ch1], c["rates"][ch2]
                    p = abs(a - b) / min(a, b) * 100
                    pair_stats.setdefault((ch1, ch2), []).append(p)
    lines.append("| Channel pair | n | median spread % | mean spread % | max spread % |")
    lines.append("|---|---:|---:|---:|---:|")
    for (ch1, ch2), ps in pair_stats.items():
        lines.append(
            f"| {CHANNEL_LABEL[ch1]} ↔ {CHANNEL_LABEL[ch2]} | {len(ps)} | "
            f"{fmt_pct(median(ps), 1)} | {fmt_pct(mean(ps), 1)} | "
            f"{fmt_pct(max(ps), 1)} |"
        )
    lines.append("")

    return "\n".join(lines), {"cells": cells_by_room_date}


# ------------------------------ Sunday tier ladder ------------------------------
# Subject-agnostic — keys on `room_type_canonical` and uses `direct` channel +
# LOS=1 + BAR. Sunday = arrival_date.weekday() == 6 (filtering on arrival_date,
# NOT scrape_date, so the tier rendered is the system's rolling-default Sunday
# rate before RM intervention — the Kerry-Mack framing per 2026-05-07 review).
# SFOEM ladder: King-only progression across standard rooms (isolates view /
# floor / club premiums on a single bedding type) + all 4 suite tiers.
# ADA + queen variants intentionally excluded — they share the same rate
# rungs as the King base on most dates and would flatten the line. Queen
# ladder is a separate analysis if needed.
SUNDAY_TIER_LADDER_ORDER = [
    "hr_king",
    "hr_king_bay",
    "hr_king_bay_balcony",
    "hr_king_water",
    "hr_king_bay_balcony_high",
    "hr_king_club",
    "hr_suite_bay_studio",
    "hr_suite_balcony",
    "hr_suite_luxury",
    "hr_suite_presidential",
]
SUNDAY_TIER_LADDER_MIN_OBS_PENTHOUSE_2BR = 3


def compute_subject_sunday_tier_ladder(
    raw_csv: Path = RAW,
    subject_property: str = SUBJECT_PROPERTY,
    channel: str = "direct",
) -> dict:
    """Median Direct BAR per canonical SKU on Sunday arrival dates only.

    Returns dict shape:
        {
          "rows": [{canonical_room_id, n_sundays, median_usd, min_usd, max_usd}],
          "view_premium_deltas": {
              "1BR_PLATINUM_HSV_minus_1BR_PLATINUM": {value_usd: float|None},
              "2BR_PLATINUM_HSV_minus_2BR_PLATINUM": {value_usd: float|None},
          },
          "n_sundays_observed": int,
        }

    Filters: subject + channel + nights==1 + is_bar==True + rate>0 + Sunday +
    available + non-ADA (drops 1BR_PLATINUM_ACCESSIBLE outright). 2BR_PENTHOUSE_TERRACE
    is hidden if its Sunday observation count is below the threshold above.
    """
    df = pd.read_csv(raw_csv)
    df = df[df["property_id"] == subject_property]
    df = df[df["channel"] == channel]
    df = df[df["nights"] == 1]
    df = df[df["is_bar"] == True]  # noqa: E712
    df = df[df["rate_per_night_usd"] > 0]
    if "availability_status" in df.columns:
        df = df[df["availability_status"] == "available"]
    df = df[df["room_type_canonical"].notna()]
    # Drop ADA / mobility variant — it's a mobility variant, not a tier rung.
    df = df[df["room_type_canonical"] != "1BR_PLATINUM_ACCESSIBLE"]
    df = df[df["room_type_canonical"].isin(SUNDAY_TIER_LADDER_ORDER)].copy()

    df["arrival_date"] = pd.to_datetime(df["arrival_date"])
    df = df[df["arrival_date"].dt.weekday == 6]  # Sunday filter on ARRIVAL date

    # Pick lowest BAR per (date × canonical SKU) — multiple rate plans per cell;
    # cheapest BAR is the canonical reference.
    df = df.sort_values(["arrival_date", "room_type_canonical", "rate_per_night_usd"])
    df = df.drop_duplicates(subset=["arrival_date", "room_type_canonical"], keep="first")

    rows: list = []
    medians: dict[str, float] = {}
    for sku in SUNDAY_TIER_LADDER_ORDER:
        sub = df[df["room_type_canonical"] == sku]
        n = int(len(sub))
        if n == 0:
            continue
        if sku == "2BR_PENTHOUSE_TERRACE" and n < SUNDAY_TIER_LADDER_MIN_OBS_PENTHOUSE_2BR:
            continue
        med = float(sub["rate_per_night_usd"].median())
        medians[sku] = med
        rows.append({
            "canonical_room_id": sku,
            "n_sundays": n,
            "median_usd": med,
            "min_usd": float(sub["rate_per_night_usd"].min()),
            "max_usd": float(sub["rate_per_night_usd"].max()),
        })

    def _delta(view_sku: str, base_sku: str) -> dict:
        if view_sku in medians and base_sku in medians:
            return {"value_usd": medians[view_sku] - medians[base_sku]}
        return {"value_usd": None}

    deltas = {
        "1BR_PLATINUM_HSV_minus_1BR_PLATINUM": _delta("1BR_PLATINUM_HSV", "1BR_PLATINUM"),
        "2BR_PLATINUM_HSV_minus_2BR_PLATINUM": _delta("2BR_PLATINUM_HSV", "2BR_PLATINUM"),
    }
    n_sundays_observed = int(df["arrival_date"].nunique())
    return {
        "rows": rows,
        "view_premium_deltas": deltas,
        "n_sundays_observed": n_sundays_observed,
    }


# ------------------------------ Main ------------------------------
def main(argv=None):
    """Build the dashboard payload. `argv` is forwarded to argparse — pass None
    (the default) to consume sys.argv, or a list to invoke main() programmatically
    from tests / orchestrators without mutating process state."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--lighthouse-csv", default=str(DEFAULT_LH_CSV),
                    help="Path to Lighthouse long-format CSV (default: sibling Lighthouse/ dir).")
    ap.add_argument("--lighthouse-only", action="store_true",
                    help="Skip scrape-derived (raw_rates.csv) pipeline and AKA-empirical "
                         "threshold asserts. For first-build SFOEM rendering before scrape "
                         "data exists and before Phase 4 anchor-validate has set per-deal "
                         "thresholds.")
    args = ap.parse_args(argv)

    if args.lighthouse_only:
        # Stub everything the scrape pipeline would have produced, so the
        # Lighthouse integration block (and the data.js write) can run.
        df = pd.DataFrame()
        cells = {}
        dates = []
        codes = []
        plans_by_cell = {}
        hc_check = {
            "los_1": {"n": 0, "min_ratio": 0.0, "max_ratio": 0.0, "suspect_count": 0},
            "los_7": {"n": 0, "suspect_count": 0},
        }
        payload2 = {"pairs": []}
        payload3 = {}
        payload5 = {"cells": []}
        print("--lighthouse-only: skipping scrape pipeline (raw_rates.csv) and "
              "AKA-empirical invariant asserts.")
    else:
        df = load_aka()
        cells = cell_table(df)

        dates = sorted(df["arrival_date"].unique().tolist())
        codes = [c for c in ROOM_ORDER if c in df["mapped_internal_code"].unique().tolist()]
        # Add any other codes present (e.g. 1BDADA if it survived ada filter)
        for c in df["mapped_internal_code"].unique().tolist():
            if c not in codes:
                codes.append(c)

        # Drop UNMAPPED (one-off inventory: Three Bedroom Premium Suite, Studio Suite, etc.)
        if "UNMAPPED" in codes:
            codes = [c for c in codes if c != "UNMAPPED"]
        cells = {k: v for k, v in cells.items() if k[2] != "UNMAPPED"}
        df = df[df["mapped_internal_code"] != "UNMAPPED"].reset_index(drop=True)

        print(f"Loaded {len(df)} AKA BAR cells across {len(dates)} dates and {len(codes)} room codes (UNMAPPED excluded).")
        print(f"Dates: {dates}")
        print(f"Codes: {codes}")

        # Rate-plan-matched view-premium engine: load all refundable rate-plan rows
        df_plans = load_refundable_rate_plans()
        plans_by_cell = rate_plans_by_cell(df_plans)
        print(f"Loaded {len(df_plans)} refundable rate-plan rows across {len(plans_by_cell)} (channel,date,code) cells.")

        # Hotels.com sanity check
        hc_check = hotels_com_sanity_check(RAW)
        los1 = hc_check["los_1"]
        los7 = hc_check["los_7"]
        if los1["n"] == 0:
            print("Hotels.com sanity LOS=1: no Hotels.com data captured (Direct-only seed).")
        else:
            print(f"Hotels.com sanity LOS=1 (analysis filter): n={los1['n']} rows, "
                  f"ratio range {los1['min_ratio']:.3f}-{los1['max_ratio']:.3f}, "
                  f"suspect (>1.10) count={los1['suspect_count']}.")
        print(f"Hotels.com sanity LOS=7: n={los7['n']} rows, "
              f"suspect={los7['suspect_count']} (LOS=7 is NOT used in this analysis; flagged for transparency).")

        # Analysis 1
        md1 = analysis_rate_matrix(df, dates, codes, cells)
        (ANALYSIS / "aka_rate_matrix.md").write_text(md1, encoding="utf-8")

        # Analysis 2 — rate-plan matched
        md2, payload2 = analysis_view_premium(dates, plans_by_cell)
        (ANALYSIS / "aka_view_premium_through_time.md").write_text(md2, encoding="utf-8")

        # Analysis 3
        md3, payload3 = analysis_tier_stepups(dates, cells)
        (ANALYSIS / "aka_tier_stepups_through_time.md").write_text(md3, encoding="utf-8")

        # Analysis 4
        md4, payload4 = analysis_yielding_intensity(dates, codes, cells)
        (ANALYSIS / "aka_yielding_intensity.md").write_text(md4, encoding="utf-8")

        # Analysis 5
        md5, payload5 = analysis_channel_parity(dates, codes, cells)
        (ANALYSIS / "aka_channel_parity_detail.md").write_text(md5, encoding="utf-8")

    # ---------- Build dashboard payload ----------

    # Headline view-premium summary (median $ and %)
    def pair_stats(pair_rows):
        ds = [r["delta"] for r in pair_rows]
        ps = [r["pct"] for r in pair_rows]
        return {
            "n": len(pair_rows),
            "median_delta": median(ds), "mean_delta": mean(ds),
            "median_pct": median(ps), "mean_pct": mean(ps),
            "min_delta": (min(ds) if ds else None), "max_delta": (max(ds) if ds else None),
            "neg_count": sum(1 for x in ds if x < 0),
            "zero_count": sum(1 for x in ds if x == 0),
        }
    headline_vp = {p["label"]: pair_stats(p["rows"]) for p in payload2["pairs"]}

    # Coverage
    coverage = {
        "n_dates": len(dates),
        "n_room_codes": len(codes),
        "n_cells": len(cells),
        "by_channel": {ch: sum(1 for k in cells if k[0] == ch) for ch in CHANNELS},
    }

    # ---------- Part 1 extension payloads (loaded from analysis/_part1_payload.json) ----------
    part1_payload_path = ANALYSIS / "_part1_payload.json"
    if part1_payload_path.exists():
        try:
            part1 = json.loads(part1_payload_path.read_text(encoding="utf-8"))
        except Exception as ex:
            print(f"  WARN: failed to load _part1_payload.json: {ex}")
            part1 = None
    else:
        print("  WARN: analysis/_part1_payload.json missing — Part 1 sections will not render.")
        part1 = None

    # ---------- Part 2 booking-window snapshot meta (if SCHEDULE.md exists) ----------
    schedule_path = OUT / "SCHEDULE.md"
    booking_window_meta = None
    if schedule_path.exists():
        sched_text = schedule_path.read_text(encoding="utf-8")
        # Headline strategic dates pulled from the schedule
        booking_window_meta = {
            "schedule_present": True,
            "schedule_summary": "Day 0 baseline scraped; follow-ups scheduled at Day 3/7/10/14 — see SCHEDULE.md.",
        }

    # AKA Sunday tier ladder (Section 4 — replaces the old multi-panel tier-spread).
    # Direct channel + Sunday arrival dates only (Kerry Mack 2026-05-07 framing).
    if args.lighthouse_only:
        aka_sunday_tier_ladder = {"n_sundays_observed": 0, "rows": []}
    else:
        aka_sunday_tier_ladder = compute_subject_sunday_tier_ladder()
        print(
            f"\n--- AKA Sunday tier ladder (Section 4): "
            f"{aka_sunday_tier_ladder['n_sundays_observed']} Sundays observed, "
            f"{len(aka_sunday_tier_ladder['rows'])} SKUs rendered ---"
        )
        for r in aka_sunday_tier_ladder["rows"]:
            print(f"  {r['canonical_room_id']:<26} median=${r['median_usd']:.0f} (n={r['n_sundays']})")

    firecrawl_payload = {
        "coverage": coverage,
        "headline_view_premium": headline_vp,
        "tier_stepups": payload3,
        "aka_sunday_tier_ladder": aka_sunday_tier_ladder,
        # Section 6 raw explorer reads channel_parity.cells from this payload —
        # underlying Firecrawl rows still surface there even though the
        # Section 5 parity rendering was removed on 2026-05-07.
        "channel_parity": payload5,
        "hotels_com_sanity": hc_check,
        # Part 1 extension data — wired into the dashboard sections added 2026-04-26 (afternoon).
        "part1": part1,
        "booking_window": booking_window_meta,
    }

    # ---------- Lighthouse integration (Phase 2) ----------
    lh = pd.read_csv(args.lighthouse_csv)
    print(f"\nLoaded {len(lh)} Lighthouse rows from {args.lighthouse_csv}")

    # Park Central drop invariant — assert before any analytics function runs.
    assert "park_central" not in set(lh["property"].unique()), \
        "park_central present in Lighthouse CSV — drop invariant violated"

    rm_verdict = lh_analytics.compute_rm_verdict(lh)
    comp_set_verdicts = lh_analytics.compute_comp_set_verdicts(lh)
    dow_pattern = lh_analytics.compute_dow_pattern(lh)
    scatter = lh_analytics.compute_demand_correlation_scatter(lh)
    compression_with_comp = lh_analytics.compute_compression_response_with_comp_baseline(lh, top_n=60)
    yielding_intensity = lh_analytics.compute_yielding_intensity_series(lh)
    market_demand = lh_analytics.compute_market_demand_heatmap(lh)
    los_grid = lh_analytics.compute_los_restrictions_grid(lh)
    flatness_scorecard = lh_analytics.compute_flatness_scorecard(lh)
    subject_vs_comp = lh_analytics.compute_subject_vs_comp_median_spread(lh)
    subject_vs_comp_audit = lh_analytics.compute_subject_vs_comp_high_bucket_audit(lh, subject_vs_comp)
    subject_vs_comp["high_bucket_audit"] = subject_vs_comp_audit
    print(
        f"\n--- Subject-vs-comp-median spread (Section 3): "
        f"delta_typical=${subject_vs_comp['headlines']['delta_typical_usd']}, "
        f"delta_high=${subject_vs_comp['headlines']['delta_high_usd']}, "
        f"spread_movement=${subject_vs_comp['headlines']['spread_movement_usd']}, "
        f"verdict={subject_vs_comp['verdict']['classification']} ---"
    )
    print(
        f"  Bucket sizes: n_normal={subject_vs_comp['headlines']['n_normal']}, "
        f"n_shoulder={subject_vs_comp['headlines']['n_shoulder']}, "
        f"n_high={subject_vs_comp['headlines']['n_high']}"
    )
    sens = subject_vs_comp_audit["sensitivity"]
    if sens["delta_high_full"] is not None and sens["shift_usd"] is not None:
        print(
            f"  Sensitivity: delta_high (as-is)=${sens['delta_high_asis']:.0f} "
            f"vs (comp_n>=4)=${sens['delta_high_full']:.0f}; "
            f"shift={sens['shift_usd']:+.0f} (robust if abs(shift) < ${lh_analytics.SUBJECT_VS_COMP_NOISE_FLOOR_USD:.0f})"
        )
    compset_rate_lines_by_quarter = lh_analytics.compute_compset_rate_lines_by_quarter(lh)
    compset_flat_stretches = lh_analytics.compute_compset_flat_stretches(lh)
    lighthouse_rates_explorer = lh_analytics.compute_lighthouse_rates_explorer(lh)
    headline_kpis = lh_analytics.compute_headline_kpis(lh, firecrawl_payload, rm_verdict)

    # ---------- Capital Hilton control — IC-relevant comparative observation ----------
    # Capital Hilton is a sophisticated Hilton-RMS comp; if AKA's RM-discipline metrics
    # come in materially below CH's on BOTH demand response and DOW range, that's a
    # comp-positioning finding regardless of how AKA's own verdict lands.
    ch_verdict = lh_analytics.compute_rm_verdict(lh, subject=CONTROL_COMP_PROPERTY)

    def _r(verdict, key):
        return verdict["rules"]["demand_response"]["sub_rules"][key].get("value")

    aka_1a = _r(rm_verdict, "1a")
    aka_1b = _r(rm_verdict, "1b")
    aka_1bl = _r(rm_verdict, "1b_levels")
    ch_1a = _r(ch_verdict, "1a")
    ch_1b = _r(ch_verdict, "1b")
    ch_1bl = _r(ch_verdict, "1b_levels")
    aka_dow = rm_verdict["rules"]["dow_normalized_range"].get("value")
    ch_dow = ch_verdict["rules"]["dow_normalized_range"].get("value")

    def _fmt(v):
        return f"{v:+.3f}" if isinstance(v, (int, float)) else "n/a"

    if not args.lighthouse_only:
        print("\n" + "=" * 78)
        print("  IC-RELEVANT COMPARATIVE: AKA vs CAPITAL HILTON (Hilton-RMS sophistication)")
        print("=" * 78)
        print(f"  AKA verdict: {rm_verdict['display_label']}")
        print(f"  CH  verdict: {ch_verdict['display_label']}")
        print()
        print(f"  Rule 1a  demand correlation 60d (gated):           "
              f"AKA r={_fmt(aka_1a)}  vs  CH r={_fmt(ch_1a)}")
        print(f"  Rule 1b  OTB first-differences 365d (gated):       "
              f"AKA r={_fmt(aka_1b)}  vs  CH r={_fmt(ch_1b)}")
        print(f"  Rule 1b_levels  OTB raw-level 365d (informational): "
              f"AKA r={_fmt(aka_1bl)}  vs  CH r={_fmt(ch_1bl)}")
        print(f"  Rule 2   DOW normalized range:                     "
              f"AKA  {_fmt(aka_dow)}  vs  CH  {_fmt(ch_dow)}")
        print()
        # IC interpretation guide.
        print("  Interpretation guide:")
        print("    1b_levels high + 1b first-diff low ==> calendar trend-following")
        print("                                            without tactical yielding.")
        print("    1a high + DOW range high           ==> tactical near-in + DOW yielding")
        print("                                            (Gopu archetype).")
        print("=" * 78)

    # ---------- Phase 2 verification gate — deterministic numeric invariants ----------
    # Symmetric bounds check: all six correlations (AKA + CH × 1a/1b/1b_levels)
    # plus the informational 1b_levels — anything in the verdict object that
    # claims to be a Pearson r should land in [-1, 1].
    for r_val, name in [
        (aka_1a, "AKA Rule 1a"),
        (aka_1b, "AKA Rule 1b first-diff"),
        (aka_1bl, "AKA Rule 1b_levels"),
        (ch_1a, "CH Rule 1a"),
        (ch_1b, "CH Rule 1b first-diff"),
        (ch_1bl, "CH Rule 1b_levels"),
    ]:
        if r_val is not None:
            assert -1.0 <= r_val <= 1.0, f"{name} correlation out of [-1,1]: {r_val}"
    for dow_val, name in [(aka_dow, "AKA dow_range"), (ch_dow, "CH dow_range")]:
        if dow_val is not None:
            assert dow_val >= 0, f"{name} negative: {dow_val}"
    for k, v in headline_kpis.items():
        val = v.get("value")
        if isinstance(val, (int, float)):
            assert math.isfinite(val), f"KPI {k} not finite: {val}"
    assert len(lh) >= 1500, f"lighthouse rows below threshold: {len(lh)}"
    assert lh["arrival_date"].nunique() >= 360, \
        f"unique arrival_dates below threshold: {lh['arrival_date'].nunique()}"
    assert len(headline_kpis) == 6, f"expected 6 headline KPIs, got {len(headline_kpis)}"

    # Compression-event response invariants. The 60 dates returned are top by
    # demand intensity; demand_frac must be in [0,1]; both Δ% values are bounded
    # generously since extreme values still surface as red-row anti-yielding signals.
    assert 1 <= len(compression_with_comp) <= 60, \
        f"compression_with_comp size out of [1,60]: {len(compression_with_comp)}"
    for r in compression_with_comp:
        assert 0.0 <= r["demand_frac"] <= 1.0, f"demand_frac out of [0,1]: {r}"
        for k in ("aka_pct_above_baseline", "comp_median_pct_above_baseline"):
            v = r[k]
            if v is not None:
                assert -1.0 <= v <= 5.0, f"{k} out of [-1,5]: {r}"
    aka_anti = sum(1 for r in compression_with_comp
                   if r["aka_pct_above_baseline"] is not None
                   and r["aka_pct_above_baseline"] < -0.10)
    print(f"Phase 2 numeric invariants: OK")
    print(f"  Compression dates: {len(compression_with_comp)} top by demand; "
          f"{aka_anti} flagged AKA delta_pct < -10% (anti-yielding rows)")

    # ---- Yielding-intensity series invariants (dual-baseline; 2026-05-06 refactor) ----
    # Baseline replaced 30d trailing MEAN with DOW-stratified annual MEDIAN +
    # plateau-aware override. Invariants calibrated against empirical data
    # after the dual-baseline refactor (the brief's original "AKA has one
    # yielding event" hypothesis was wrong against the data — AKA's BAR has
    # multiple distinct compression clusters where it yields aggressively):
    #   - AKA mean smoothed delta_pct within +/- 15%. Plateau-pinned dates
    #     (median = 0%) drag the mean toward 0; the high-amplitude compression
    #     clusters drag it positive. Empirical observation +12.46% sits inside
    #     this band; ±15% catches both "data shifted" and "plateau detection
    #     broke" failure modes without false-positive on healthy noise.
    #   - AKA p95 smoothed delta_pct <= +130% — empirically +119.71% (driven
    #     by early-May DC compression cluster + Cherry Blossom + July 4).
    #     If p95 exceeds +130% the data has materially shifted.
    #   - Plateau dates flagged for AKA >= 150 — empirically 159 (six
    #     plateaus: $302 / $185 / $356 / $320 / $176 / $356, matching the six
    #     flat-stretch annotations on the quarterly panels).
    #   - Comp median p95 smoothed delta_pct >= +25% on >= 3 of 5 comps —
    #     all 5 comps clear empirically (Hay-Adams +21.05% is the floor and
    #     just misses; rest are +51% to +73%). Threshold kept at >= 3.
    #   - Days where (comp_median - aka) >= +20% must number >= 30 —
    #     empirically 66 (despite AKA having multiple yielding clusters,
    #     comps still yield more days than AKA).
    # Failing any invariant means the data is telling a different story than
    # the diligence narrative — surface and stop, do not push.
    yi_per_prop = yielding_intensity["per_property"]
    yi_composite = yielding_intensity["composite"]
    by_prop_pct: dict[str, list[float]] = {}
    aka_plateau_count = 0
    aka_prop = SUBJECT_PROPERTY
    for r in yi_per_prop:
        by_prop_pct.setdefault(r["property"], []).append(r["pct_above_baseline_smoothed"])
        if r["property"] == aka_prop and r.get("on_plateau"):
            aka_plateau_count += 1
    print("\n--- Yielding-intensity series (dual baseline: DOW median + plateau override; 7d smoothed) ---")
    print(f"  Per-property cells: {len(yi_per_prop)} across {len(by_prop_pct)} properties; "
          f"composite rows: {len(yi_composite)}")
    print(f"  AKA plateau-flagged dates: {aka_plateau_count} (delta_pct pinned to 0 on these dates)")
    print(f"  {'property':<20} {'n':>5} {'mean':>8} {'median':>8} {'p95':>8} {'min':>8} {'max':>8}")
    aka_mean_pct = None
    aka_p95_pct = None
    comp_p95_passes_25 = 0
    comps_only = [p for p in by_prop_pct if p != aka_prop]
    for prop in sorted(by_prop_pct):
        vals = sorted(by_prop_pct[prop])
        n = len(vals)
        mean_v = sum(vals) / n
        median_v = vals[n // 2] if n % 2 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])
        p95_v = vals[max(0, int(round(0.95 * (n - 1))))] if n else 0.0
        print(f"  {prop:<20} {n:>5} {mean_v * 100:>7.2f}% {median_v * 100:>7.2f}% "
              f"{p95_v * 100:>7.2f}% {min(vals) * 100:>7.2f}% {max(vals) * 100:>7.2f}%")
        if prop == aka_prop:
            aka_mean_pct = mean_v
            aka_p95_pct = p95_v
        elif p95_v >= 0.25:
            comp_p95_passes_25 += 1

    diag_dates = [r for r in yi_composite
                  if r["aka_pct"] is not None
                  and r["comp_median_pct"] is not None
                  and (r["comp_median_pct"] - r["aka_pct"]) >= 0.20]
    print(f"  Dates where (comp_median - aka) >= +20%: {len(diag_dates)} "
          f"(diagnostic — comps yielding while AKA holds flat)")

    assert aka_mean_pct is not None, "yielding-intensity: no subject cells in series"
    assert aka_p95_pct is not None, "yielding-intensity: no subject p95 in series"
    # The five thresholds below were calibrated against AKA empirical data
    # (DC luxury). For SFOEM and any other deal pre-Phase-4-anchor-validate,
    # they are not yet meaningful — degrade to warnings under --lighthouse-only.
    yi_checks = [
        (abs(aka_mean_pct) <= 0.15,
         f"subject mean smoothed delta_pct = {aka_mean_pct * 100:+.2f}% outside +/- 15% (AKA empirical baseline)"),
        (aka_p95_pct <= 1.30,
         f"subject p95 smoothed delta_pct = {aka_p95_pct * 100:+.2f}% exceeds +130% (AKA empirical baseline)"),
        (aka_plateau_count >= 150,
         f"only {aka_plateau_count} subject plateau-flagged dates (expected >= 150 per AKA empirical baseline)"),
        (comp_p95_passes_25 >= 3,
         f"only {comp_p95_passes_25} of {len(comps_only)} comps show smoothed p95 delta_pct >= +25%"),
        (len(diag_dates) >= 30,
         f"only {len(diag_dates)} dates show (comp_median - subject) >= +20%"),
    ]
    # AKA-empirical thresholds are calibrated against aka_white_house. For
    # any non-AKA subject (e.g. hr_embarcadero), the asserts aren't yet
    # meaningful pre-Phase-4-anchor-validate — degrade to WARN, same as the
    # --lighthouse-only escape hatch.
    is_aka = SUBJECT_PROPERTY == "aka_white_house"
    warns_fired = 0
    for cond, msg in yi_checks:
        if cond:
            continue
        warns_fired += 1
        if args.lighthouse_only or not is_aka:
            mode_tag = "lighthouse-only" if args.lighthouse_only else f"non-AKA subject ({SUBJECT_PROPERTY})"
            print(f"  WARN [{mode_tag}, AKA-empirical baseline]: {msg}")
        else:
            raise AssertionError(f"yielding-intensity invariant FAIL: {msg} — investigate before pushing.")
    if not args.lighthouse_only and warns_fired == 0:
        print(f"  subject mean smoothed delta_pct = {aka_mean_pct * 100:+.2f}% (within +/- 15%): OK")
        print(f"  subject p95  smoothed delta_pct = {aka_p95_pct * 100:+.2f}% (<= +130%): OK")
        print(f"  subject plateau-flagged dates: {aka_plateau_count} (>= 150 required): OK")
        print(f"  Comps with smoothed p95 >= +25%: {comp_p95_passes_25} of "
              f"{len(comps_only)} (>=3 required): OK")
        print(f"  Diagnostic dates (comp_median - subject >= +20%): {len(diag_dates)} "
              f"(>=30 required): OK")

    # ---- Section 6 lighthouse_rates_explorer payload sanity ----
    # Same filter as the Section 1 quarterly panels and Section 3 subject-vs-
    # comp-median spread: AKA on suite-tier, comps on any-tier, brandcom +
    # available only. Every property must clear >= 200 rate cells across the
    # forward year — drops below that suggest the filter regressed (e.g. tier
    # flag changed shape). Spot-checks AKA's 2026-05-12 = $1,044 and
    # 2026-07-04 = $1,391 cells — those are the empirical proof points cited
    # in Section 3's high-bucket sample; mismatch means the filter is wrong
    # and the verdict is now disconnected from the explorer rows.
    explorer_payload_kb = (
        len(json.dumps(lighthouse_rates_explorer, default=str).encode("utf-8")) / 1024
    )
    by_prop_count: dict[str, int] = {}
    by_prop_rates: dict[str, list[float]] = {}
    cells_with_obs: set[tuple[str, str]] = set()
    # TODO Phase 4 anchor-validate: capture per-deal spot-check rates here
    # (the AKA template captured aka_may12 / aka_jul4 — golden-snapshot
    # values from the IC verdict). For this deal, harvest two arrival
    # dates Andrew flags as Section 3 high-bucket exemplars.
    for r in lighthouse_rates_explorer:
        by_prop_count[r["property"]] = by_prop_count.get(r["property"], 0) + 1
        by_prop_rates.setdefault(r["property"], []).append(r["rate_usd"])
        cells_with_obs.add((r["property"], r["arrival_date"]))
    print(
        f"\n--- Section 6 Lighthouse rates explorer (per-property × per-date) ---"
    )
    print(
        f"  Total rows: {len(lighthouse_rates_explorer)}; "
        f"unique (property, arrival_date) cells: {len(cells_with_obs)}; "
        f"payload size: {explorer_payload_kb:.1f} KB"
    )
    print(f"  {'property':<22} {'n':>5} {'min':>8} {'median':>9} {'max':>9}")
    for p in sorted(by_prop_count):
        n = by_prop_count[p]
        rates = sorted(by_prop_rates[p])
        med = (rates[n // 2] if n % 2 else 0.5 * (rates[n // 2 - 1] + rates[n // 2]))
        print(f"  {p:<22} {n:>5} {min(rates):>8.0f} {med:>9.0f} {max(rates):>9.0f}")

    # TODO Phase 4 anchor-validate: replace with golden-snapshot values from this
    # build's first known-good run. The assertion pattern (per-property minimum
    # cell-count + two date-anchored spot checks) is reusable methodology; the
    # specific numeric thresholds are per-deal snapshots persisted to
    # build_snapshot.json after a verified first-known-good build.
    if not all(c >= 200 for c in by_prop_count.values()):
        msg = (f"explorer: a property has < 200 rate cells: {by_prop_count}. "
               f"Filter likely regressed.")
        if args.lighthouse_only or not is_aka:
            mode_tag = "lighthouse-only" if args.lighthouse_only else f"non-AKA subject ({SUBJECT_PROPERTY})"
            print(f"  WARN [{mode_tag}, AKA-empirical baseline]: {msg}")
        else:
            raise AssertionError(msg + " — investigate before pushing.")
    else:
        print(f"  All {len(by_prop_count)} properties have >= 200 rate cells: OK")
    if explorer_payload_kb > 500:
        print(
            f"  WARNING: explorer payload {explorer_payload_kb:.1f} KB exceeds 500 KB "
            f"guidance — surface to Andrew before pushing."
        )
    # TODO Phase 4 anchor-validate: print golden-snapshot spot-check confirmations
    # corresponding to the assertions above (per-deal anchor dates + values).

    payload = {
        "meta": {
            "firecrawl_source": "raw_rates.csv (post anchor-purge 2026-04-25)",
            "lighthouse_source": str(args.lighthouse_csv),
            "lighthouse_rows": int(len(lh)),
            "lighthouse_as_of": str(lh["as_of_date"].iloc[0]) if len(lh) else None,
            "lighthouse_comps_present": sorted(lh["property"].unique().tolist()),
            "filters_rate_matrix": "property=hr_embarcadero, LOS=1, is_bar=True, rate>0; lowest non-ADA per cell",
            "filters_view_premium": "property=hr_embarcadero, LOS=1, refundable=True; rate-plan-matched (revised 2026-04-26)",
            "generated": pd.Timestamp.utcnow().isoformat(),
            "channels": CHANNELS,
            "channel_labels": CHANNEL_LABEL,
            "dates": dates,
            "codes": codes,
            "code_labels": ROOM_LABEL,
            # kpi_snapshot is intentionally the same dict as derived.headline_kpis
            # below — duplicated under meta so Phase 3 dashboard.js can do UI ↔
            # snapshot reconciliation (golden-snapshot pattern from the plan, Phase 3
            # verification gate). Do NOT dedupe — the duplication is load-bearing.
            "kpi_snapshot": headline_kpis,
        },
        "firecrawl": firecrawl_payload,
        "lighthouse": {
            "rows_total": int(len(lh)),
            "market_demand": market_demand,
            "los_restrictions": los_grid,
            "flatness_scorecard": flatness_scorecard,
            # NOTE: full long-format `raw_rates` deferred to Phase 3 to keep data.js < 5 MB.
            # Section 6 raw explorer can fetch lighthouse_rates.csv lazily if needed.
        },
        "derived": {
            # Section 3 metric (replaces the deleted DOW-stratified rm_verdict
            # construct rejected by Kerry Mack on 2026-05-07).
            "subject_vs_comp_median_spread": subject_vs_comp,
            "compset_rate_lines_by_quarter": compset_rate_lines_by_quarter,
            "compset_flat_stretches": compset_flat_stretches,
            "lighthouse_rates_explorer": lighthouse_rates_explorer,
            "headline_kpis": headline_kpis,
        },
    }

    js_text = "window.DASHBOARD_DATA = " + json.dumps(payload, indent=2, default=str) + ";\n"
    (OUT / "data.js").write_text(js_text, encoding="utf-8")
    js_size_mb = (OUT / "data.js").stat().st_size / (1024 * 1024)
    print(f"\nWrote data.js ({js_size_mb:.2f} MB)")
    if js_size_mb > 5.0:
        print(f"WARNING: data.js exceeds 5 MB threshold from plan ({js_size_mb:.2f} MB).")

    # ---------- Print summary stats for sanity check ----------
    print("\n--- Headline summary ---")
    for label, s in headline_vp.items():
        if s["n"]:
            print(f"  {label}: n={s['n']}  median delta_dollar=${s['median_delta']:.2f}  median delta_pct={s['median_pct']:.2f}%")
    print(f"  Total BAR cells: {len(cells)}")
    print(f"\n  Embarcadero verdict: {rm_verdict['display_label']}")
    r1 = rm_verdict["rules"]["demand_response"]
    sa = r1["sub_rules"]["1a"]
    sb = r1["sub_rules"]["1b"]
    sbl = r1["sub_rules"]["1b_levels"]
    print(f"  Rule 1 (demand response): pass={r1['pass']} (gated on 1a AND 1b)")
    print(f"    1a demand 60d (gated):                  r={sa['value']}, thr={sa['threshold']}, pass={sa['pass']}, low_power={sa['low_power']} (n={sa['n_used']})")
    print(f"    1b OTB first-diff 365d (gated):         r={sb['value']}, thr={sb['threshold']}, pass={sb['pass']}, low_power={sb['low_power']} (n={sb['n_used']})")
    print(f"    1b_levels OTB raw-level (informational): r={sbl['value']}, thr={sbl['threshold']}, low_power={sbl['low_power']} (n={sbl['n_used']})")
    rd = rm_verdict["rules"]["dow_normalized_range"]
    print(f"  Rule 2 (DOW normalized range): {rd['value']}, threshold={rd['threshold']}, pass={rd['pass']}, low_power={rd['low_power']}")
    print(f"  Rule 3 (compression lead-time): n/a -- multi-pull stack required")

    return payload


if __name__ == "__main__":
    main()
