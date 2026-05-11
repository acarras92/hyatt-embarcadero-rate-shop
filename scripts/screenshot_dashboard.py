"""Headless dashboard screenshot helper.

Usage:
    python scripts/screenshot_dashboard.py verification/phase1_reductive_sweep.png

Renders index.html in Chromium, waits for window.__DASHBOARD_BOOTED__,
captures full-page PNG, and reports any pageerror / console.error messages.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: screenshot_dashboard.py <out_png>")
        return 2
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    index_path = Path("index.html").resolve()
    index_url = index_path.as_uri()
    issues: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1480, "height": 1100},
                                device_scale_factor=2)
        page.on("pageerror", lambda e: issues.append(f"[pageerror] {e}"))
        page.on(
            "console",
            lambda m: issues.append(f"[console.{m.type}] {m.text}")
            if m.type in ("error", "warning")
            else None,
        )
        page.goto(index_url, wait_until="networkidle")
        page.wait_for_function("window.__DASHBOARD_BOOTED__ === true", timeout=20000)
        time.sleep(1.0)
        page.screenshot(path=str(out), full_page=True)
        browser.close()
    print(f"Saved screenshot to {out} ({out.stat().st_size} bytes)")
    if issues:
        print("--- console issues ---")
        for e in issues[:50]:
            print(" ", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
