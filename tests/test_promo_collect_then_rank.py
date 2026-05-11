"""Phase A2.4 #6 — promo collect-then-rank.

Verification step (per brief):
  Unit test with synthetic markdown containing both
  "Pay now and enjoy savings up to 15pct" AND "Visa Worlds Offer ... up to
  10pct off best flexible rate". Assert: canonical promo_banner_text
  contains "Pay now"; canonical promo_banner_pct is 15;
  member_or_card_offer_text contains the Visa string.
"""
from __future__ import annotations
import os
import unittest

os.environ.setdefault("FIRECRAWL_API_KEY", "test-key-not-used")


class TestPromoCollectThenRank(unittest.TestCase):
    def test_brief_pay_now_wins_over_visa_offer_even_when_visa_appears_first(self):
        from scrape import extract_promo_signals
        # Visa text deliberately appears BEFORE Pay Now so first-match-wins
        # would have picked the wrong one. Collect-then-rank must pick Pay Now.
        markdown = (
            "Visa Worlds Offer — enjoy up to 10pct off best flexible rate when "
            "paid with a Visa card. Pay now and enjoy savings up to 15pct on "
            "this stay. Comp WiFi."
        )
        signals = extract_promo_signals(markdown)
        self.assertIn("Pay now", signals["promo_banner_text"])
        self.assertEqual(signals["promo_banner_pct"], 15.0)
        self.assertIsNotNone(signals["member_or_card_offer_text"])
        self.assertIn(
            "Visa", signals["member_or_card_offer_text"],
            f"member_or_card_offer_text should capture the Visa text; "
            f"got {signals['member_or_card_offer_text']!r}",
        )

    def test_percent_off_only(self):
        # Booking-style 15% off banner with no Pay Now copy.
        from scrape import extract_promo_signals
        markdown = "**15% off**  Limited time. Reduced weekly rate available."
        signals = extract_promo_signals(markdown)
        self.assertIn("15% off", signals["promo_banner_text"])
        self.assertEqual(signals["promo_banner_pct"], 15.0)

    def test_weekly_rate_disclosure_lowest_priority(self):
        # Only WEEKLY_RATE_DISCLOSURE present — should be the canonical pick.
        from scrape import extract_promo_signals
        markdown = "You're getting a reduced weekly rate because this property..."
        signals = extract_promo_signals(markdown)
        self.assertIn("reduced weekly rate", signals["promo_banner_text"].lower())
        self.assertIsNone(signals["promo_banner_pct"])

    def test_visa_only_does_not_become_canonical_promo(self):
        # If only card-issuer copy is present, canonical promo stays None;
        # the Visa text lives in member_or_card_offer_text.
        from scrape import extract_promo_signals
        markdown = "Mastercard Offer — enjoy up to 10pct off best flexible rate."
        signals = extract_promo_signals(markdown)
        self.assertIsNone(signals["promo_banner_text"])
        self.assertIsNone(signals["promo_banner_pct"])
        self.assertIsNotNone(signals["member_or_card_offer_text"])
        self.assertIn("Mastercard", signals["member_or_card_offer_text"])

    def test_member_gated_signin_classified_as_member(self):
        from scrape import extract_promo_signals
        markdown = "Sign in to see member-only prices on this stay."
        signals = extract_promo_signals(markdown)
        # canonical promo None — only member-gated copy
        self.assertIsNone(signals["promo_banner_text"])
        self.assertIsNotNone(signals["member_or_card_offer_text"])

    def test_pay_now_picks_pct_from_correct_match(self):
        # When PAY_NOW pattern matches AND a separate "X% off" appears, the
        # canonical pct should come from the PAY_NOW match (highest priority),
        # not the 8% off elsewhere.
        from scrape import extract_promo_signals
        markdown = ("Earlier banner: 8% off seasonal rates.  "
                    "Pay now and save up to 15pct on extended stays. ")
        signals = extract_promo_signals(markdown)
        self.assertEqual(signals["promo_banner_pct"], 15.0,
                         "PAY_NOW_AND_SAVE pct (15) should win over PERCENT_OFF (8)")


if __name__ == "__main__":
    unittest.main()
