"""Phase A2.4 #5 — canonical BAR rate anchor (Rule 9).

Verification step (per brief):
  Unit test with synthetic markdown containing "$295" only, plus an
  extracted plan list that has BAR_NON_REF=$295 (should anchor) AND
  BAR_FLEX=$340 (not in markdown — should fail). Assert: cell fails with
  anchor-violation status.
"""
from __future__ import annotations
import os
import unittest

os.environ.setdefault("FIRECRAWL_API_KEY", "test-key-not-used")


class TestCheckCanonicalBarAnchored(unittest.TestCase):
    def test_brief_canonical_case_bar_anchors_flex_does_not_fail_cell(self):
        from validators import check_canonical_bar_anchored

        markdown = "Best rate $295 / night. Free cancellation."
        extracted = {
            "rooms": [
                {
                    "marketing_name": "One Bedroom Platinum Suite",
                    "rate_plans": [
                        {"rate_plan_label": "Non-refundable",
                         "cancellation_phrase": "Non-refundable",
                         "rate_per_night_usd": 295.0},
                        {"rate_plan_label": "Free cancellation",
                         "cancellation_phrase": "Free cancellation before Jul 10",
                         "rate_per_night_usd": 340.0},
                    ],
                }
            ]
        }
        ok, reason, _ = check_canonical_bar_anchored(extracted, markdown)
        self.assertFalse(ok, "BAR_FLEX=$340 not in markdown → must fail")
        self.assertIn("FAIL_BAR_NOT_ANCHORED", reason)
        self.assertIn("BAR_FLEX", reason)
        self.assertIn("340", reason)

    def test_both_anchored_passes(self):
        from validators import check_canonical_bar_anchored
        markdown = "Non-refundable $295. Free cancellation $340."
        extracted = {
            "rooms": [
                {
                    "marketing_name": "One Bedroom Platinum Suite",
                    "rate_plans": [
                        {"rate_plan_label": "Non-refundable",
                         "cancellation_phrase": "Non-refundable",
                         "rate_per_night_usd": 295.0},
                        {"rate_plan_label": "Free cancellation",
                         "cancellation_phrase": "Free cancellation",
                         "rate_per_night_usd": 340.0},
                    ],
                }
            ]
        }
        ok, reason, _ = check_canonical_bar_anchored(extracted, markdown)
        self.assertTrue(ok, f"both rates anchored — should pass; got {reason!r}")

    def test_bar_non_ref_unanchored_fails(self):
        # The opposite of the brief case — BAR_NON_REF rate isn't in markdown
        from validators import check_canonical_bar_anchored
        markdown = "Free cancellation $340 / night. Standard rate not visible."
        extracted = {
            "rooms": [
                {
                    "marketing_name": "One Bedroom Platinum Suite",
                    "rate_plans": [
                        {"rate_plan_label": "Non-refundable",
                         "cancellation_phrase": "Non-refundable",
                         "rate_per_night_usd": 295.0},
                        {"rate_plan_label": "Free cancellation",
                         "cancellation_phrase": "Free cancellation",
                         "rate_per_night_usd": 340.0},
                    ],
                }
            ]
        }
        ok, reason, _ = check_canonical_bar_anchored(extracted, markdown)
        self.assertFalse(ok)
        self.assertIn("BAR_NON_REF", reason)

    def test_hotels_com_addon_flex_pattern_passes(self):
        # Hotels.com legitimate computed rate: BAR_FLEX = base + addon.
        # base $295 and "+$46" both visible in markdown → should pass.
        from validators import check_canonical_bar_anchored
        markdown = ("Suite, 1 Bedroom (Platinum)  $295 / night.  "
                    "Cancellation policy: Non-Refundable + $0 / Fully refundable + $46 / "
                    "Non-Refundable + $81 / Fully refundable + $128.")
        extracted = {
            "rooms": [
                {
                    "marketing_name": "Suite, 1 Bedroom (Platinum)",
                    "rate_plans": [
                        {"rate_plan_label": "Standard / lead-in non-ref",
                         "cancellation_phrase": "Non-refundable",
                         "rate_per_night_usd": 295.0},
                        {"rate_plan_label": "+$46 add-on (cancel-flex pay-now)",
                         "cancellation_phrase": "Fully refundable",
                         "rate_per_night_usd": 341.0},
                    ],
                }
            ]
        }
        ok, reason, _ = check_canonical_bar_anchored(extracted, markdown)
        self.assertTrue(ok, f"Hotels.com addon path should anchor; got {reason!r}")

    def test_non_canonical_unanchored_warns_but_does_not_fail(self):
        # A non-BAR plan with a fabricated rate should produce a WARN, not a FAIL.
        from validators import check_canonical_bar_anchored
        markdown = "Non-refundable $295. Free cancellation $340. Visa Offer rate not visible."
        extracted = {
            "rooms": [
                {
                    "marketing_name": "One Bedroom Platinum Suite",
                    "rate_plans": [
                        {"rate_plan_label": "Non-refundable",
                         "cancellation_phrase": "Non-refundable",
                         "rate_per_night_usd": 295.0},
                        {"rate_plan_label": "Free cancellation",
                         "cancellation_phrase": "Free cancellation",
                         "rate_per_night_usd": 340.0},
                        # Card-issuer offer, not picked as canonical, but $999 not in markdown
                        {"rate_plan_label": "Visa Offer",
                         "cancellation_phrase": "Free cancellation",
                         "rate_per_night_usd": 999.0},
                    ],
                }
            ]
        }
        ok, reason, warnings = check_canonical_bar_anchored(extracted, markdown)
        self.assertTrue(ok, f"non-canonical unanchored should not fail cell; got {reason!r}")
        self.assertTrue(
            any(w.get("rate") == 999.0 for w in warnings),
            f"expected non_canonical_rate_unanchored warning for $999 in {warnings!r}",
        )

    def test_hotels_com_addon_with_real_firecrawl_serialization_anchors(self):
        # Phase A2.5b regression: Firecrawl serializes Hotels.com radio button
        # rows with NO whitespace between the +$N amount and the trailing
        # 'Reserve now' aria label, e.g. '\\+ $163Reserve now, pay deposit'.
        # The earlier `\b` end-anchor in _bar_rate_anchored fails on the
        # digit→letter transition ($163R), even though semantically $163 is
        # the add-on amount. Use real-shape markdown to lock the fix.
        from validators import check_canonical_bar_anchored
        markdown = (
            "### Suite, 1 Bedroom (Platinum)\n"
            "- 820 sq ft\n"
            "#### Cancellation policy\n"
            "Non-Refundable\n"
            "\\+ $0Reserve now, pay depositReserve now, pay deposit\n"
            "Fully refundable before Sep 23\n"
            "\\+ $163Reserve now, pay depositReserve now, pay deposit\n"
            "$348 nightly\n"
        )
        extracted = {
            "rooms": [{
                "marketing_name": "Suite, 1 Bedroom (Platinum)",
                "rate_plans": [
                    {"rate_plan_label": "Non-Refundable + $0 (pay deposit)",
                     "cancellation_phrase": "Non-Refundable",
                     "rate_per_night_usd": 348.0},
                    {"rate_plan_label": "Fully refundable + $163 (pay deposit)",
                     "cancellation_phrase": "Fully refundable before Sep 23",
                     "rate_per_night_usd": 511.0},
                ],
            }],
        }
        ok, reason, _ = check_canonical_bar_anchored(extracted, markdown)
        self.assertTrue(ok,
                        f"both rates should anchor against real-shape markdown; "
                        f"got reason={reason!r}")

    def test_longer_number_does_not_match_substring(self):
        # Sanity: $163 must NOT anchor against $1634 in markdown.
        from validators import check_canonical_bar_anchored
        markdown = "Some rate listed as $1634 / night."
        extracted = {
            "rooms": [{
                "marketing_name": "Phantom",
                "rate_plans": [
                    {"rate_plan_label": "Non-refundable",
                     "cancellation_phrase": "Non-refundable",
                     "rate_per_night_usd": 163.0},
                ],
            }],
        }
        ok, reason, _ = check_canonical_bar_anchored(extracted, markdown)
        self.assertFalse(ok, "$163 should NOT match $1634 substring")

    def test_thousand_separator_in_markdown_anchors(self):
        # AKA Penthouse rates display as "$1,807" in markdown.
        from validators import check_canonical_bar_anchored
        markdown = "One Bedroom Terrace Penthouse — Pay Now and Save $1,807 / night."
        extracted = {
            "rooms": [
                {
                    "marketing_name": "One Bedroom Terrace Penthouse",
                    "rate_plans": [
                        {"rate_plan_label": "Pay Now and Save",
                         "cancellation_phrase": "Non-refundable",
                         "rate_per_night_usd": 1807.0},
                    ],
                }
            ]
        }
        ok, _, _ = check_canonical_bar_anchored(extracted, markdown)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
