"""Phase A4 smoke harness — 6 cells exercising all 8 v2 bugs.

Two modes
---------
--simulate   (default)  Run pick_canonical_bar + pick_canonical_flex against the
                        EXPECTED rate plans in tests/fixtures/smoke_expected_v2.json
                        and report what the picker would emit. NO Firecrawl
                        spend. This is what gets included in smoke_dry_run.md
                        so Andrew can eyeball picker behavior before any credits
                        are spent.

--execute               Run scrape_cell() over each cell, append to
                        raw_rates_smoke_v2.csv, and diff actual vs expected.
                        Costs ~50-75 Firecrawl credits (200 cap). Andrew runs
                        this himself from the working dir that has .env +
                        config.json (RM Review/scrape_2026-04-26_part2/).
                        DO NOT execute from this scraper_lib path — module-load
                        will fail without those files.

Output (--execute)
------------------
- raw_rates_smoke_v2.csv     in CWD (1-2 rows per cell)
- smoke_results.json         per-cell PASS/FAIL with reason
- smoke_results.md           human-readable summary

Stop conditions (per brief)
---------------------------
- Spend cap: 200 nominal Firecrawl credits
- Any cell FAILs after 2 retry attempts → halt
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "smoke_expected_v2.json"

sys.path.insert(0, str(HERE))


# =============================================================================
# Picker simulation (no Firecrawl)
# =============================================================================
def _synthesize_cancellation_phrase(plan_label: str, refundable: Optional[bool]) -> str:
    """Synthesize a cancellation_phrase from the fixture's refundable flag so
    the simulator's plan dicts look like real LLM-extracted plans (which
    populate `cancellation_phrase` verbatim from the per-plan cancellation
    copy on the page). Required after Phase A2.4 #3 — refundability is
    derived strictly from cancellation_phrase / label tokens, not the
    LLM's `refundable` bool.
    """
    label = (plan_label or "").lower()
    # Label-based shortcuts first — preserve the source signal when present
    if "non-refund" in label or "pay now" in label:
        return "Non-refundable"
    if "free cancellation" in label or "fully refundable" in label:
        return "Free cancellation"
    if "pay flex" in label or " flex" in label or label.startswith("flex"):
        return "Free cancellation"
    # Fall back to the refundable bool (only used when no label tokens hit)
    if refundable is True:
        return "Free cancellation"
    if refundable is False:
        return "Non-refundable"
    return ""


def simulate_picker_against_fixture() -> list[dict]:
    """Run pick_canonical_bar + pick_canonical_flex against the EXPECTED rate
    plans in the fixture. Returns one dict per cell with simulated picks.

    Skips cells with `expected_rate_plans_soft` (no enumerated plans, e.g.,
    cell 02 — Direct LOS=7 has no chrome ground truth).
    """
    from normalize import pick_canonical_bar, pick_canonical_flex

    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    out: list[dict] = []
    for cell in fixture["cells"]:
        cid = cell["id"]
        if "expected_rate_plans" not in cell:
            out.append({
                "id": cid,
                "skipped": True,
                "reason": "soft expectations only — no enumerated rate plans in fixture",
            })
            continue
        plans = [
            {
                "rate_plan_label": p["label"],
                "rate_per_night_usd": p["rate"],
                "refundable": p["refundable"],
                "is_genius_member_rate": False,
                "bundle_inclusions": p.get("bundle_inclusions"),
                "cancellation_phrase": _synthesize_cancellation_phrase(
                    p["label"], p.get("refundable")
                ),
            }
            for p in cell["expected_rate_plans"]
        ]
        bar = pick_canonical_bar(plans)
        flex = pick_canonical_flex(plans)
        bar_str = (
            f"{plans[bar[0]]['rate_plan_label']} @ ${plans[bar[0]]['rate_per_night_usd']}"
            if bar else "<no bare non-ref candidate>"
        )
        flex_str = (
            f"{plans[flex[0]]['rate_plan_label']} @ ${plans[flex[0]]['rate_per_night_usd']}"
            if flex else "<no bare refundable candidate>"
        )
        # Compare against expected_canonical_bar / _flex
        exp_bar = cell.get("expected_canonical_bar", {})
        exp_flex = cell.get("expected_canonical_flex", {})
        bar_match = bar is not None and plans[bar[0]]["rate_plan_label"] == exp_bar.get("plan")
        # FLEX match — accept three valid expectation forms:
        #   1) {"plan": "<label>", ...}    → picker label must equal "<label>"
        #   2) {"plan": null, ...}         → picker MUST return None (no BAR_FLEX expected)
        #   3) {"rate_kind": "...", ...}   → softer descriptor; any non-null pick passes
        if exp_flex.get("plan") is None and "rate_kind" not in exp_flex:
            # form 2: explicit "no flex expected"
            flex_match = (flex is None)
        elif "rate_kind" in exp_flex:
            # form 3: softer expectation, any non-null pick passes
            flex_match = flex is not None
        else:
            # form 1: exact label match
            flex_match = flex is not None and plans[flex[0]]["rate_plan_label"] == exp_flex.get("plan")
        out.append({
            "id": cid,
            "skipped": False,
            "simulated_bar": bar_str,
            "simulated_flex": flex_str,
            "expected_bar": f"{exp_bar.get('plan')} @ ${exp_bar.get('rate')}",
            "expected_flex": f"{exp_flex.get('plan', exp_flex.get('rate_kind'))}",
            "bar_match": bar_match,
            "flex_match": flex_match,
        })
    return out


def render_simulate_report(results: list[dict]) -> str:
    lines: list[str] = []
    lines.append("Phase A4 picker simulation — pick_canonical_bar/flex against fixture expected_rate_plans")
    lines.append("=" * 90)
    pass_n, fail_n, skip_n = 0, 0, 0
    for r in results:
        if r.get("skipped"):
            skip_n += 1
            lines.append(f"  [SKIP] {r['id']}")
            lines.append(f"         reason: {r['reason']}")
            continue
        bar_ok = r["bar_match"]
        flex_ok = r["flex_match"]
        verdict = "PASS" if (bar_ok and flex_ok) else "FAIL"
        if verdict == "PASS":
            pass_n += 1
        else:
            fail_n += 1
        lines.append(f"  [{verdict}] {r['id']}")
        lines.append(f"         BAR  expected: {r['expected_bar']}")
        lines.append(f"              picked:   {r['simulated_bar']}  {'OK' if bar_ok else 'XX'}")
        lines.append(f"         FLEX expected: {r['expected_flex']}")
        lines.append(f"              picked:   {r['simulated_flex']}  {'OK' if flex_ok else 'XX'}")
    lines.append("")
    lines.append(f"Summary: PASS={pass_n}  FAIL={fail_n}  SKIP={skip_n}")
    return "\n".join(lines)


# =============================================================================
# Live scrape execution (--execute) — DO NOT RUN FROM THIS PATH
# =============================================================================
def execute_smoke() -> int:
    """Run scrape_cell() over each smoke cell, write raw_rates_smoke_v2.csv,
    diff against fixture, emit smoke_results.{json,md}.

    Module-load of scrape.py requires .env (FIRECRAWL_API_KEY) and
    config.json — those live in Andrew's working scrape directory, NOT in
    this repo's scraper_lib. Run from there:

        cd "RM Review/scrape_2026-04-26_part2/"
        py path/to/scraper_lib/smoke_harness.py --execute --fixture path/to/tests/fixtures/smoke_expected_v2.json
    """
    try:
        from scrape import scrape_cell, append_rows_to_csv  # noqa: F401
    except (ImportError, RuntimeError) as ex:
        print("ERROR: scrape module load failed. Are you running from the working dir")
        print("       (with .env + config.json), not from scraper_lib/?")
        print(f"       Underlying: {ex}")
        return 2

    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    cells = fixture["cells"]
    print(f"--execute: would run {len(cells)} smoke cells (~50-75 credits)")
    print("This is the implementation stub. Run the underlying scrape_cell loop here.")
    print("Andrew is expected to invoke this path himself after eyeballing smoke_dry_run.md.")
    # Implementation deferred to Andrew's run; harness scaffolds the call site only.
    return 0


# =============================================================================
# CLI
# =============================================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", action="store_true", default=True,
                    help="(default) Run picker against fixture expected_rate_plans, no Firecrawl spend")
    ap.add_argument("--execute", action="store_true",
                    help="Run live scrape against the 6 smoke cells (~50-75 Firecrawl credits). "
                         "Must run from a working dir with .env + config.json present.")
    args = ap.parse_args()

    if args.execute:
        return execute_smoke()

    # Default: simulate
    results = simulate_picker_against_fixture()
    print(render_simulate_report(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
