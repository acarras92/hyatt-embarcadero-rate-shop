"""Phase 2 high-bucket comp-availability audit + sensitivity.

For each of the 8 high-demand dates (market_demand_frac >= 0.80) the helper
included in its bucket, list every comp's availability_status, identify which
ones were excluded from the comp_median and why (sold_out / not_loaded /
los_restricted / blank / room_na / no_flex / third_party_only / one_guest_only),
and re-compute delta_high_usd under two cuts:
  (a) as-is (current median over whatever comps were available)
  (b) restricted to dates with comp_n >= 4

Reports the share of full-panel vs reduced-panel high-bucket dates, the delta
shift between cuts, and the sold_out vs not_loaded breakdown of excluded cells.

Run:
    python scripts/phase2_high_bucket_audit.py
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
SUBJECT = "aka_white_house"
COMP_SET = [p for p in la.LH_COMP_SET if p != SUBJECT]


def per_comp_status(lh_any: pd.DataFrame, arrival_date: str) -> dict:
    """Return {property: {status, rate_usd}} for every comp on a date.

    `lh_any` is pre-filtered to source='brandcom' AND room_tier='any' so we see
    one row per (property, date) at most. If a property has no row at all the
    return value records status='absent_row' to distinguish that from sold_out.
    """
    out: dict[str, dict] = {}
    for prop in COMP_SET:
        sub = lh_any[(lh_any["property"] == prop) & (lh_any["arrival_date"] == arrival_date)]
        if sub.empty:
            out[prop] = {"status": "absent_row", "rate_usd": None}
            continue
        # If multiple rows somehow exist, prefer the available one for inclusion
        # accounting; otherwise pick the first row's status verbatim.
        avail = sub[sub["availability_status"] == "available"]
        if not avail.empty:
            r = avail.iloc[0]
            rate = float(r["rate_usd"]) if pd.notna(r["rate_usd"]) else None
            out[prop] = {"status": "available", "rate_usd": rate}
        else:
            r = sub.iloc[0]
            out[prop] = {"status": str(r["availability_status"]),
                         "rate_usd": (float(r["rate_usd"]) if pd.notna(r["rate_usd"]) else None)}
    return out


def main() -> int:
    lh = pd.read_csv(LH_CSV)
    result = la.compute_subject_vs_comp_median_spread(lh, subject=SUBJECT, comp_set=COMP_SET)
    rows = result["rows"]

    # All Brand.com any-tier rows for the comp set — used to read availability
    # status verbatim per comp per date.
    lh_any = lh[(lh["source"] == "brandcom") & (lh["room_tier"] == "any")].copy()

    high_rows = [r for r in rows if r["bucket"] == "high"]
    high_rows.sort(key=lambda r: r["arrival_date"])

    print("=" * 96)
    print(f"  HIGH-BUCKET COMP-AVAILABILITY AUDIT  (n={len(high_rows)} high-demand dates)")
    print("=" * 96)

    excluded_status_tally: dict[str, int] = {}
    panel_size_tally: dict[int, int] = {}

    for r in high_rows:
        d = r["arrival_date"]
        statuses = per_comp_status(lh_any, d)
        included = [(p, s["rate_usd"]) for p, s in statuses.items() if s["status"] == "available"]
        excluded = [(p, s["status"]) for p, s in statuses.items() if s["status"] != "available"]
        comp_n = len(included)
        panel_size_tally[comp_n] = panel_size_tally.get(comp_n, 0) + 1
        for _p, st in excluded:
            excluded_status_tally[st] = excluded_status_tally.get(st, 0) + 1

        print(f"\n  arrival_date={d}  market_demand_frac={r['market_demand_frac']:.3f}  "
              f"aka_rate=${r['aka_rate_usd']:.0f}  "
              f"comp_n={comp_n}  comp_median=${r['comp_median_rate_usd']:.0f}  "
              f"daily_delta=${r['daily_delta_usd']:.0f}")
        for p, rate in sorted(included):
            print(f"      INCLUDED  {p:<22}  status=available     rate=${rate:.0f}")
        for p, st in sorted(excluded):
            print(f"      EXCLUDED  {p:<22}  status={st}")

    # Sensitivity cut: restrict to dates with comp_n >= 4.
    high_full = []
    for r in high_rows:
        statuses = per_comp_status(lh_any, r["arrival_date"])
        n_avail = sum(1 for s in statuses.values() if s["status"] == "available")
        if n_avail >= 4:
            high_full.append(r)

    deltas_asis = [r["daily_delta_usd"] for r in high_rows if r["daily_delta_usd"] is not None]
    deltas_full = [r["daily_delta_usd"] for r in high_full if r["daily_delta_usd"] is not None]
    delta_high_asis = float(statistics.median(deltas_asis)) if deltas_asis else None
    delta_high_full = float(statistics.median(deltas_full)) if deltas_full else None
    delta_typical = result["headlines"]["delta_typical_usd"]

    spread_asis = (delta_high_asis - delta_typical) if (delta_high_asis is not None and delta_typical is not None) else None
    spread_full = (delta_high_full - delta_typical) if (delta_high_full is not None and delta_typical is not None) else None

    n_high = len(high_rows)
    full_panel = panel_size_tally.get(5, 0)
    reduced = sum(v for k, v in panel_size_tally.items() if k < 5)

    print()
    print("=" * 96)
    print("  PANEL-SIZE DISTRIBUTION (high-bucket only)")
    print("=" * 96)
    for n_comps in sorted(panel_size_tally.keys()):
        print(f"  comp_n={n_comps}  count={panel_size_tally[n_comps]}  share={panel_size_tally[n_comps]/n_high:.1%}")
    print(f"  Full-panel (comp_n=5):       {full_panel}/{n_high} = {full_panel/n_high:.1%}")
    print(f"  Reduced (comp_n in {{3,4}}): {reduced}/{n_high} = {reduced/n_high:.1%}")

    print()
    print("=" * 96)
    print("  EXCLUDED-COMP STATUS TALLY  (across all 8 high-bucket dates × 5 comps each)")
    print("=" * 96)
    sold_out_count = excluded_status_tally.get("sold_out", 0)
    not_loaded_count = excluded_status_tally.get("not_loaded", 0)
    los_restricted_count = excluded_status_tally.get("los_restricted", 0)
    other_total = sum(v for k, v in excluded_status_tally.items()
                      if k not in ("sold_out", "not_loaded", "los_restricted"))
    for st, cnt in sorted(excluded_status_tally.items(), key=lambda kv: -kv[1]):
        tag = ""
        if st == "sold_out":
            tag = "  (legitimate exclusion — comp genuinely unavailable)"
        elif st == "not_loaded":
            tag = "  (data gap — Lighthouse hasn't pulled this comp on this date)"
        elif st == "los_restricted":
            tag = "  (LOS gate — 1-night BAR not bookable; comp may have a real but hidden any-tier rate)"
        print(f"  {st:<22}  count={cnt}{tag}")
    total_excluded = sum(excluded_status_tally.values())
    print(f"  TOTAL excluded comp-cells: {total_excluded}  "
          f"(sold_out={sold_out_count}, not_loaded={not_loaded_count}, "
          f"los_restricted={los_restricted_count}, other={other_total})")

    print()
    print("=" * 96)
    print("  SENSITIVITY: delta_high_usd under two cuts")
    print("=" * 96)
    print(f"  delta_typical_usd (normal bucket median, unchanged): ${delta_typical:.0f}")
    print()
    print(f"  AS-IS                  delta_high=${delta_high_asis:.0f}   "
          f"n={len(deltas_asis)}   "
          f"spread_movement=${spread_asis:.0f}")
    if delta_high_full is None:
        print("  comp_n>=4 RESTRICTED   delta_high=  N/A   "
              f"n={len(deltas_full)}  (no high-bucket dates with comp_n>=4)")
    else:
        print(f"  comp_n>=4 RESTRICTED   delta_high=${delta_high_full:.0f}   "
              f"n={len(deltas_full)}   "
              f"spread_movement=${spread_full:.0f}")

    if delta_high_full is not None:
        shift = delta_high_full - delta_high_asis
        print(f"\n  delta_high shift between cuts: ${shift:+.0f}  (threshold: abs(shift) < $25 = robust)")
        if abs(shift) < 25:
            print("  ROBUST -- verdict (ANTI_YIELDS) holds across panel-size cut.")
        else:
            print(f"  COMPOSITIONAL RISK -- high-bucket sample shifts by ${abs(shift):.0f} when "
                  "thin-panel dates are excluded.  Reconsider before wiring to the dashboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
