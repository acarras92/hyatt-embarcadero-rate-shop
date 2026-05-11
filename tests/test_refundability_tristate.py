"""Phase A2.4 #3 — refundability_state() tri-state coverage + integration.

Verification step (per brief):
  Unit tests covering all three states with canonical strings:
    "Free cancellation before Jul 10" → REFUNDABLE
    "Non-refundable"                  → NON_REFUNDABLE
    "Standard Rate"                   → UNKNOWN
  Plus an integration test where a plan with policy "Standard Rate" does
  NOT get picked as BAR_FLEX.
"""
from __future__ import annotations
import unittest

from normalize import (
    REFUNDABLE,
    NON_REFUNDABLE,
    UNKNOWN,
    refundability_state,
    pick_canonical_bar,
    pick_canonical_flex,
)


class TestRefundabilityState(unittest.TestCase):
    def test_free_cancellation_phrase_is_refundable(self):
        plan = {"cancellation_phrase": "Free cancellation before Jul 10",
                "rate_plan_label": "", "rate_per_night_usd": 320.0}
        self.assertEqual(refundability_state(plan), REFUNDABLE)

    def test_fully_refundable_phrase_is_refundable(self):
        plan = {"cancellation_phrase": "Fully refundable",
                "rate_plan_label": "", "rate_per_night_usd": 320.0}
        self.assertEqual(refundability_state(plan), REFUNDABLE)

    def test_non_refundable_phrase_is_non_refundable(self):
        plan = {"cancellation_phrase": "Non-refundable",
                "rate_plan_label": "", "rate_per_night_usd": 285.0}
        self.assertEqual(refundability_state(plan), NON_REFUNDABLE)

    def test_non_refundable_with_qualifier_is_non_refundable(self):
        plan = {"cancellation_phrase": "Non-refundable - no changes allowed",
                "rate_plan_label": "", "rate_per_night_usd": 285.0}
        self.assertEqual(refundability_state(plan), NON_REFUNDABLE)

    def test_standard_rate_label_is_unknown(self):
        plan = {"cancellation_phrase": "",
                "rate_plan_label": "Standard Rate", "rate_per_night_usd": 320.0}
        self.assertEqual(refundability_state(plan), UNKNOWN)

    def test_empty_phrase_and_label_is_unknown(self):
        plan = {"cancellation_phrase": "", "rate_plan_label": "",
                "rate_per_night_usd": 320.0}
        self.assertEqual(refundability_state(plan), UNKNOWN)

    def test_pay_now_label_is_non_refundable_via_label_fallback(self):
        # Label fallback when phrase is missing — "Pay Now and Save" is a
        # deterministic non-ref token used by AKA Direct.
        plan = {"cancellation_phrase": "",
                "rate_plan_label": "Pay Now and Save",
                "rate_per_night_usd": 342.0}
        self.assertEqual(refundability_state(plan), NON_REFUNDABLE)

    def test_llm_refundable_bool_is_ignored(self):
        # Brief: "The LLM-emitted `refundable` bool is NOT consulted." Plan
        # has refundable=true but no phrase or label tokens — must be UNKNOWN.
        plan = {"cancellation_phrase": "",
                "rate_plan_label": "Standard Rate",
                "refundable": True,
                "rate_per_night_usd": 320.0}
        self.assertEqual(refundability_state(plan), UNKNOWN)


class TestPickerExcludesUnknown(unittest.TestCase):
    """Integration: pick_canonical_flex must NOT pick a plan with policy
    'Standard Rate' (i.e. UNKNOWN refundability). Brief verification step
    for #3."""

    def test_standard_rate_plan_is_not_picked_as_flex(self):
        plans = [
            {"rate_plan_label": "Standard Rate",
             "cancellation_phrase": "",
             "rate_per_night_usd": 320.0,
             "bundle_inclusions": None,
             "refundable": True},  # LLM bool intentionally true; must be ignored
        ]
        pick = pick_canonical_flex(plans)
        self.assertIsNone(pick, "UNKNOWN-refundability plan must not be picked as BAR_FLEX")

    def test_standard_rate_plan_is_not_picked_as_bar(self):
        plans = [
            {"rate_plan_label": "Standard Rate",
             "cancellation_phrase": "",
             "rate_per_night_usd": 320.0,
             "bundle_inclusions": None,
             "refundable": False},
        ]
        pick = pick_canonical_bar(plans)
        self.assertIsNone(pick, "UNKNOWN-refundability plan must not be picked as BAR_NON_REF")

    def test_non_ref_with_explicit_phrase_is_picked_as_bar(self):
        plans = [
            {"rate_plan_label": "Standard Rate",
             "cancellation_phrase": "Non-refundable",
             "rate_per_night_usd": 285.0,
             "bundle_inclusions": None},
            {"rate_plan_label": "Free cancellation",
             "cancellation_phrase": "Free cancellation before Jul 10",
             "rate_per_night_usd": 320.0,
             "bundle_inclusions": None},
        ]
        bar = pick_canonical_bar(plans)
        self.assertIsNotNone(bar)
        self.assertEqual(bar[0], 0)
        self.assertEqual(bar[1]["rate_per_night_usd"], 285.0)

    def test_refundable_with_explicit_phrase_is_picked_as_flex(self):
        plans = [
            {"rate_plan_label": "Non-refundable",
             "cancellation_phrase": "Non-refundable",
             "rate_per_night_usd": 285.0,
             "bundle_inclusions": None},
            {"rate_plan_label": "Free cancellation",
             "cancellation_phrase": "Free cancellation before Jul 10",
             "rate_per_night_usd": 320.0,
             "bundle_inclusions": None},
        ]
        flex = pick_canonical_flex(plans)
        self.assertIsNotNone(flex)
        self.assertEqual(flex[0], 1)
        self.assertEqual(flex[1]["rate_per_night_usd"], 320.0)


if __name__ == "__main__":
    unittest.main()
