"""Phase 5 sensitivity analysis on Section 1 plateau detection thresholds.

Runs `detect_plateaus()` across the 6 LH_COMP_SET properties at four threshold
combinations and reports pct_flat + longest_plateau_days per (property × combo).
Replicates the inner logic of `compute_flatness_scorecard` but lets the caller
override `min_length_days` and `max_dod_pct`.

Threshold combinations:
  - default:   min=10d, dod=5%   (the current dashboard default)
  - tightened: min=10d, dod=3%
  - loosened:  min=10d, dod=10%
  - longer:    min=14d, dod=5%

Pass criteria:
  - AKA pct_flat >= 2x the comp_median pct_flat at every threshold combo
  - Capital Hilton AND Willard pct_flat < 10% under the loosened 10%-DoD combo

Run:
    python scripts/phase5_plateau_sensitivity.py
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import analytics_lighthouse as la  # noqa: E402

LH_CSV = ROOT.parent / "AKA White House" / "Lighthouse" / "lighthouse_rates.csv"


def per_property(lh: pd.DataFrame, prop: str, min_length: int, max_dod: float) -> dict:
    sub = lh[
        (lh["source"] == "brandcom")
        & (lh["room_tier"] == "any")
        & (lh["property"] == prop)
        & (lh["availability_status"] == "available")
        & lh["rate_usd"].notna()
    ][["arrival_date", "rate_usd"]].copy()
    if sub.empty:
        return {"property": prop, "n_observed": 0, "n_on_plateau": 0,
                "pct_flat": None, "longest_plateau_days": 0}

    sub["arrival_date"] = pd.to_datetime(sub["arrival_date"])
    sub = sub.sort_values("arrival_date")
    rate_series = sub.set_index("arrival_date")["rate_usd"].astype(float)

    plateaus = la.detect_plateaus(
        rate_series,
        min_length_days=min_length,
        max_dod_pct=max_dod,
    )
    on_plateau = pd.Series(False, index=rate_series.index)
    plateau_days_each: list[int] = []
    for start, end, _med in plateaus:
        on_plateau.loc[start:end] = True
        plateau_days_each.append(int((end - start).days) + 1)

    n_observed = int(len(rate_series.index))
    n_on_plateau = int(on_plateau.sum())
    pct_flat = (n_on_plateau / n_observed) if n_observed else None
    longest = max(plateau_days_each) if plateau_days_each else 0
    return {
        "property": prop,
        "n_observed": n_observed,
        "n_on_plateau": n_on_plateau,
        "pct_flat": pct_flat,
        "longest_plateau_days": longest,
    }


COMBOS = [
    ("default",   10, 0.05),
    ("tightened", 10, 0.03),
    ("loosened",  10, 0.10),
    ("longer",    14, 0.05),
]


def main() -> int:
    lh = pd.read_csv(LH_CSV)

    # Compute per (combo, property)
    results: dict[str, dict[str, dict]] = {label: {} for label, _, _ in COMBOS}
    for label, ml, dod in COMBOS:
        for prop in la.LH_COMP_SET:
            results[label][prop] = per_property(lh, prop, ml, dod)

    print("=" * 96)
    print("  Section 1 plateau-detection sensitivity — pct_flat / longest_plateau_days per combo")
    print("=" * 96)

    # ---------- raw numbers, plain text ----------
    label_pad = max(len(p) for p in la.LH_COMP_SET)
    header = f"  {'property':<{label_pad}}  " + "  ".join(f"{lab:<22}" for lab, _, _ in COMBOS)
    print(header)
    for prop in la.LH_COMP_SET:
        cells = []
        for lab, _, _ in COMBOS:
            r = results[lab][prop]
            pct = "—" if r["pct_flat"] is None else f"{round(r['pct_flat']*100):>3}%"
            longest = f"{r['longest_plateau_days']:>3}d"
            cells.append(f"{pct} / {longest}".ljust(22))
        print(f"  {prop:<{label_pad}}  " + "  ".join(cells))

    # ---------- markdown table ----------
    print()
    print("Markdown:")
    print()
    print("| Property | " + " | ".join(
        f"{lab} ({ml}d, {int(dod*100)}% DoD)" for lab, ml, dod in COMBOS
    ) + " |")
    print("|---" * (1 + len(COMBOS)) + "|")
    PROP_LABEL = {
        "aka_white_house": "**AKA White House**",
        "hay_adams": "Hay-Adams",
        "capital_hilton": "Capital Hilton",
        "jefferson": "The Jefferson",
        "st_regis": "St Regis",
        "willard": "Willard",
    }
    for prop in la.LH_COMP_SET:
        cells = []
        for lab, _, _ in COMBOS:
            r = results[lab][prop]
            if r["pct_flat"] is None:
                cells.append("—")
            else:
                cells.append(f"{round(r['pct_flat']*100)}% / {r['longest_plateau_days']}d")
        print(f"| {PROP_LABEL[prop]} | " + " | ".join(cells) + " |")

    # ---------- pass-criteria check ----------
    print()
    print("=" * 96)
    print("  PASS-CRITERIA CHECK")
    print("=" * 96)
    failures: list[str] = []
    summary_lines: list[str] = []
    for lab, _, _ in COMBOS:
        aka = results[lab]["aka_white_house"]["pct_flat"]
        comps = [results[lab][p]["pct_flat"] for p in la.LH_COMP_SET if p != "aka_white_house"]
        comp_clean = [c for c in comps if c is not None]
        comp_median = statistics.median(comp_clean) if comp_clean else None
        ratio = (aka / comp_median) if (aka is not None and comp_median and comp_median > 0) else None
        ratio_str = f"{ratio:.1f}x" if ratio is not None else ("inf" if (aka and not comp_median) else "-")
        pass_aka = (ratio is not None and ratio >= 2.0) or (aka is not None and aka > 0 and (comp_median in (None, 0)))
        tag = "OK" if pass_aka else "FAIL"
        akapct = "—" if aka is None else f"{round(aka*100)}%"
        cmpct = "—" if comp_median is None else f"{round(comp_median*100)}%"
        line = f"  [{lab:<10}] AKA pct_flat={akapct}  comp_median={cmpct}  ratio={ratio_str}  ({tag} >= 2x)"
        print(line)
        summary_lines.append((lab, akapct, cmpct, ratio_str))
        if not pass_aka:
            failures.append(f"{lab}: ratio {ratio_str} below 2x threshold")

    # Capital Hilton + Willard at the loosened combo
    cap_loose = results["loosened"]["capital_hilton"]["pct_flat"] or 0
    will_loose = results["loosened"]["willard"]["pct_flat"] or 0
    cap_ok = cap_loose < 0.10
    will_ok = will_loose < 0.10
    print(
        f"  [loosened ] Capital Hilton pct_flat={round(cap_loose*100)}%  "
        f"Willard pct_flat={round(will_loose*100)}%  "
        f"({'OK' if cap_ok and will_ok else 'FAIL'} both < 10%)"
    )
    if not cap_ok:
        failures.append(f"loosened.capital_hilton pct_flat={round(cap_loose*100)}% >= 10%")
    if not will_ok:
        failures.append(f"loosened.willard pct_flat={round(will_loose*100)}% >= 10%")

    print()
    if failures:
        print("OVERALL: FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("OVERALL: PASS — plateau detection is robust to threshold choice.")
    print()
    print("Suggested methodology blurb:")
    print()
    template = (
        "Plateau detection is robust to threshold choice. "
        "At a tighter 3% day-over-day threshold, AKA's pct_flat = {tightened_aka} "
        "(comp median {tightened_comp}); at a looser 10% threshold, {loosened_aka} "
        "(comp median {loosened_comp}); at a longer 14-day minimum, {longer_aka} "
        "(comp median {longer_comp}). The AKA-flatter-than-comp-set ranking holds "
        "across all reasonable threshold choices."
    )
    sub = {lab: (akapct, cmpct) for lab, akapct, cmpct, _r in summary_lines}
    print(template.format(
        tightened_aka=sub["tightened"][0], tightened_comp=sub["tightened"][1],
        loosened_aka=sub["loosened"][0], loosened_comp=sub["loosened"][1],
        longer_aka=sub["longer"][0], longer_comp=sub["longer"][1],
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
