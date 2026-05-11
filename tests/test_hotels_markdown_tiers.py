"""Phase A2.5b-#2a — deterministic parser for Hotels.com cancellation tiers.

Anchored on real markdown captured from the 2026-04-26 part2 smoke run, so
the regex stays calibrated to Firecrawl's actual serialization (escaped
'\\+ $N', triple-repeated ARIA labels, `$N nightly` lead-in anchor).
"""
from __future__ import annotations
import os
import unittest

os.environ.setdefault("FIRECRAWL_API_KEY", "test-key-not-used")


# Real markdown from cell_output/aka_white_house__hotels_com__2026-09-26__LOS3
# (trimmed to two rooms — one 4-tier, one 2-tier — for fixture economy).
SAMPLE_MARKDOWN = r"""## Choose your unit

### View all photos for Suite, 1 Bedroom (Platinum)

![photo](https://example/photo.jpg)

### Suite, 1 Bedroom (Platinum)

- 820 sq ft

- Sleeps 2

- 1 King Bed

- High-speed internet access


More detailsMore details for Suite, 1 Bedroom (Platinum)

#### Cancellation policy

per stay

More details on all policy options

Cancellation policy

Non-Refundable
\+ $0Reserve now, pay depositReserve now, pay deposit

Fully refundable before Sep 23
\+ $163Reserve now, pay depositReserve now, pay deposit

Non-Refundable
\+ $242Reserve now, pay laterReserve now, pay later

Fully refundable before Sep 23
\+ $407Reserve now, pay laterReserve now, pay later

#### Extras

per night

Extras

No extras
\+ $0

Parking
\+ $0

$348 nightly

The current price is $1,257 total

$1,257 total

Total with taxes and fees

ReserveReserve Suite, 1 Bedroom (Platinum)

You will not be charged yet

### View all photos for Penthouse, 1 Bedroom, Terrace

![photo](https://example/photo2.jpg)

### Penthouse, 1 Bedroom, Terrace

- 820 sq ft

- Sleeps 2

- 1 King Bed


More detailsMore details for Penthouse, 1 Bedroom, Terrace

#### Cancellation policy

per stay

More details on all policy options

Cancellation policy

Non-Refundable
\+ $0Reserve now, pay laterReserve now, pay later

Fully refundable before Sep 23
\+ $393Reserve now, pay depositReserve now, pay deposit

#### Extras

per night

Extras

No extras
\+ $0

Parking
\+ $81

$832 nightly

The current price is $2,940 total

ReserveReserve Penthouse, 1 Bedroom, Terrace
"""


class TestFindRoomBlock(unittest.TestCase):
    def test_finds_correct_block_for_first_room(self):
        from hotels_markdown import find_room_block
        block = find_room_block(SAMPLE_MARKDOWN, "Suite, 1 Bedroom (Platinum)")
        self.assertIsNotNone(block)
        self.assertIn("$348 nightly", block)
        # Must NOT bleed into the next room.
        self.assertNotIn("Penthouse, 1 Bedroom, Terrace", block)
        self.assertNotIn("$832 nightly", block)

    def test_finds_correct_block_for_penthouse(self):
        from hotels_markdown import find_room_block
        block = find_room_block(SAMPLE_MARKDOWN, "Penthouse, 1 Bedroom, Terrace")
        self.assertIsNotNone(block)
        self.assertIn("$832 nightly", block)
        self.assertNotIn("$348 nightly", block)

    def test_returns_none_for_missing_room(self):
        from hotels_markdown import find_room_block
        self.assertIsNone(find_room_block(SAMPLE_MARKDOWN, "Suite, 3 Bedrooms (Imaginary)"))

    def test_handles_whitespace_variation_in_name(self):
        from hotels_markdown import find_room_block
        # Extra whitespace in caller's name shouldn't matter.
        self.assertIsNotNone(find_room_block(SAMPLE_MARKDOWN,
                                             "  Suite, 1 Bedroom (Platinum)  "))


class TestParseCancellationTiers(unittest.TestCase):
    def test_four_tier_block(self):
        from hotels_markdown import find_room_block, parse_cancellation_tiers
        block = find_room_block(SAMPLE_MARKDOWN, "Suite, 1 Bedroom (Platinum)")
        tiers = parse_cancellation_tiers(block)
        self.assertEqual(len(tiers), 4)
        # Four tiers in source order.
        self.assertEqual(
            [(t["refundability"], t["pay_timing"], t["add_on_usd"]) for t in tiers],
            [("Non-Refundable", "deposit", 0),
             ("Fully refundable before Sep 23", "deposit", 163),
             ("Non-Refundable", "later", 242),
             ("Fully refundable before Sep 23", "later", 407)],
        )

    def test_two_tier_penthouse(self):
        from hotels_markdown import find_room_block, parse_cancellation_tiers
        block = find_room_block(SAMPLE_MARKDOWN, "Penthouse, 1 Bedroom, Terrace")
        tiers = parse_cancellation_tiers(block)
        self.assertEqual(len(tiers), 2)
        self.assertEqual(tiers[0]["refundability"], "Non-Refundable")
        self.assertEqual(tiers[0]["pay_timing"], "later")
        self.assertEqual(tiers[0]["add_on_usd"], 0)
        self.assertTrue(tiers[1]["refundability"].lower().startswith("fully refundable"))
        self.assertEqual(tiers[1]["add_on_usd"], 393)

    def test_empty_block_returns_no_tiers(self):
        from hotels_markdown import parse_cancellation_tiers
        self.assertEqual(parse_cancellation_tiers(""), [])

    def test_block_without_radio_buttons_returns_no_tiers(self):
        from hotels_markdown import parse_cancellation_tiers
        boilerplate = "### Some Room\n- 700 sq ft\n$295 nightly\n"
        self.assertEqual(parse_cancellation_tiers(boilerplate), [])


class TestParseLeadInNightly(unittest.TestCase):
    def test_pulls_first_nightly_anchor(self):
        from hotels_markdown import find_room_block, parse_lead_in_nightly
        block = find_room_block(SAMPLE_MARKDOWN, "Suite, 1 Bedroom (Platinum)")
        self.assertEqual(parse_lead_in_nightly(block), 348)

    def test_handles_comma_grouped(self):
        from hotels_markdown import parse_lead_in_nightly
        self.assertEqual(parse_lead_in_nightly("$1,234 nightly"), 1234)

    def test_returns_none_when_absent(self):
        from hotels_markdown import parse_lead_in_nightly
        self.assertIsNone(parse_lead_in_nightly("Suite stuff but no rate."))


class TestExpandTiersFromMarkdown(unittest.TestCase):
    def test_replaces_llm_plans_with_four_tier_expansion(self):
        from hotels_markdown import expand_tiers_from_markdown
        # Simulate the LLM mis-extraction observed in production: only the
        # lead-in tier captured, and the +$163 mis-read as the absolute rate.
        extracted = {
            "property_name": "AKA White House", "nights": 3, "arrival_date": "2026-09-26",
            "rooms": [
                {"marketing_name": "Suite, 1 Bedroom (Platinum)",
                 "rate_plans": [
                     {"rate_plan_label": "Non-Refundable",
                      "rate_per_night_usd": 348, "refundable": False},
                     {"rate_plan_label": "Fully refundable before Sep 23",
                      "rate_per_night_usd": 163, "refundable": True},
                 ]},
                {"marketing_name": "Penthouse, 1 Bedroom, Terrace",
                 "rate_plans": [
                     {"rate_plan_label": "Non-Refundable",
                      "rate_per_night_usd": 832, "refundable": False},
                 ]},
            ],
        }
        new_extracted, diag = expand_tiers_from_markdown(extracted, SAMPLE_MARKDOWN)
        self.assertEqual(diag["rooms_replaced"], 2)
        self.assertEqual(diag["rooms_passthrough"], 0)
        self.assertEqual(diag["tiers_total"], 6)  # 4 + 2

        # Suite plans
        suite = new_extracted["rooms"][0]
        self.assertEqual(len(suite["rate_plans"]), 4)
        nightlies = [p["rate_per_night_usd"] for p in suite["rate_plans"]]
        self.assertEqual(nightlies, [348, 348 + 163, 348 + 242, 348 + 407])
        # Refundability tagging
        refundables = [p["refundable"] for p in suite["rate_plans"]]
        self.assertEqual(refundables, [False, True, False, True])
        # Cancellation phrase preserved verbatim for refundability_state()
        self.assertEqual(suite["rate_plans"][0]["cancellation_phrase"], "Non-Refundable")
        self.assertTrue(
            suite["rate_plans"][1]["cancellation_phrase"]
            .lower().startswith("fully refundable")
        )
        # Stay total = nightly × nights (bare; downstream consumers don't
        # rely on fees/taxes here).
        self.assertEqual(suite["rate_plans"][1]["total_stay_usd"], (348 + 163) * 3)

        # Penthouse plans
        penthouse = new_extracted["rooms"][1]
        self.assertEqual(len(penthouse["rate_plans"]), 2)
        self.assertEqual(penthouse["rate_plans"][0]["rate_per_night_usd"], 832)
        self.assertEqual(penthouse["rate_plans"][1]["rate_per_night_usd"], 832 + 393)

    def test_passthrough_for_room_not_in_markdown(self):
        from hotels_markdown import expand_tiers_from_markdown
        original_plans = [
            {"rate_plan_label": "Some plan", "rate_per_night_usd": 100, "refundable": True}
        ]
        extracted = {
            "rooms": [
                {"marketing_name": "Phantom Room", "rate_plans": original_plans},
            ],
        }
        new_extracted, diag = expand_tiers_from_markdown(extracted, SAMPLE_MARKDOWN)
        self.assertEqual(diag["rooms_replaced"], 0)
        self.assertEqual(diag["rooms_passthrough"], 1)
        # Plans untouched (same identity)
        self.assertEqual(new_extracted["rooms"][0]["rate_plans"], original_plans)

    def test_passthrough_when_markdown_has_no_cancellation_block(self):
        from hotels_markdown import expand_tiers_from_markdown
        thin_md = (
            "### View all photos for Suite, 1 Bedroom (Platinum)\n"
            "![p](http://e/p.jpg)\n"
            "### Suite, 1 Bedroom (Platinum)\n"
            "- 820 sq ft\n"
            "$348 nightly\n"
        )
        extracted = {
            "rooms": [
                {"marketing_name": "Suite, 1 Bedroom (Platinum)",
                 "rate_plans": [
                     {"rate_plan_label": "BAR",
                      "rate_per_night_usd": 348, "refundable": False},
                 ]},
            ],
        }
        new_extracted, diag = expand_tiers_from_markdown(extracted, thin_md)
        self.assertEqual(diag["rooms_replaced"], 0)
        self.assertEqual(diag["rooms_passthrough"], 1)
        self.assertEqual(new_extracted["rooms"][0]["rate_plans"][0]["rate_plan_label"], "BAR")

    def test_idempotent_under_repeat(self):
        from hotels_markdown import expand_tiers_from_markdown
        extracted = {
            "nights": 3,
            "rooms": [
                {"marketing_name": "Suite, 1 Bedroom (Platinum)", "rate_plans": []},
            ],
        }
        once, _ = expand_tiers_from_markdown(extracted, SAMPLE_MARKDOWN)
        twice, _ = expand_tiers_from_markdown(once, SAMPLE_MARKDOWN)
        self.assertEqual(once["rooms"][0]["rate_plans"],
                         twice["rooms"][0]["rate_plans"])

    def test_handles_missing_nights_field(self):
        from hotels_markdown import expand_tiers_from_markdown
        # extracted has no `nights` — fall back to LOS=1 for stay-total math.
        extracted = {"rooms": [{"marketing_name": "Suite, 1 Bedroom (Platinum)",
                                 "rate_plans": []}]}
        new_extracted, diag = expand_tiers_from_markdown(extracted, SAMPLE_MARKDOWN)
        self.assertEqual(diag["rooms_replaced"], 1)
        # Stay-total equals nightly when nights=1 fallback applies.
        first = new_extracted["rooms"][0]["rate_plans"][0]
        self.assertEqual(first["total_stay_usd"], first["rate_per_night_usd"])


class TestRefundabilityCompatibilityWithPicker(unittest.TestCase):
    """The replaced rate_plans must satisfy the canonical picker contracts:
    pick_canonical_bar / pick_canonical_flex tag tri-state via
    refundability_state(plan), which reads cancellation_phrase first then
    rate_plan_label. Tier rows we emit must hit the explicit phrase tokens.
    """

    def test_picker_finds_bar_non_ref_and_bar_flex(self):
        from hotels_markdown import expand_tiers_from_markdown
        from normalize import pick_canonical_bar, pick_canonical_flex
        extracted = {
            "nights": 3,
            "rooms": [{"marketing_name": "Suite, 1 Bedroom (Platinum)",
                       "rate_plans": []}],
        }
        new_extracted, _ = expand_tiers_from_markdown(extracted, SAMPLE_MARKDOWN)
        plans = new_extracted["rooms"][0]["rate_plans"]
        bar = pick_canonical_bar(plans)
        flex = pick_canonical_flex(plans)
        self.assertIsNotNone(bar, "BAR_NON_REF picker must find a non-ref tier")
        self.assertIsNotNone(flex, "BAR_FLEX picker must find a refundable tier")
        # BAR_NON_REF should be the cheapest non-ref (Non-Refundable + $0 = 348).
        self.assertEqual(bar[1]["rate_per_night_usd"], 348)
        # BAR_FLEX should be the cheapest refundable (Fully ref + $163 = 511).
        self.assertEqual(flex[1]["rate_per_night_usd"], 348 + 163)


if __name__ == "__main__":
    unittest.main()
