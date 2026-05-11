"""
Re-deploy the dashboard after a fresh scrape.

Usage:
  python redeploy.py                                         # uses ./raw_rates.csv
  python redeploy.py --csv "../RM Review/scrape_YYYY-MM-DD/raw_rates.csv"

What it does:
  1. Copies the new raw_rates.csv into this repo (if --csv supplied)
  2. Runs build_dashboard.py to regenerate data.js
  3. git add / commit / push to origin main
  4. Prints the live URL

Assumes:
  - You're authenticated with `gh` (gh auth status)
  - The repo already exists at github.com/acarras92/hyatt-embarcadero-rate-shop
  - GitHub Pages is enabled on main branch / root
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
LIVE_URL = "https://acarras92.github.io/hyatt-embarcadero-rate-shop/"


def run(cmd, **kw):
    print(f"[redeploy] $ {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    return subprocess.run(cmd, cwd=REPO_ROOT, check=True, shell=isinstance(cmd, str), **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="Path to a fresh raw_rates.csv from a new scrape")
    ap.add_argument("--message", default=None, help="Optional commit message")
    ap.add_argument("--no-push", action="store_true", help="Skip git push")
    args = ap.parse_args()

    if args.csv:
        src = Path(args.csv).expanduser().resolve()
        if not src.exists():
            sys.exit(f"ERROR: {src} not found")
        dst = REPO_ROOT / "raw_rates.csv"
        print(f"[redeploy] Copying {src} -> {dst}")
        shutil.copy2(src, dst)

    # Regenerate data.js — auto-detect mode based on raw_rates.csv presence.
    # F35: zero-config dispatch so this script works both pre-scrape (no csv yet
    # → --lighthouse-only) and post-scrape (csv present → full mode).
    raw_csv = REPO_ROOT / "raw_rates.csv"
    build_cmd = [sys.executable, str(REPO_ROOT / "build_dashboard.py")]
    if raw_csv.exists():
        print("[redeploy] raw_rates.csv present — full mode.")
    else:
        build_cmd.append("--lighthouse-only")
        print("[redeploy] raw_rates.csv absent — --lighthouse-only mode.")
    run(build_cmd)

    # Stage and commit — only include raw_rates.csv when it exists.
    files_to_stage = ["data.js"]
    if raw_csv.exists():
        files_to_stage.insert(0, "raw_rates.csv")
    run(["git", "add"] + files_to_stage)

    # Check if anything actually changed
    status = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True)
    if not status.stdout.strip():
        print("[redeploy] No changes to commit. Already up to date.")
        print(f"[redeploy] Live: {LIVE_URL}")
        return

    msg = args.message or f"Refresh dashboard data — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    run(["git", "commit", "-m", msg])

    if not args.no_push:
        run(["git", "push", "origin", "main"])
        print(f"[redeploy] Pushed. Live URL: {LIVE_URL}")
        print("[redeploy] GitHub Pages typically rebuilds in 30-90 seconds.")
    else:
        print("[redeploy] Skipped push (use git push origin main when ready).")


if __name__ == "__main__":
    main()
