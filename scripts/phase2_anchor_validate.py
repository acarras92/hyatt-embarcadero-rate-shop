"""Phase 2 anchor-validation harness for compute_subject_vs_comp_median_spread.

Runs the helper on the live Lighthouse pull, prints the four headline numbers
+ verdict, and hand-computes aka_rate / comp_median / daily_delta for one date
in each bucket (normal / shoulder / high). Asserts the helper matches hand
computation within $0.50.

Run:
    python scripts/phase2_anchor_validate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import analytics_lighthouse as la  # noqa: E402

LH_CSV = ROOT.parent / "AKA White House" / "Lighthouse" / "lighthouse_rates.csv"
SUBJECT = "aka_white_house"
COMP_SET = [p for p in la.LH_COMP_SET if p != SUBJECT]


def hand_compute(lh: pd.DataFrame, arrival_date: str) -> dict:
    """Mirror the helper filter independently and return the row-level inputs."""
    sub = lh[
        (lh["source"] == "brandcom")
        & (lh["room_tier"] == "any")
        & (lh["property"] == SUBJECT)
        & (lh["arrival_date"] == arrival_date)
        & (lh["availability_status"] == "available")
        & lh["rate_usd"].notna()
    ]
    aka_rate = float(sub["rate_usd"].iloc[0]) if len(sub) else None
    market_demand = float(sub["market_demand_frac"].iloc[0]) if len(sub) else None

    comps = lh[
        (lh["source"] == "brandcom")
        & (lh["room_tier"] == "any")
        & (lh["property"].isin(COMP_SET))
        & (lh["arrival_date"] == arrival_date)
        & (lh["availability_status"] == "available")
        & lh["rate_usd"].notna()
    ][["property", "rate_usd"]]
    comp_rates = {p: float(r) for p, r in zip(comps["property"], comps["rate_usd"])}
    comp_rates_sorted = dict(sorted(comp_rates.items()))
    comp_median = float(pd.Series(list(comp_rates.values())).median()) if comp_rates else None
    daily_delta = (aka_rate - comp_median) if (aka_rate is not None and comp_median is not None) else None
    return {
        "arrival_date": arrival_date,
        "aka_rate_usd": aka_rate,
        "comp_rates_by_property": comp_rates_sorted,
        "n_comps_available": len(comp_rates),
        "comp_median_rate_usd": comp_median,
        "daily_delta_usd": daily_delta,
        "market_demand_frac": market_demand,
    }


def main() -> int:
    lh = pd.read_csv(LH_CSV)
    result = la.compute_subject_vs_comp_median_spread(lh, subject=SUBJECT, comp_set=COMP_SET)
    h = result["headlines"]
    v = result["verdict"]

    print("=" * 72)
    print("  Section 3 metric — compute_subject_vs_comp_median_spread")
    print("=" * 72)
    print(f"  delta_typical_usd:    {h['delta_typical_usd']}")
    print(f"  delta_high_usd:       {h['delta_high_usd']}")
    print(f"  spread_movement_usd:  {h['spread_movement_usd']}")
    print(f"  bucket counts:        n_normal={h['n_normal']} n_shoulder={h['n_shoulder']} n_high={h['n_high']}")
    print(f"  verdict:              {v['classification']} — {v['label']}")
    print(f"  rationale:            {v['rationale']}")
    print()

    # Pick the first row from each bucket where comp_median is non-null.
    buckets = {"normal": None, "shoulder": None, "high": None}
    for r in result["rows"]:
        if buckets[r["bucket"]] is None and r["daily_delta_usd"] is not None:
            buckets[r["bucket"]] = r

    print("=" * 72)
    print("  Anchor validation — hand-computed inputs vs helper output (tolerance $0.50)")
    print("=" * 72)
    failures: list[str] = []
    for bucket_name in ("normal", "shoulder", "high"):
        helper_row = buckets[bucket_name]
        if helper_row is None:
            print(f"  [{bucket_name}] NO ROWS — bucket empty or all comps unavailable")
            continue
        d = helper_row["arrival_date"]
        hand = hand_compute(lh, d)
        print(f"\n  [{bucket_name}] arrival_date={d}  market_demand_frac={hand['market_demand_frac']:.3f}")
        print(f"    aka_rate (subject Brand.com any-tier available BAR): ${hand['aka_rate_usd']:.2f}")
        print(f"    comp rates by property (n={hand['n_comps_available']}):")
        for p, r in hand["comp_rates_by_property"].items():
            print(f"        {p:<22}  ${r:.2f}")
        print(f"    comp_median = median of those rates: ${hand['comp_median_rate_usd']:.2f}")
        print(f"    daily_delta = aka - comp_median:      ${hand['daily_delta_usd']:.2f}")
        print(f"    helper_aka=${helper_row['aka_rate_usd']:.2f}  "
              f"helper_comp_median=${helper_row['comp_median_rate_usd']:.2f}  "
              f"helper_delta=${helper_row['daily_delta_usd']:.2f}")
        # Tolerance check
        tol = 0.50
        for k in ("aka_rate_usd", "comp_median_rate_usd", "daily_delta_usd"):
            diff = abs(helper_row[k] - hand[k])
            ok = diff <= tol
            tag = "OK" if ok else "FAIL"
            print(f"    diff[{k}] = ${diff:.4f}  ({tag} <= ${tol:.2f})")
            if not ok:
                failures.append(f"{bucket_name}.{k} diff=${diff:.4f}")

    print()
    if failures:
        print("ANCHOR VALIDATION FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ANCHOR VALIDATION PASSED for all 3 buckets within $0.50 tolerance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
