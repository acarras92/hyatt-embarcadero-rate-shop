"""One-time migration: raw_rates.csv v1 → v2.

What it does
------------
1. Backs up the existing raw_rates.csv to raw_rates_pre_v2_<date>.csv.
2. Re-writes raw_rates.csv with the v2 header (legacy 28 cols + 9 new cols).
3. Existing rows get blank values for all 9 new columns.

What it does NOT do
-------------------
- Does NOT attempt to back-fill room_type_canonical for legacy rows. Existing
  scraped_marketing_name strings include known hallucinations ("Two Bedroom
  Duplex Penthouse" etc.); back-filling canonical IDs would re-admit them
  without surfacing the FAIL_UNKNOWN_ROOM_TYPE signal. Future scrapes will
  populate canonical fields cleanly via the v2 extractor + validator.
- Does NOT recompute is_bar. The v1 classify_bar() decisions stay intact for
  legacy rows; Phase A3 introduces pick_canonical_bar() and runs a separate
  audit of disagreements.

Idempotency
-----------
Detects v2 header on the existing file and exits without re-writing if
already migrated.

Usage
-----
    py scripts/migrate_v1_to_v2.py                  # run from repo root
    py scripts/migrate_v1_to_v2.py --dry-run        # show row counts only
"""
from __future__ import annotations
import argparse
import csv
import datetime
import shutil
import sys
from pathlib import Path

# Tolerate being run from repo root or scraper_lib/
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE if (HERE / "raw_rates.csv").exists() else HERE.parent
sys.path.insert(0, str(REPO_ROOT / "scraper_lib"))

from schema import LEGACY_COLUMNS, NEW_COLUMNS, RAW_HEADER  # noqa: E402

RAW_CSV = REPO_ROOT / "raw_rates.csv"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report row counts without writing")
    args = ap.parse_args()

    if not RAW_CSV.exists():
        print(f"NO raw_rates.csv at {RAW_CSV}; nothing to migrate")
        return 0

    with open(RAW_CSV, encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        existing_header = rdr.fieldnames or []
        rows = list(rdr)

    # Idempotency: if all NEW_COLUMNS already in header, no-op
    if all(col in existing_header for col in NEW_COLUMNS):
        print(f"Already at v2 — {len(rows)} rows, header has all {len(NEW_COLUMNS)} new columns. No-op.")
        return 0

    # Validate the source IS v1 (LEGACY_COLUMNS subset of existing)
    missing_legacy = [c for c in LEGACY_COLUMNS if c not in existing_header]
    if missing_legacy:
        print(f"REFUSING migration — existing header is missing legacy columns: {missing_legacy}")
        print(f"Existing header: {existing_header}")
        return 2

    # Stats
    aka_rows = sum(1 for r in rows if r.get("property_id") == "aka_white_house")
    print(f"Source: {RAW_CSV} — {len(rows)} rows ({aka_rows} AKA, {len(rows)-aka_rows} comp)")
    print(f"Existing header has {len(existing_header)} cols; v2 expects {len(RAW_HEADER)} ({len(NEW_COLUMNS)} new).")
    print(f"New cols (will be blank for all legacy rows): {NEW_COLUMNS}")

    if args.dry_run:
        print("DRY RUN — exiting without write.")
        return 0

    # Backup
    today = datetime.date.today().isoformat()
    backup_path = RAW_CSV.parent / f"raw_rates_pre_v2_{today}.csv"
    if backup_path.exists():
        print(f"Backup {backup_path} already exists — refusing to overwrite. Move/delete it then retry.")
        return 3
    shutil.copy2(RAW_CSV, backup_path)
    print(f"Backed up to {backup_path}")

    # Re-write with v2 header. csv.DictWriter renders missing keys as empty
    # strings (since extrasaction='ignore' skips extras and defaults missing).
    # Use empty string explicitly for clarity in the CSV.
    with open(RAW_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RAW_HEADER, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            for col in NEW_COLUMNS:
                r.setdefault(col, "")
            w.writerow(r)

    print(f"Wrote {len(rows)} rows to {RAW_CSV} with v2 header ({len(RAW_HEADER)} cols).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
