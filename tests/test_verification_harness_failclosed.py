"""Phase A2.4 #4 — verification harness fail-closed (sub-issues a, b, c).

Verification steps (per brief):
  Three unit tests, one per sub-issue:
    (a) Synthesize a report where a critical field has zero checks; assert
        overall_verdict() returns RED.
    (b) Synthesize a report where report.run_verdict=GREEN but local
        aggregation says RED; assert script exits with nonzero status and
        the warning message; assert --trust-report-verdict flag overrides.
    (c) Synthesize a report where entry.verdict=PASS but the tracked
        field's actual value disagrees with expected; assert that row is
        NOT written to chrome_truth.csv.
"""
from __future__ import annotations
import csv
import sys
import unittest
from pathlib import Path

# verification/apply_chrome_verification.py lives outside the conftest's
# scraper_lib path. Add the verification dir manually for the tests.
REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFICATION_DIR = REPO_ROOT / "verification"
if str(VERIFICATION_DIR) not in sys.path:
    sys.path.insert(0, str(VERIFICATION_DIR))


class TestOverallVerdictCriticalFieldsCoverage(unittest.TestCase):
    """(a) — When a critical field has zero checks, overall_verdict() must
    return RED. Prior code skipped denom=0 entries and let the worst-pass-
    rate stay at 1.0, returning GREEN."""

    def test_zero_checks_for_critical_field_forces_red(self):
        from apply_chrome_verification import overall_verdict
        # Counters present for all NON-critical fields with 100% pass; the
        # critical bar_non_ref_nightly_allin field has zero entries (was
        # never tracked in this report).
        bias = {
            "counters": {
                ("direct", "promo_banner_text"): {"pass": 4, "fail": 0, "n_a": 0},
                ("direct", "marketing_name_verbatim"): {"pass": 4, "fail": 0, "n_a": 0},
                ("direct", "room_type_canonical_present"): {"pass": 4, "fail": 0, "n_a": 0},
                # Note: bar_non_ref_nightly_allin completely absent
            },
            "fail_samples": {},
        }
        verdict = overall_verdict(bias)
        self.assertEqual(verdict, "RED",
                         "missing critical field bar_non_ref_nightly_allin "
                         "must force RED, not silently pass")

    def test_zero_checks_for_marketing_name_verbatim_forces_red(self):
        from apply_chrome_verification import overall_verdict
        bias = {
            "counters": {
                ("direct", "bar_non_ref_nightly_allin"): {"pass": 4, "fail": 0, "n_a": 0},
                ("direct", "room_type_canonical_present"): {"pass": 4, "fail": 0, "n_a": 0},
                # marketing_name_verbatim absent
            },
            "fail_samples": {},
        }
        self.assertEqual(overall_verdict(bias), "RED")

    def test_all_critical_present_with_full_pass_returns_green(self):
        from apply_chrome_verification import overall_verdict
        bias = {
            "counters": {
                ("direct", "bar_non_ref_nightly_allin"): {"pass": 4, "fail": 0, "n_a": 0},
                ("direct", "marketing_name_verbatim"): {"pass": 4, "fail": 0, "n_a": 0},
                ("direct", "room_type_canonical_present"): {"pass": 4, "fail": 0, "n_a": 0},
            },
            "fail_samples": {},
        }
        self.assertEqual(overall_verdict(bias), "GREEN")


class TestVerdictDisagreementFailClosed(unittest.TestCase):
    """(b) — When report.run_verdict disagrees with local aggregation, the
    script must exit nonzero with a warning, unless --trust-report-verdict."""

    def test_disagreement_without_trust_returns_nonzero_with_message(self):
        from apply_chrome_verification import resolve_verdict_disagreement
        final, msg, code = resolve_verdict_disagreement(
            local_verdict="RED", report_verdict="GREEN", trust_report=False,
        )
        self.assertEqual(code, 4)
        self.assertIsNotNone(msg)
        self.assertIn("ERROR", msg)
        self.assertIn("report.run_verdict=GREEN", msg)
        self.assertIn("local aggregation=RED", msg)
        self.assertIn("--trust-report-verdict", msg)
        # final_verdict stays at the local (refusing the report)
        self.assertEqual(final, "RED")

    def test_disagreement_with_trust_overrides_to_report_verdict(self):
        from apply_chrome_verification import resolve_verdict_disagreement
        final, msg, code = resolve_verdict_disagreement(
            local_verdict="RED", report_verdict="GREEN", trust_report=True,
        )
        self.assertEqual(code, 0)
        self.assertEqual(final, "GREEN")
        self.assertIsNotNone(msg)
        self.assertIn("--trust-report-verdict", msg)

    def test_agreement_does_not_warn(self):
        from apply_chrome_verification import resolve_verdict_disagreement
        final, msg, code = resolve_verdict_disagreement(
            local_verdict="GREEN", report_verdict="GREEN", trust_report=False,
        )
        self.assertEqual(code, 0)
        self.assertIsNone(msg)
        self.assertEqual(final, "GREEN")


class TestWriteChromeTruthLocalRecomputeGate(unittest.TestCase):
    """(c) — write_chrome_truth must NOT write a row for a PASS entry whose
    locally-recomputed tracked critical fields disagree with the spec.
    Schema drift could let a misformed PASS poison the regression fixture."""

    def test_pass_entry_with_disagreeing_actual_is_not_written(self):
        # Synthesize: spec says expected.bar_non_ref_nightly_allin=300, but
        # actual reports 999 (way outside ±2%). Report marks verdict=PASS
        # (a Cowork-side bug). Local recompute must catch the disagreement.
        from apply_chrome_verification import write_chrome_truth, CHROME_TRUTH_PATH
        spec = {
            "cells": [
                {
                    "id": "bad_cell",
                    "channel": "direct",
                    "property": "aka_white_house",
                    "room_canonical": "1BR_PLATINUM",
                    "checkin": "2026-07-13",
                    "los": 1,
                    "adults": 2,
                    "expected": {
                        "bar_non_ref_nightly_allin": 300.0,
                        "marketing_name_verbatim": "One Bedroom Platinum Suite",
                        "room_type_canonical_present": True,
                    },
                },
                {
                    "id": "good_cell",
                    "channel": "direct",
                    "property": "aka_white_house",
                    "room_canonical": "1BR_PLATINUM_HSV",
                    "checkin": "2026-07-13",
                    "los": 1,
                    "adults": 2,
                    "expected": {
                        "bar_non_ref_nightly_allin": 362.0,
                        "marketing_name_verbatim": "One Bedroom Platinum Suite - H Street View",
                        "room_type_canonical_present": True,
                    },
                },
            ]
        }
        report = {
            "verified_at": "2026-04-26",
            "results": [
                {
                    "cell_id": "bad_cell",
                    "verdict": "PASS",  # report says PASS, but actual disagrees
                    "actual": {
                        "bar_non_ref_nightly_allin": 999.0,  # very wrong vs 300
                        "marketing_name": "One Bedroom Platinum Suite",
                        "room_canonical_resolved": "1BR_PLATINUM",
                    },
                },
                {
                    "cell_id": "good_cell",
                    "verdict": "PASS",
                    "actual": {
                        "bar_non_ref_nightly_allin": 362.0,
                        "marketing_name": "One Bedroom Platinum Suite - H Street View",
                        "room_canonical_resolved": "1BR_PLATINUM_HSV",
                    },
                },
            ],
        }

        # Redirect CHROME_TRUTH_PATH to a temp file so we don't pollute the
        # real fixture.
        import tempfile
        import apply_chrome_verification as acv
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td) / "chrome_truth.csv"
            original = acv.CHROME_TRUTH_PATH
            acv.CHROME_TRUTH_PATH = tmp_path
            try:
                n = write_chrome_truth(report, spec, append=False)
            finally:
                acv.CHROME_TRUTH_PATH = original

            self.assertEqual(n, 1, "only the good_cell row should be written; bad_cell skipped")
            with open(tmp_path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["cell_id"], "good_cell")


class TestWriteChromeTruthRecomputesAllFixtureFields(unittest.TestCase):
    """Phase A2.4b #3 — `_entry_passes_local_recompute` must verify every
    field that ends up in the fixture, not just CRITICAL_FIELDS. The bug:
    a PASS entry whose bar_non_ref_nightly_allin matched but
    promo_banner_pct disagreed with spec was being written to
    chrome_truth.csv anyway, poisoning regression ground truth.

    Verification step (per brief): entry.verdict=PASS, critical field
    matches, but promo_banner_pct disagrees. Assert: row NOT written.
    """

    def test_promo_pct_disagreement_blocks_fixture_write(self):
        from apply_chrome_verification import write_chrome_truth
        spec = {
            "cells": [
                {
                    "id": "promo_mismatch_cell",
                    "channel": "direct",
                    "property": "aka_white_house",
                    "room_canonical": "1BR_PLATINUM",
                    "checkin": "2026-07-13",
                    "los": 1,
                    "adults": 2,
                    "expected": {
                        # Critical fields all match — would pass under the
                        # narrower CRITICAL_FIELDS recompute.
                        "bar_non_ref_nightly_allin": 342.0,
                        "marketing_name_verbatim": "One Bedroom Platinum Suite",
                        "room_type_canonical_present": True,
                        # But promo_banner_pct expected = 15, actual = 25.
                        # Without the broader recompute, this disagreement
                        # silently lands in the fixture as 25.
                        "promo_banner_pct": 15,
                    },
                },
            ]
        }
        report = {
            "verified_at": "2026-04-27",
            "results": [
                {
                    "cell_id": "promo_mismatch_cell",
                    "verdict": "PASS",
                    "actual": {
                        "bar_non_ref_nightly_allin": 342.0,
                        "marketing_name": "One Bedroom Platinum Suite",
                        "room_canonical_resolved": "1BR_PLATINUM",
                        "promo_banner_pct": 25,  # disagrees with spec=15
                        "promo_banner_text": "Pay Now and Save up to 25pct",
                    },
                },
            ],
        }
        import csv
        import tempfile
        from pathlib import Path
        import apply_chrome_verification as acv
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td) / "chrome_truth.csv"
            original = acv.CHROME_TRUTH_PATH
            acv.CHROME_TRUTH_PATH = tmp_path
            try:
                n = write_chrome_truth(report, spec, append=False)
            finally:
                acv.CHROME_TRUTH_PATH = original

            self.assertEqual(n, 0,
                             "row must NOT be written when promo_banner_pct "
                             "disagrees with spec, even though every "
                             "CRITICAL_FIELDS check passes")
            self.assertFalse(tmp_path.exists() and tmp_path.stat().st_size > 0,
                             "fixture file should be empty / not created when "
                             "all candidate rows fail the recompute gate")

    def test_rate_plan_label_disagreement_blocks_fixture_write(self):
        # rate_plan_label is also written into the fixture — if spec
        # expects "Non-refundable" and actual reports "Standard Rate" the
        # row must be skipped.
        from apply_chrome_verification import write_chrome_truth
        spec = {
            "cells": [{
                "id": "rate_plan_mismatch",
                "channel": "direct",
                "property": "aka_white_house",
                "room_canonical": "1BR_PLATINUM",
                "checkin": "2026-07-13",
                "los": 1,
                "adults": 2,
                "expected": {
                    "bar_non_ref_nightly_allin": 342.0,
                    "marketing_name_verbatim": "One Bedroom Platinum Suite",
                    "room_type_canonical_present": True,
                    "rate_plan_label": "Non-refundable",
                },
            }]
        }
        report = {
            "verified_at": "2026-04-27",
            "results": [{
                "cell_id": "rate_plan_mismatch",
                "verdict": "PASS",
                "actual": {
                    "bar_non_ref_nightly_allin": 342.0,
                    "marketing_name": "One Bedroom Platinum Suite",
                    "room_canonical_resolved": "1BR_PLATINUM",
                    "rate_plan_label": "Standard Rate",  # disagrees
                },
            }]
        }
        import tempfile
        from pathlib import Path
        import apply_chrome_verification as acv
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td) / "chrome_truth.csv"
            original = acv.CHROME_TRUTH_PATH
            acv.CHROME_TRUTH_PATH = tmp_path
            try:
                n = write_chrome_truth(report, spec, append=False)
            finally:
                acv.CHROME_TRUTH_PATH = original
            self.assertEqual(n, 0, "rate_plan_label disagreement must block fixture write")

    def test_all_fixture_fields_agree_writes_row(self):
        # Sanity: when every fixture-written field agrees with spec, the
        # row is written. Without this we can't tell if the gate is
        # always-blocking.
        from apply_chrome_verification import write_chrome_truth
        spec = {
            "cells": [{
                "id": "all_agree",
                "channel": "direct",
                "property": "aka_white_house",
                "room_canonical": "1BR_PLATINUM",
                "checkin": "2026-07-13",
                "los": 1,
                "adults": 2,
                "expected": {
                    "bar_non_ref_nightly_allin": 342.0,
                    "marketing_name_verbatim": "One Bedroom Platinum Suite",
                    "room_type_canonical_present": True,
                    "promo_banner_pct": 15,
                    "promo_banner_text": "Pay Now",
                    "rate_plan_label": "Non-refundable",
                },
            }]
        }
        report = {
            "verified_at": "2026-04-27",
            "results": [{
                "cell_id": "all_agree",
                "verdict": "PASS",
                "actual": {
                    "bar_non_ref_nightly_allin": 342.0,
                    "marketing_name": "One Bedroom Platinum Suite",
                    "room_canonical_resolved": "1BR_PLATINUM",
                    "promo_banner_pct": 15,
                    "promo_banner_text": "Pay Now and Save up to 15pct",
                    "rate_plan_label": "Non-refundable",
                },
            }]
        }
        import tempfile
        from pathlib import Path
        import apply_chrome_verification as acv
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td) / "chrome_truth.csv"
            original = acv.CHROME_TRUTH_PATH
            acv.CHROME_TRUTH_PATH = tmp_path
            try:
                n = write_chrome_truth(report, spec, append=False)
            finally:
                acv.CHROME_TRUTH_PATH = original
            self.assertEqual(n, 1, "fully-agreeing entry must be written")


if __name__ == "__main__":
    unittest.main()
