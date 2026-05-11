"""Phase A3 migration audit — pick_canonical_bar() vs legacy classify_bar().

Walks the migrated raw_rates.csv (v2 schema, rows still carry legacy is_bar
selections from the v1 classify_bar). Re-groups by (channel, property,
scraped_marketing_name, arrival_date, nights), runs pick_canonical_bar over
each room's reconstructed rate_plans, and reports AGREE / DISAGREE / NEW_NONE
counts per channel + sample disagreements.

Why disagreement is EXPECTED (and not necessarily a bug):
  v1 classify_bar picked the highest-priced refundable plan as BAR.
  v2 pick_canonical_bar picks the cheapest bare + non-refundable plan.
  These are different concepts — disagreement on most cells is the
  intentional policy change, not a regression.

Read the report as a documentation of the policy shift, not a pass/fail.
The Phase A4 smoke (with re-extraction at v2 prompt) is the real test.

Usage:
    py scraper_lib/audit_canonical_bar.py            # full report
    py scraper_lib/audit_canonical_bar.py --aka-only # AKA rows only
"""
from __future__ import annotations
import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from normalize import pick_canonical_bar, pick_canonical_flex  # noqa: E402

RAW_CSV = REPO_ROOT / "raw_rates.csv"


def _row_to_plan(r: dict) -> dict:
    """Reconstruct a plan-dict from a raw_rates.csv row, in the shape
    pick_canonical_bar expects."""
    rate = r.get("rate_per_night_usd")
    try:
        rate_num = float(rate) if rate not in (None, "") else None
    except (TypeError, ValueError):
        rate_num = None
    refundable_str = (r.get("refundable") or "").strip().lower()
    refundable: object
    if refundable_str == "true":
        refundable = True
    elif refundable_str == "false":
        refundable = False
    else:
        refundable = None
    is_genius_str = (r.get("is_genius_member_rate") or "").strip().lower()
    is_genius = (is_genius_str == "true")
    return {
        "rate_per_night_usd": rate_num,
        "rate_plan_label": r.get("rate_plan_label") or "",
        "refundable": refundable,
        "is_genius_member_rate": is_genius,
        "bundle_inclusions": r.get("bundle_inclusions") or "",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aka-only", action="store_true",
                    help="restrict audit to property_id=aka_white_house")
    ap.add_argument("--samples", type=int, default=10,
                    help="how many disagreement samples to print (default 10)")
    args = ap.parse_args()

    if not RAW_CSV.exists():
        print(f"NO raw_rates.csv at {RAW_CSV}")
        return 2

    with open(RAW_CSV, encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        all_rows = list(rdr)

    rows = [r for r in all_rows
            if (not args.aka_only or r.get("property_id") == "aka_white_house")]
    print(f"Audit input: {len(rows)} rows ({'AKA only' if args.aka_only else 'all properties'})")

    # Group by (property, channel, arrival, nights, marketing_name) — that's the
    # "room" granularity at which classify_bar / pick_canonical_bar both operate.
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        key = (r.get("property_id"), r.get("channel"), r.get("arrival_date"),
               r.get("nights"), r.get("scraped_marketing_name"))
        groups[key].append(r)

    print(f"Distinct rooms: {len(groups)}")

    # Per-channel counters
    channels = ["direct", "booking", "hotels_com", "expedia"]
    counters: dict[str, dict[str, int]] = {
        ch: {"total": 0, "agree": 0, "disagree": 0, "new_none_legacy_pick": 0,
             "no_legacy_bar": 0, "both_none": 0} for ch in channels
    }
    samples: list[dict] = []

    for key, room_rows in groups.items():
        prop, ch, arr, nights, mkt = key
        ch_key = ch if ch in counters else None
        if ch_key is None:
            continue
        c = counters[ch_key]
        c["total"] += 1

        # Legacy pick: row(s) with is_bar=True
        legacy_bar_rows = [r for r in room_rows if (r.get("is_bar") or "").strip().lower() == "true"]
        if not legacy_bar_rows:
            c["no_legacy_bar"] += 1
            # Run new picker anyway to see if v2 would have found something
            plans = [_row_to_plan(r) for r in room_rows]
            new_pick = pick_canonical_bar(plans)
            if new_pick is None:
                c["both_none"] += 1
            continue

        legacy_bar = legacy_bar_rows[0]  # in practice only one is_bar=True per room
        plans = [_row_to_plan(r) for r in room_rows]
        new_pick = pick_canonical_bar(plans)

        if new_pick is None:
            c["new_none_legacy_pick"] += 1
            if len(samples) < args.samples:
                samples.append({
                    "key": key, "kind": "NEW_NONE",
                    "legacy_label": legacy_bar.get("rate_plan_label"),
                    "legacy_rate": legacy_bar.get("rate_per_night_usd"),
                    "legacy_refundable": legacy_bar.get("refundable"),
                    "n_plans": len(room_rows),
                })
            continue

        new_idx, _ = new_pick
        new_row = room_rows[new_idx]
        legacy_label = (legacy_bar.get("rate_plan_label") or "").strip()
        legacy_rate = (legacy_bar.get("rate_per_night_usd") or "").strip()
        new_label = (new_row.get("rate_plan_label") or "").strip()
        new_rate = (new_row.get("rate_per_night_usd") or "").strip()

        if legacy_label == new_label and legacy_rate == new_rate:
            c["agree"] += 1
        else:
            c["disagree"] += 1
            if len(samples) < args.samples:
                samples.append({
                    "key": key, "kind": "DISAGREE",
                    "legacy": f"{legacy_label} @ ${legacy_rate}",
                    "new": f"{new_label} @ ${new_rate}",
                    "n_plans": len(room_rows),
                })

    # ---- Report ----
    print("\n" + "=" * 70)
    print(f"{'Channel':<14}{'rooms':>8}{'agree':>8}{'disagree':>10}{'new=none':>10}{'no legacy':>11}")
    print("-" * 70)
    grand = {"total": 0, "agree": 0, "disagree": 0, "new_none_legacy_pick": 0, "no_legacy_bar": 0}
    for ch in channels:
        c = counters[ch]
        if c["total"] == 0:
            continue
        for k in grand:
            grand[k] += c[k]
        print(f"{ch:<14}{c['total']:>8}{c['agree']:>8}{c['disagree']:>10}"
              f"{c['new_none_legacy_pick']:>10}{c['no_legacy_bar']:>11}")
    print("-" * 70)
    print(f"{'TOTAL':<14}{grand['total']:>8}{grand['agree']:>8}{grand['disagree']:>10}"
          f"{grand['new_none_legacy_pick']:>10}{grand['no_legacy_bar']:>11}")

    if grand["total"]:
        comparable = grand["total"] - grand["no_legacy_bar"]
        if comparable:
            disagree_rate = (grand["disagree"] + grand["new_none_legacy_pick"]) / comparable
            print(f"\nDisagreement rate (vs legacy is_bar pick): {disagree_rate*100:.1f}%")
            print(f"  (comparable = {comparable} rooms; disagree+new_none = "
                  f"{grand['disagree'] + grand['new_none_legacy_pick']})")

    if samples:
        print(f"\nFirst {len(samples)} disagreement samples:")
        for s in samples:
            prop, ch, arr, nights, mkt = s["key"]
            print(f"  [{s['kind']}] {ch}/{prop}/{arr}/L{nights} '{mkt[:40]}' (n_plans={s['n_plans']})")
            if s["kind"] == "DISAGREE":
                print(f"      legacy: {s['legacy']}")
                print(f"      v2:     {s['new']}")
            else:
                print(f"      legacy: {s['legacy_label']} @ ${s['legacy_rate']} "
                      f"(refundable={s['legacy_refundable']})")
                print(f"      v2:     <no bare non-ref bar candidate>")

    return 0


if __name__ == "__main__":
    sys.exit(main())
