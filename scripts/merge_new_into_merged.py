"""Merge a fresh scrape's raw_rates.csv into the canonical raw_rates_merged.csv.

Usage:
  py scripts/merge_new_into_merged.py
  py scripts/merge_new_into_merged.py --new path/to/raw_rates.csv
  py scripts/merge_new_into_merged.py --merged path/to/raw_rates_merged.csv
  py scripts/merge_new_into_merged.py --dry-run

Dedup key (per spec): property_id + channel + arrival_date + nights + room_type_canonical.
Any old row whose key matches a new row's key is dropped; all new rows are appended.
This makes the operation idempotent: re-running a scrape replaces the prior
row-set for those cells rather than double-counting.

Schema handling:
  The new scrape CSV may contain extra columns the merged file does not
  know about (e.g. penthouse_variant added 2026-04-29). They are silently
  dropped to keep raw_rates_merged.csv schema-stable. Columns the merged
  file has but the new file lacks are left blank for new rows.

Backup:
  Before writing, the merged file is copied to raw_rates_merged.csv.bak.
  Disable with --no-backup.
"""
from __future__ import annotations

import argparse
import csv
import shutil
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NEW = REPO_ROOT / "scraper_lib" / "raw_rates.csv"
DEFAULT_MERGED = REPO_ROOT / "raw_rates_merged.csv"

DEDUP_KEYS = ("property_id", "channel", "arrival_date", "nights",
              "room_type_canonical")


def _key(row: dict) -> tuple:
    return tuple(row.get(k, "") or "" for k in DEDUP_KEYS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", type=Path, default=DEFAULT_NEW,
                    help="Path to a fresh scrape CSV to merge in")
    ap.add_argument("--merged", type=Path, default=DEFAULT_MERGED,
                    help="Path to the canonical merged CSV")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    if not args.new.exists():
        print(f"[ERROR] new CSV not found: {args.new}")
        return 1
    if not args.merged.exists():
        print(f"[ERROR] merged CSV not found: {args.merged}")
        return 1

    with open(args.merged, encoding="utf-8") as f:
        rd = csv.DictReader(f)
        merged_fields = list(rd.fieldnames)
        merged_rows = list(rd)
    with open(args.new, encoding="utf-8") as f:
        rd = csv.DictReader(f)
        new_fields = list(rd.fieldnames)
        new_rows = list(rd)

    print(f"  merged CSV: {len(merged_rows)} rows, {len(merged_fields)} cols")
    print(f"  new CSV   : {len(new_rows)} rows, {len(new_fields)} cols")

    extra_in_new = sorted(set(new_fields) - set(merged_fields))
    missing_in_new = sorted(set(merged_fields) - set(new_fields))
    if extra_in_new:
        print(f"  cols in new not in merged (will be dropped): {extra_in_new}")
    if missing_in_new:
        print(f"  cols in merged not in new (will be blank): {missing_in_new}")

    new_keys = {_key(r) for r in new_rows}
    print(f"  new dedup keys: {len(new_keys)}")
    new_cells = {(r['property_id'], r['channel'], r['arrival_date'], r['nights'])
                 for r in new_rows}
    print(f"  new cells (prop/ch/date/LOS): {len(new_cells)}")

    kept_old = [r for r in merged_rows if _key(r) not in new_keys]
    dropped = len(merged_rows) - len(kept_old)
    print(f"  old rows dropped (key collision): {dropped}")

    # Project new rows onto merged schema (drop unknown cols, blank missing)
    projected_new: list[dict] = []
    for r in new_rows:
        projected_new.append({k: r.get(k, "") for k in merged_fields})

    final = kept_old + projected_new
    print(f"  final row count: {len(final)} "
          f"(delta: {len(final) - len(merged_rows):+d})")

    if args.dry_run:
        print("  [dry-run] no write")
        return 0

    if not args.no_backup:
        bak = args.merged.with_suffix(args.merged.suffix + ".bak")
        shutil.copy2(args.merged, bak)
        print(f"  backup -> {bak.name}")

    with open(args.merged, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=merged_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(final)

    print(f"  wrote {args.merged} at "
          f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
