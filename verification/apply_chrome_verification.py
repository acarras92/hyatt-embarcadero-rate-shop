"""Phase B3 — ingest chrome_verification_report.json from the Cowork harness.

Two outputs:
  1. tests/fixtures/chrome_truth.csv     — regression fixture; locked-in PASS
                                           cells. Future scraper runs are
                                           checked against this in CI.
  2. chrome_bias_summary.md              — aggregate per (channel, field) bias
                                           report. Run-level verdict
                                           (GREEN/YELLOW/RED). When YELLOW or
                                           RED, formatted as a Phase A1-style
                                           bug list so the next claude-code
                                           session can use it directly as
                                           prompt input.

Usage:
    py verification/apply_chrome_verification.py path/to/chrome_verification_report.json
    py verification/apply_chrome_verification.py path/to/report.json --append-truth
    py verification/apply_chrome_verification.py path/to/report.json --bias-only

Exit codes:
  0   GREEN run (or --bias-only)
  1   YELLOW run
  2   RED run
  3   bad input file
"""
from __future__ import annotations
import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
CHROME_TRUTH_PATH = REPO_ROOT / "tests" / "fixtures" / "chrome_truth.csv"
SPEC_PATH = HERE / "chrome_verify_sample_spec.json"
BIAS_SUMMARY_PATH = HERE / "chrome_bias_summary.md"
RUN_HISTORY_PATH = HERE / "run_history.jsonl"
README_PATH = HERE / "README.md"
TRACKER_BEGIN = "<!-- VERIFICATION_TRACKER_BEGIN -->"
TRACKER_END = "<!-- VERIFICATION_TRACKER_END -->"
PHASE_C_THRESHOLD = 3

# Run-level thresholds (mirror verification/README.md)
GREEN_THRESHOLD = 0.95
YELLOW_THRESHOLD = 0.80


# Fields the harness reports — used for per-(channel, field) tabulation.
# Phase A2.4 #4: room_type_canonical_present added so it can be enforced as
# a critical field even when the spec doesn't always assert it.
TRACKED_FIELDS: tuple[str, ...] = (
    "bar_non_ref_nightly_allin",
    "promo_banner_text",
    "promo_banner_pct",
    "weekly_rate_disclosure_present",
    "bundle_inclusions",
    "marketing_name_verbatim",
    "member_rate_gated",
    "min_rate_rows_visible",
    "room_type_canonical_present",
)


# Phase A2.4 #4: aliases to bridge the spec / report / actual{} key drift.
# Cowork harness emits actual{} keys that don't always match the canonical
# TRACKED_FIELDS name. Likewise, spec expected{} keys sometimes use
# `_contains` / `_present` suffixes. The aliases let local recompute see
# the same evidence the report did, so honest agreement / disagreement
# can be detected (vs spurious "field missing" failures).
#
# Format: canonical → (list of spec keys to try, list of actual keys to try).
TRACKED_FIELD_ALIASES: dict[str, tuple[list[str], list[str]]] = {
    "bar_non_ref_nightly_allin": (
        ["bar_non_ref_nightly_allin"],
        ["bar_non_ref_nightly_allin"],
    ),
    "promo_banner_text": (
        ["promo_banner_text_contains", "promo_banner_text"],
        ["promo_banner_text"],
    ),
    "promo_banner_pct": (
        ["promo_banner_pct"],
        ["promo_banner_pct"],
    ),
    "weekly_rate_disclosure_present": (
        ["weekly_rate_disclosure_present"],
        ["weekly_rate_disclosure_present", "weekly_rate_disclosure"],
    ),
    "bundle_inclusions": (
        ["bundle_inclusions"],
        ["bundle_inclusions"],
    ),
    "marketing_name_verbatim": (
        ["marketing_name_verbatim"],
        ["marketing_name_verbatim", "marketing_name_actual", "marketing_name"],
    ),
    "member_rate_gated": (
        ["member_rate_gated"],
        ["member_rate_gated"],
    ),
    "min_rate_rows_visible": (
        ["min_rate_rows_visible", "expected_min_rate_rows"],
        ["min_rate_rows_visible", "rate_rows_visible"],
    ),
    "room_type_canonical_present": (
        ["room_type_canonical_present"],
        ["room_type_canonical_present", "room_canonical_resolved"],
    ),
    # Phase A2.4b #3: rate_plan_label is written into chrome_truth.csv
    # directly from actual{}, so it must be verifiable for the recompute
    # gate. No spec/actual key drift observed in practice.
    "rate_plan_label": (
        ["rate_plan_label"],
        ["rate_plan_label"],
    ),
}


# Phase A2.4 #4(a): fields that MUST have at least one check (PASS or FAIL)
# across the run. If any critical field has zero checks (denom=0), the run
# verdict goes RED — the prior overall_verdict() silently let zero-denom
# fields count as 100% PASS, so a report that omitted a critical field
# entirely could land GREEN.
CRITICAL_FIELDS: tuple[str, ...] = (
    "bar_non_ref_nightly_allin",
    "marketing_name_verbatim",
    "room_type_canonical_present",
)


# Phase A2.4b #3: fields to recompute locally before writing a row into
# tests/fixtures/chrome_truth.csv. CRITICAL_FIELDS is narrower (it gates
# run-level verdict, not fixture writes); the fixture stores more fields
# (promo banner text/pct, rate_plan_label) and any of them being wrong
# poisons regression. Mirrors the actual columns written by
# write_chrome_truth() that originate from the report's actual{} block —
# pure spec metadata (channel/property/checkin/los/adults) is excluded
# because it doesn't come from the report.
FIXTURE_RECOMPUTE_FIELDS: tuple[str, ...] = (
    "bar_non_ref_nightly_allin",
    "marketing_name_verbatim",
    "room_type_canonical_present",
    "promo_banner_text",
    "promo_banner_pct",
    "rate_plan_label",
)


def _first_present(d: dict, keys: list[str]):
    """Return the value of the first key in `keys` that is present (and
    not None) in `d`, else None. Used to bridge spec/actual key drift.

    NOTE: collapses missing-key and explicit-None to the same return
    value. For expected (spec) lookups where None is a meaningful
    assertion ("must be None"), use _first_asserted_in_expected instead.
    """
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _first_asserted_in_expected(d: dict, keys: list[str]):
    """Return (value, present): for the first key that's IN d (even with
    a None value), return (d[k], True). If none of `keys` are in d,
    return (None, False).

    Codex item 2 (2026-04-28): used for spec/expected lookups so a key
    written as `null` in the fixture (e.g., promo_banner_pct: null)
    asserts that actual must also be None — distinct from "the field is
    not asserted at all", which is the missing-key case.
    """
    if not isinstance(d, dict):
        return (None, False)
    for k in keys:
        if k in d:
            return (d[k], True)
    return (None, False)


def _coerce_actual(field_name: str, raw):
    """Normalize the raw actual value into the form cell_field_match expects.
    Currently only `room_type_canonical_present` needs coercion: actual
    `room_canonical_resolved` is a string SKU id, but expected is a bool.
    Treat any non-empty string as 'resolved → present'.
    """
    if raw is None:
        return None
    if field_name == "room_type_canonical_present":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return bool(raw.strip())
        return bool(raw)
    return raw


def load_report(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"report not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_spec() -> dict:
    if not SPEC_PATH.exists():
        raise FileNotFoundError(f"spec not found: {SPEC_PATH}")
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def cell_field_match(
    actual_value, expected_value, field_name: str,
    *, expected_present: bool = True,
) -> Optional[bool]:
    """Return True if actual matches expected, False if it doesn't, None if
    `expected_present` is False (the field wasn't asserted in the spec).

    Codex item 2 (2026-04-28): an explicit `null` in the spec is a real
    assertion that actual must also be None — only a *missing key*
    (expected_present=False) yields the N/A verdict. Callers should
    determine expected_present via `_first_asserted_in_expected`.
    """
    if not expected_present:
        return None
    if expected_value is None:
        return actual_value is None
    if actual_value is None:
        return False
    if field_name == "bar_non_ref_nightly_allin":
        try:
            return abs(float(actual_value) - float(expected_value)) / float(expected_value) <= 0.02
        except (TypeError, ValueError, ZeroDivisionError):
            return False
    if field_name == "promo_banner_text":
        return isinstance(actual_value, str) and isinstance(expected_value, str) \
               and expected_value.lower() in actual_value.lower()
    return actual_value == expected_value


def _resolve_field_values(field_name: str, expected: dict, actual: dict):
    """Look up (expected_value, expected_present, actual_value) for
    `field_name`, honoring TRACKED_FIELD_ALIASES + _coerce_actual.

    `expected_present` is True iff at least one of the spec alias keys is
    present in `expected` (even if its value is None). Codex item 2
    (2026-04-28): callers must skip on `not expected_present`, not on
    `exp_v is None`, so explicit `null` assertions are honored.
    """
    spec_keys, actual_keys = TRACKED_FIELD_ALIASES.get(
        field_name, ([field_name], [field_name])
    )
    exp_v, exp_present = _first_asserted_in_expected(expected, spec_keys)
    act_v = _coerce_actual(field_name, _first_present(actual, actual_keys))
    return exp_v, exp_present, act_v


def aggregate_bias(report: dict, spec: dict) -> dict:
    """Build the per-(channel, field) PASS/FAIL counters used by the bias summary."""
    spec_by_id = {c["id"]: c for c in spec.get("cells", [])}
    counters: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"pass": 0, "fail": 0, "n_a": 0}
    )
    fail_samples: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for entry in report.get("results", report.get("cells", [])):
        cid = entry.get("cell_id") or entry.get("id")
        spec_cell = spec_by_id.get(cid)
        if not spec_cell:
            continue
        channel = spec_cell.get("channel", "?")
        actual = entry.get("actual", {})
        expected = spec_cell.get("expected", {})
        for fld in TRACKED_FIELDS:
            exp_v, exp_present, act_v = _resolve_field_values(fld, expected, actual)
            verdict = cell_field_match(act_v, exp_v, fld, expected_present=exp_present)
            key = (channel, fld)
            if verdict is None:
                counters[key]["n_a"] += 1
            elif verdict:
                counters[key]["pass"] += 1
            else:
                counters[key]["fail"] += 1
                if len(fail_samples[key]) < 3:
                    fail_samples[key].append({
                        "cell_id": cid,
                        "expected": exp_v,
                        "actual": act_v,
                    })

    return {"counters": dict(counters), "fail_samples": dict(fail_samples)}


def overall_verdict(bias: dict) -> str:
    """Return GREEN / YELLOW / RED based on lowest pass-rate per (channel, field).

    Phase A2.4 #4(a): forces RED when any CRITICAL_FIELDS field has zero
    checks across the entire run. The prior code skipped denom=0 entries
    and let `worst` stay at 1.0, so a critical field that wasn't tracked
    at all could land GREEN. Now zero-checked critical fields fail the run.
    """
    counters = bias["counters"]
    if not counters:
        return "RED"

    # Critical-field coverage gate
    # TODO (Phase A2.4b #4, ~2026-04-27): this loop checks that each
    # CRITICAL_FIELDS entry has at least one PASS/FAIL anywhere in the
    # run, but does NOT partition by channel. If a future spec asserts
    # `room_type_canonical_present` for Direct + Booking but accidentally
    # omits it for Hotels.com cells, the run still goes GREEN because the
    # field has SOME checks (just none on Hotels.com). Acceptable for the
    # current 10-cell × 3-channel spec. Tighten to "at least one check
    # per (channel, critical_field)" once the spec grows beyond ~15 cells
    # or adds a 4th channel — until then the partition adds noise without
    # catching real coverage gaps.
    for cf in CRITICAL_FIELDS:
        total_checked = sum(
            c["pass"] + c["fail"]
            for (_, fld), c in counters.items()
            if fld == cf
        )
        if total_checked == 0:
            return "RED"

    worst = 1.0
    for key, c in counters.items():
        denom = c["pass"] + c["fail"]
        if denom == 0:
            continue
        pr = c["pass"] / denom
        if pr < worst:
            worst = pr
    if worst >= GREEN_THRESHOLD:
        return "GREEN"
    if worst >= YELLOW_THRESHOLD:
        return "YELLOW"
    return "RED"


def _entry_passes_local_recompute(
    entry: dict, spec_cell: dict,
    *, fields: tuple[str, ...] = FIXTURE_RECOMPUTE_FIELDS,
) -> tuple[bool, list[str]]:
    """Phase A2.4 #4(c) + Phase A2.4b #3: recompute each of `fields`
    locally for this entry, ignoring the report's self-assessment. Schema
    drift in actual{} could let a report mark a cell PASS even when a
    fixture-written field disagrees with the spec — write_chrome_truth
    must NOT trust report.verdict alone, since the truth fixture is the
    regression ground truth and can't be poisoned by report-side errors.

    The `fields` tuple defaults to FIXTURE_RECOMPUTE_FIELDS — the full
    set of fields actually persisted into chrome_truth.csv. The narrower
    CRITICAL_FIELDS subset gates run-level verdict elsewhere; for the
    fixture write the principle is "if it's in the fixture, verify it,"
    so promo banner + rate_plan_label are checked here too.

    Returns (locally_ok, fail_reasons).
    """
    expected = spec_cell.get("expected", {})
    actual = entry.get("actual", {})
    fail_reasons: list[str] = []
    for fld in fields:
        exp_v, exp_present, act_v = _resolve_field_values(fld, expected, actual)
        if not exp_present:
            continue  # field key absent from spec — N/A
        if not cell_field_match(act_v, exp_v, fld, expected_present=exp_present):
            fail_reasons.append(f"{fld}: expected={exp_v!r}, actual={act_v!r}")
    return (not fail_reasons), fail_reasons


def _resolve_cell_expected_verdict(entry: dict, spec_cell: dict) -> str:
    """Map a Chrome-harness cell entry to one of validators.EXPECTED_VERDICT_VALUES.

    Resolution 8: when an empty-row entry's evidence is bot-block / 404 / 5xx /
    DataDome / Imperva, surface ``FAIL_URL_BROKEN`` distinctly from
    ``PASS_NO_INVENTORY`` (URL works, page renders, cell is legitimately empty).

    Decision is delegated to ``validators.classify_chrome_cell_verdict``;
    this helper just plucks the relevant evidence out of the report entry +
    spec cell.
    """
    sys.path.insert(0, str(REPO_ROOT / "scraper_lib"))
    try:
        from validators import classify_chrome_cell_verdict  # type: ignore
    finally:
        if str(REPO_ROOT / "scraper_lib") in sys.path:
            sys.path.remove(str(REPO_ROOT / "scraper_lib"))

    actual = entry.get("actual", {}) or {}
    rows = actual.get("rows") or actual.get("rate_rows") or []
    rows_extracted = len(rows) if isinstance(rows, list) else int(actual.get("rate_rows_visible") or 0)
    return classify_chrome_cell_verdict(
        cell_status=entry.get("cell_status") or entry.get("status"),
        expected_present=bool(spec_cell.get("expected_present", True)),
        rows_extracted=rows_extracted,
        bot_block_signature=entry.get("bot_block_signature"),
        http_status=entry.get("http_status"),
        empty_page_classification=entry.get("empty_page_classification"),
    )


def write_chrome_truth(report: dict, spec: dict, append: bool) -> int:
    """Write/append a row per PASS cell to tests/fixtures/chrome_truth.csv.
    Returns count of rows written.

    Phase A2.4 #4(c) + Phase A2.4b #3: each candidate row is gated on
    `_entry_passes_local_recompute` over FIXTURE_RECOMPUTE_FIELDS — the
    report's `verdict==PASS` alone is not enough; every actual-derived
    field that ends up in the fixture (BAR rate, marketing name, room
    canonical, promo banner text/pct, rate_plan_label) must locally
    agree with spec expectations. Cells that disagree are skipped with a
    stderr note so the truth fixture stays reliable as regression
    ground truth.

    Resolution 8: each row also carries `expected_verdict_resolved`, the
    classification the harness derived from the cell's evidence
    (PASS / PASS_NO_INVENTORY / FAIL_URL_BROKEN / etc). This lets future
    regression runs distinguish a silently-broken URL from a legitimately
    empty cell — both used to surface as identical NO_DATA rows.
    """
    spec_by_id = {c["id"]: c for c in spec.get("cells", [])}
    verified_at = report.get("verified_at") or report.get("captured_at") or date.today().isoformat()
    rows: list[dict] = []
    for entry in report.get("results", report.get("cells", [])):
        if entry.get("verdict") != "PASS":
            continue
        cid = entry.get("cell_id") or entry.get("id")
        spec_cell = spec_by_id.get(cid)
        if not spec_cell:
            continue
        locally_ok, fail_reasons = _entry_passes_local_recompute(entry, spec_cell)
        if not locally_ok:
            print(
                f"  WARN: skipping PASS row for {cid} — local recompute disagrees: "
                f"{'; '.join(fail_reasons)[:200]}",
                file=sys.stderr,
            )
            continue
        actual = entry.get("actual", {})
        rows.append({
            "verified_at":               verified_at,
            "cell_id":                   cid,
            "channel":                   spec_cell.get("channel"),
            "property":                  spec_cell.get("property"),
            "room_canonical":            spec_cell.get("room_canonical"),
            "checkin":                   spec_cell.get("checkin"),
            "los":                       spec_cell.get("los"),
            "adults":                    spec_cell.get("adults"),
            "bar_non_ref_nightly_allin": actual.get("bar_non_ref_nightly_allin"),
            "rate_plan_label":           actual.get("rate_plan_label"),
            "promo_banner_pct":          actual.get("promo_banner_pct"),
            "promo_banner_text":         actual.get("promo_banner_text"),
            "marketing_name_actual":     actual.get("marketing_name_verbatim")
                                         or actual.get("marketing_name_actual")
                                         or actual.get("marketing_name"),
            "room_canonical_resolved":   actual.get("room_canonical_resolved"),
            "expected_verdict_resolved": _resolve_cell_expected_verdict(entry, spec_cell),
        })
    if not rows:
        return 0
    CHROME_TRUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_header = not (CHROME_TRUTH_PATH.exists() and append)
    mode = "a" if (append and CHROME_TRUTH_PATH.exists()) else "w"
    fieldnames = list(rows[0].keys())
    with open(CHROME_TRUTH_PATH, mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            w.writeheader()
        w.writerows(rows)
    return len(rows)


def render_bias_summary(report: dict, spec: dict, bias: dict, verdict: str) -> str:
    counters = bias["counters"]
    fail_samples = bias["fail_samples"]
    captured = report.get("captured_at") or date.today().isoformat()
    lines: list[str] = []
    lines.append(f"# Chrome verification — bias summary ({captured})")
    lines.append("")
    lines.append(f"**Run-level verdict: `{verdict}`**")
    lines.append("")
    lines.append("## Per-(channel, field) pass rate")
    lines.append("")
    lines.append("| Channel | Field | PASS | FAIL | N/A | Pass-rate |")
    lines.append("|---------|-------|-----:|-----:|----:|----------:|")
    for (ch, fld), c in sorted(counters.items()):
        denom = c["pass"] + c["fail"]
        pr = (c["pass"] / denom) if denom else None
        pr_str = f"{pr*100:.0f}%" if pr is not None else "—"
        lines.append(f"| {ch} | {fld} | {c['pass']} | {c['fail']} | {c['n_a']} | {pr_str} |")
    lines.append("")

    if verdict in ("YELLOW", "RED"):
        lines.append("## Bug list (next claude-code prompt input)")
        lines.append("")
        lines.append(f"Run-level `{verdict}` — do NOT use the latest scrape for IC narrative until resolved.")
        lines.append("")
        for (ch, fld), samples in sorted(fail_samples.items()):
            if not samples:
                continue
            lines.append(f"### Bias: `{ch}` / `{fld}`")
            lines.append("")
            n_fail = counters.get((ch, fld), {}).get("fail", 0)
            lines.append(f"Failures: {n_fail}. Samples:")
            for s in samples:
                lines.append(f"- `{s['cell_id']}` — expected `{s['expected']!r}`, actual `{s['actual']!r}`")
            lines.append("")
        lines.append("## Suggested next-session prompt outline")
        lines.append("")
        lines.append("Open a new claude-code session in this repo with the prompt:")
        lines.append("> The Chrome verification report at `verification/chrome_verification_report.json` ")
        lines.append("> landed `" + verdict + "`. Read `verification/chrome_bias_summary.md` for the bug list, ")
        lines.append("> then propose code fixes for each (channel, field) bias. Stop and confirm before ")
        lines.append("> editing extractor or normalize logic.")
    else:
        lines.append("## Run is GREEN")
        lines.append("")
        lines.append(f"All (channel, field) pairs >= {int(GREEN_THRESHOLD*100)}% PASS. The v2 scrape is safe to use for IC narrative.")
        lines.append("")
        lines.append("PASS cells were appended to `tests/fixtures/chrome_truth.csv` as regression ground truth.")
        lines.append("")
        lines.append("Stop condition for the verification loop overall: 3 consecutive GREEN runs OR Andrew sign-off.")
    return "\n".join(lines)


def update_readme_tracker(report: dict, verdict: str) -> tuple[int, int]:
    """Append this run to run_history.jsonl, then rewrite the
    <!-- VERIFICATION_TRACKER_BEGIN/END --> section of README.md.
    Returns (consecutive_green_count, total_runs).

    Phase C eligibility = consecutive_green_count >= PHASE_C_THRESHOLD.
    Any non-GREEN verdict resets the consecutive counter to 0 on the next
    GREEN run (i.e., the streak is the count of GREEN runs at the tail
    of run_history with no intervening YELLOW/RED).
    """
    results = report.get("results", report.get("cells", []))
    pass_n = sum(1 for r in results if r.get("verdict") == "PASS")
    total = len(results)
    verified_at = report.get("verified_at") or report.get("captured_at") or date.today().isoformat()

    entry = {
        "verified_at": verified_at,
        "verdict": verdict,
        "pass": pass_n,
        "total": total,
        "scraper_commit_under_test": report.get("scraper_commit_under_test"),
    }
    with open(RUN_HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    runs = [json.loads(line) for line in RUN_HISTORY_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    consecutive_green = 0
    for r in reversed(runs):
        if r["verdict"] == "GREEN":
            consecutive_green += 1
        else:
            break

    lines: list[str] = [
        TRACKER_BEGIN,
        "## Verification run tracker",
        "",
        f"**Last run:** {entry['verified_at']} — `{verdict}` ({entry['pass']}/{entry['total']} PASS)",
        f"**Consecutive GREEN runs:** {consecutive_green} of {PHASE_C_THRESHOLD} "
        f"needed for Phase C eligibility (unattended weekly `/schedule` wiring).",
        "",
        "Run history (most recent first):",
    ]
    for r in reversed(runs[-10:]):
        commit = r.get("scraper_commit_under_test") or "?"
        lines.append(f"- {r['verified_at']} — `{r['verdict']}` — {r['pass']}/{r['total']} PASS — scraper `{commit}`")
    lines.append("")
    lines.append(TRACKER_END)
    section = "\n".join(lines)

    txt = README_PATH.read_text(encoding="utf-8")
    if TRACKER_BEGIN in txt and TRACKER_END in txt:
        before, _, rest = txt.partition(TRACKER_BEGIN)
        _, _, after = rest.partition(TRACKER_END)
        new_txt = before + section + after
    else:
        # First time: insert after the H1 line and its following blank line
        out_lines = txt.split("\n")
        insert_at = 1
        # Skip any subtitle paragraph until we find the first blank line after H1
        for i, line in enumerate(out_lines):
            if i > 0 and line.strip() == "":
                insert_at = i + 1
                break
        out_lines.insert(insert_at, section + "\n")
        new_txt = "\n".join(out_lines)

    README_PATH.write_text(new_txt, encoding="utf-8")
    return consecutive_green, len(runs)


def resolve_verdict_disagreement(
    *, local_verdict: str, report_verdict: Optional[str], trust_report: bool,
) -> tuple[str, Optional[str], int]:
    """Phase A2.4 #4(b): the prior code silently honored report.run_verdict
    whenever it disagreed with local aggregation, which let a stale or
    malformed report override correct local math (this happened in the
    2026-04-26 B5 ingest run).

    Returns (final_verdict, error_message, exit_code):
      - error_message is non-None when there's a disagreement to surface.
      - exit_code is 4 (refuse) when local≠report and trust_report=False.
      - exit_code is 0 otherwise; final_verdict is local OR report depending
        on whether trust_report was used.
    """
    if not report_verdict or report_verdict == local_verdict:
        return local_verdict, None, 0
    if not trust_report:
        msg = (
            f"ERROR: report.run_verdict={report_verdict} but local "
            f"aggregation={local_verdict}.\n"
            f"Refusing to honor report. Pass --trust-report-verdict if intentional."
        )
        return local_verdict, msg, 4
    msg = (
        f"--trust-report-verdict: honoring report ({report_verdict}) over "
        f"local aggregation ({local_verdict})"
    )
    return report_verdict, msg, 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report_path", type=Path,
                    help="Path to chrome_verification_report.json from Cowork")
    ap.add_argument("--append-truth", action="store_true",
                    help="Append PASS cells to tests/fixtures/chrome_truth.csv (default: overwrite)")
    ap.add_argument("--bias-only", action="store_true",
                    help="Compute bias summary but skip the truth fixture write")
    ap.add_argument("--rewrite-bias", action="store_true",
                    help="Regenerate chrome_bias_summary.md from the report (default: preserve any Cowork-produced version)")
    # Phase A2.4 #4(b): fail-closed on report/local disagreement. Override
    # only when intentional (e.g., the spec was just edited and the report
    # is from before the edit).
    ap.add_argument("--trust-report-verdict", action="store_true",
                    help="When report.run_verdict disagrees with local aggregation, "
                         "honor the report instead of failing closed. Use only when "
                         "the disagreement is known to be intentional.")
    args = ap.parse_args()

    try:
        report = load_report(args.report_path)
        spec = load_spec()
    except FileNotFoundError as ex:
        print(f"ERROR: {ex}")
        return 3

    bias = aggregate_bias(report, spec)
    verdict = overall_verdict(bias)
    # Phase A2.4 #4(b): see resolve_verdict_disagreement() for fail-closed logic.
    final_verdict, error_msg, exit_code = resolve_verdict_disagreement(
        local_verdict=verdict,
        report_verdict=report.get("run_verdict"),
        trust_report=args.trust_report_verdict,
    )
    if error_msg:
        print(error_msg, file=sys.stderr)
    if exit_code != 0:
        return exit_code
    verdict = final_verdict
    print(f"Run verdict: {verdict}")

    # Cowork's bias summary is canonical (richer per-field bias notes from
    # live-page evidence). Only write our aggregated version if Cowork didn't
    # ship one, or --rewrite-bias is passed.
    if BIAS_SUMMARY_PATH.exists() and not args.rewrite_bias:
        print(f"Bias summary preserved (Cowork-produced): {BIAS_SUMMARY_PATH}")
    else:
        summary = render_bias_summary(report, spec, bias, verdict)
        BIAS_SUMMARY_PATH.write_text(summary, encoding="utf-8")
        print(f"Bias summary -> {BIAS_SUMMARY_PATH}")

    if not args.bias_only:
        n = write_chrome_truth(report, spec, append=args.append_truth)
        print(f"Truth fixture rows written: {n} -> {CHROME_TRUTH_PATH}")

    consecutive_green, total_runs = update_readme_tracker(report, verdict)
    print(f"README tracker -> {consecutive_green}/{PHASE_C_THRESHOLD} consecutive GREEN ({total_runs} total runs)")

    return {"GREEN": 0, "YELLOW": 1, "RED": 2}.get(verdict, 3)


if __name__ == "__main__":
    sys.exit(main())
