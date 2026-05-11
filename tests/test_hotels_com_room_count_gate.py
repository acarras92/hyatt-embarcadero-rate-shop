"""Phase A2.4 #7 — Hotels.com gate-4 retry on room-count mismatch.

Verification step (per brief):
  Unit test with synthetic extraction returning 1 room, 3 tiers. Assert:
  gate-4 retry triggers because room count < 5.
"""
from __future__ import annotations
import os
import unittest

os.environ.setdefault("FIRECRAWL_API_KEY", "test-key-not-used")


class TestShouldTriggerHotelsRetry(unittest.TestCase):
    def test_one_room_three_tiers_does_NOT_inner_retry_in_phase_a2_5b(self):
        # Phase A2.5b intentionally narrows the room-count retry trigger:
        # the inner-loop retry now fires only when min_plans_per_room < 2
        # (some rendered room is thin). The Phase A2.4 #7 "1 room × 3 tiers"
        # case still gets recovered via detect_room_count_short / FAIL_ROOM_COUNT_SHORT
        # sentinel on the next run — but no longer risks the inner retry's
        # bot-block path overwriting good data.
        from scrape import should_trigger_hotels_com_retry, EXPECTED_ROOM_COUNT_BY_CHANNEL
        expected = EXPECTED_ROOM_COUNT_BY_CHANNEL["hotels_com"]
        trigger, _ = should_trigger_hotels_com_retry(
            channel="hotels_com",
            n_rooms=1,
            max_plans_per_room=3,
            min_plans_per_room=3,
            expected_room_count=expected,
        )
        self.assertFalse(
            trigger,
            "1 room × 3 full tiers — inner retry must NOT fire in Phase A2.5b. "
            "Recovery path is the post-loop FAIL_ROOM_COUNT_SHORT sentinel."
        )

    def test_one_room_thin_tiers_DOES_trigger_via_min_plans(self):
        # Cell where 1 room rendered with only 1 plan (lead-in only) and
        # 4 rooms didn't render at all → min=1, n_rooms=1 < expected. Retry.
        from scrape import should_trigger_hotels_com_retry, EXPECTED_ROOM_COUNT_BY_CHANNEL
        expected = EXPECTED_ROOM_COUNT_BY_CHANNEL["hotels_com"]
        trigger, reason = should_trigger_hotels_com_retry(
            channel="hotels_com",
            n_rooms=1,
            max_plans_per_room=1,
            min_plans_per_room=1,
            expected_room_count=expected,
        )
        self.assertTrue(trigger)
        # Either gate (max<2 or room-count short with min<2) can fire.
        self.assertIn("plans", reason.lower())

    def test_full_render_does_not_trigger(self):
        from scrape import should_trigger_hotels_com_retry
        trigger, _ = should_trigger_hotels_com_retry(
            channel="hotels_com",
            n_rooms=5,
            max_plans_per_room=4,
            expected_room_count=5,
        )
        self.assertFalse(trigger)

    def test_max_plans_gate_still_fires_on_thin_render(self):
        # Legacy gate-4: when no room expanded at all (max < 2), still trigger.
        from scrape import should_trigger_hotels_com_retry
        trigger, reason = should_trigger_hotels_com_retry(
            channel="hotels_com",
            n_rooms=5,
            max_plans_per_room=1,
            expected_room_count=5,
        )
        self.assertTrue(trigger)
        self.assertIn("max=1", reason)

    def test_other_channels_never_trigger(self):
        from scrape import should_trigger_hotels_com_retry
        for ch in ("direct", "booking", "expedia"):
            trigger, _ = should_trigger_hotels_com_retry(
                channel=ch, n_rooms=0, max_plans_per_room=0,
                expected_room_count=5,
            )
            self.assertFalse(trigger,
                             f"channel {ch} should never trigger Hotels.com retry")

    def test_expected_zero_disables_room_count_gate(self):
        # When config has no expected count, room-count gate is suppressed.
        from scrape import should_trigger_hotels_com_retry
        trigger, _ = should_trigger_hotels_com_retry(
            channel="hotels_com",
            n_rooms=1,
            max_plans_per_room=4,
            expected_room_count=0,
        )
        self.assertFalse(trigger,
                         "with expected=0 and max_plans>=2, no retry should fire")

    def test_default_constant_has_aka_canonical_counts(self):
        # AKA canonical counts: direct/booking = 5; hotels_com = 6 (Hotels.com
        # surfaces the Mobility-Accessible 1BR variant as its own room card).
        from scrape import EXPECTED_ROOM_COUNT_BY_CHANNEL
        self.assertEqual(EXPECTED_ROOM_COUNT_BY_CHANNEL["hotels_com"], 6)
        self.assertEqual(EXPECTED_ROOM_COUNT_BY_CHANNEL["direct"], 5)
        self.assertEqual(EXPECTED_ROOM_COUNT_BY_CHANNEL["booking"], 5)

    def test_six_rooms_does_not_trigger_hotels_com_retry(self):
        # Phase A2.5b: full render returns 6 rooms; gate-4 must NOT fire.
        from scrape import should_trigger_hotels_com_retry, EXPECTED_ROOM_COUNT_BY_CHANNEL
        expected = EXPECTED_ROOM_COUNT_BY_CHANNEL["hotels_com"]
        trigger, reason = should_trigger_hotels_com_retry(
            channel="hotels_com",
            n_rooms=6,
            max_plans_per_room=4,
            expected_room_count=expected,
        )
        self.assertFalse(
            trigger,
            f"6 rooms with 4 tiers must NOT trigger retry; got reason={reason!r}",
        )

    def test_five_rooms_with_full_tiers_does_NOT_trigger(self):
        # Phase A2.5b regression: live smoke 2026-04-27 returned 5 rooms ×
        # 4 tiers each on a date where Hotels.com only had 5 SKUs available.
        # The retry then bot-blocked and overwrote a perfectly good extract.
        # Now: full tiers (min>=2, max>=4) means the page rendered cleanly
        # and the lower count is real availability — NO retry.
        from scrape import should_trigger_hotels_com_retry, EXPECTED_ROOM_COUNT_BY_CHANNEL
        expected = EXPECTED_ROOM_COUNT_BY_CHANNEL["hotels_com"]
        trigger, reason = should_trigger_hotels_com_retry(
            channel="hotels_com",
            n_rooms=5,
            max_plans_per_room=4,
            min_plans_per_room=2,  # Penthouse legitimately has 2 tiers
            expected_room_count=expected,
        )
        self.assertFalse(
            trigger,
            f"5 rooms with full tier rendering must NOT retry — that overwrites "
            f"a good extract. Got reason={reason!r}"
        )

    def test_five_rooms_with_thin_tiers_still_triggers(self):
        # Phase A2.5b: 5/6 rooms with min<2 (some room got 0 or 1 plan) IS
        # a render bug worth retrying.
        from scrape import should_trigger_hotels_com_retry, EXPECTED_ROOM_COUNT_BY_CHANNEL
        expected = EXPECTED_ROOM_COUNT_BY_CHANNEL["hotels_com"]
        trigger, reason = should_trigger_hotels_com_retry(
            channel="hotels_com",
            n_rooms=5,
            max_plans_per_room=2,
            min_plans_per_room=1,
            expected_room_count=expected,
        )
        self.assertTrue(trigger)
        self.assertIn(f"of expected {expected}", reason)


class TestDetectRoomCountShort(unittest.TestCase):
    """Phase A2.4b #2 — post-retry-exhaustion detector for room-count gate.

    Pure function over (extracted, channel) → (is_short, n, expected). No
    Firecrawl side effects, so we can test directly without monkey-patching.
    """

    # Codex item 1 (2026-04-28): the gate is AKA-only, so all of these tests
    # pin property_id="aka_white_house" to exercise the active path.

    def test_two_rooms_against_expected_six_is_short(self):
        from scrape import detect_room_count_short
        extracted = {"rooms": [
            {"marketing_name": "One Bedroom Platinum Suite", "rate_plans": []},
            {"marketing_name": "Studio Suite", "rate_plans": []},
        ]}
        is_short, n, expected = detect_room_count_short(
            extracted, channel="hotels_com", property_id="aka_white_house",
        )
        self.assertTrue(is_short)
        self.assertEqual(n, 2)
        self.assertEqual(expected, 6)

    def test_full_room_count_is_not_short(self):
        # Hotels.com expected = 6 (Phase A2.5b).
        from scrape import detect_room_count_short
        extracted = {"rooms": [{"marketing_name": f"R{i}", "rate_plans": []} for i in range(6)]}
        is_short, n, expected = detect_room_count_short(
            extracted, channel="hotels_com", property_id="aka_white_house",
        )
        self.assertFalse(is_short)
        self.assertEqual(n, 6)

    def test_unknown_channel_disables_gate(self):
        from scrape import detect_room_count_short
        extracted = {"rooms": [{"marketing_name": "R0", "rate_plans": []}]}
        is_short, n, expected = detect_room_count_short(
            extracted, channel="airbnb", property_id="aka_white_house",
        )
        self.assertFalse(is_short, "channels with no expected count have the gate disabled")
        self.assertEqual(expected, 0)

    def test_non_aka_property_disables_gate(self):
        # Codex item 1 (2026-04-28): comp properties have different SKU counts
        # than AKA's 5/6. Applying the AKA expected count to comps caused
        # spurious FAIL_ROOM_COUNT_SHORT on every comp Booking/Direct cell.
        from scrape import detect_room_count_short
        extracted = {"rooms": [{"marketing_name": "King Room", "rate_plans": []}]}
        is_short, n, expected = detect_room_count_short(
            extracted, channel="booking", property_id="capital_hilton",
        )
        self.assertFalse(is_short, "non-AKA properties bypass the room-count gate")
        self.assertEqual(n, 1)
        self.assertEqual(expected, 0)

    def test_none_extracted_is_not_short(self):
        # Defensive: if upstream returned no extracted dict, the gate is skipped
        # so other failure paths (extract_failed) own the verdict.
        from scrape import detect_room_count_short
        is_short, _, _ = detect_room_count_short(
            None, channel="hotels_com", property_id="aka_white_house",
        )
        self.assertFalse(is_short)

    def test_page_declared_inventory_overrides_canonical_max(self):
        # Phase A2.5b: when Hotels.com markdown declares "Showing 5 of 5 rooms",
        # the cell rendered the full inventory for THIS check-in date, even
        # though the canonical-max EXPECTED_ROOM_COUNT_BY_CHANNEL says 6.
        # Live smoke 2026-04-27 hit this on 2026-07-13: page actually has 5
        # rooms available; sentinel was firing falsely.
        from scrape import detect_room_count_short
        extracted = {"rooms": [{"marketing_name": f"R{i}", "rate_plans": []} for i in range(5)]}
        markdown = "...some HTML... Showing 5 of 5 rooms ..."
        is_short, n, expected = detect_room_count_short(
            extracted, channel="hotels_com", markdown=markdown,
            property_id="aka_white_house",
        )
        self.assertFalse(
            is_short,
            "5 rooms when page declares 5 of 5 must NOT fire FAIL_ROOM_COUNT_SHORT"
        )
        self.assertEqual(n, 5)
        self.assertEqual(expected, 5,
                         "effective_expected should reflect page-declared inventory")

    def test_page_declared_inventory_gap_still_triggers(self):
        # Page says "Showing 4 of 6 rooms" — extraction got 4, but page
        # itself confirms 6 exist; we're missing 2. Trigger sentinel.
        from scrape import detect_room_count_short
        extracted = {"rooms": [{"marketing_name": f"R{i}", "rate_plans": []} for i in range(4)]}
        markdown = "Showing 4 of 6 rooms"
        is_short, n, expected = detect_room_count_short(
            extracted, channel="hotels_com", markdown=markdown,
            property_id="aka_white_house",
        )
        self.assertTrue(is_short)
        self.assertEqual(n, 4)
        self.assertEqual(expected, 6)

    def test_declaration_only_used_for_hotels_com(self):
        # Other channels don't have this marker; fall back to canonical max.
        from scrape import detect_room_count_short
        extracted = {"rooms": [{"marketing_name": f"R{i}", "rate_plans": []} for i in range(4)]}
        markdown = "Showing 4 of 4 rooms"  # would override IF used
        is_short, _, expected = detect_room_count_short(
            extracted, channel="direct", markdown=markdown,
            property_id="aka_white_house",
        )
        # Direct channel canonical = 5 → 4 < 5 → still short.
        self.assertTrue(is_short)
        self.assertEqual(expected, 5,
                         "non-hotels_com channels ignore the declaration marker")


class TestRoomCountShortSentinelAndResume(unittest.TestCase):
    """Phase A2.4b #2 — verification step (per brief):

    Hotels.com cell where both initial extraction and gate-4 retry return 2
    rooms (below threshold of 5). Assert:
      (a) no plan rows written (only the sentinel),
      (b) a FAIL_ROOM_COUNT_SHORT row appears in the output,
      (c) load_completed_cells() does not treat the cell as complete.
    """

    def test_short_render_writes_only_sentinel_and_re_queues(self):
        import csv
        import tempfile
        from pathlib import Path
        import scrape

        # Step 1: simulate the post-retry detection helper finding short.
        # 2 rooms (each with full plan ladders — proves we're not gating
        # on plans, only on room count) when expected is 6.
        extracted = {
            "property_name": "AKA White House",
            "rooms": [
                {"marketing_name": "One Bedroom Platinum Suite", "rate_plans": [
                    {"rate_plan_label": "Non-refundable", "rate_per_night_usd": 295.0,
                     "cancellation_phrase": "Non-refundable", "refundable": False},
                    {"rate_plan_label": "Free cancellation", "rate_per_night_usd": 320.0,
                     "cancellation_phrase": "Free cancellation", "refundable": True},
                ]},
                {"marketing_name": "Studio Suite", "rate_plans": [
                    {"rate_plan_label": "Non-refundable", "rate_per_night_usd": 245.0,
                     "cancellation_phrase": "Non-refundable", "refundable": False},
                    {"rate_plan_label": "Free cancellation", "rate_per_night_usd": 270.0,
                     "cancellation_phrase": "Free cancellation", "refundable": True},
                ]},
            ],
        }
        is_short, n, expected = scrape.detect_room_count_short(
            extracted, channel="hotels_com", property_id="aka_white_house",
        )
        self.assertTrue(is_short, "2 rooms < expected 6 must be flagged short")

        # Step 2: build the cell-level sentinel and assert it has no rate fields.
        sentinel = scrape._make_room_count_short_sentinel(
            property_id="aka_white_house", channel_id="hotels_com",
            arrival="2026-07-13", los=1, source_url="https://example/",
            markdown="", n_rooms=n, expected_n=expected,
        )
        self.assertEqual(sentinel["status"], "FAIL_ROOM_COUNT_SHORT")
        self.assertEqual(sentinel["rate_plan_canonical"], "FAIL_ROOM_COUNT_SHORT")
        self.assertIsNone(sentinel["rate_per_night_usd"])
        self.assertIsNone(sentinel["total_stay_usd"])
        self.assertEqual(sentinel["scraped_marketing_name"], "",
                         "cell-level sentinel carries no room identity")
        # FAIL_ROOM_COUNT_SHORT must be in SENTINEL_STATUSES (used by the
        # resume gate). Without this, the cell would never be re-queued.
        self.assertIn("FAIL_ROOM_COUNT_SHORT", scrape.SENTINEL_STATUSES)

        # Step 3: write only the sentinel to a temp CSV (no plan rows) and
        # verify load_completed_cells does NOT include the cell.
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "raw_rates.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["property_id", "channel",
                                                  "arrival_date", "nights",
                                                  "status"])
                w.writeheader()
                w.writerow({"property_id": "aka_white_house", "channel": "hotels_com",
                            "arrival_date": "2026-07-13", "nights": 1,
                            "status": "FAIL_ROOM_COUNT_SHORT"})
            original = scrape.RAW_CSV
            scrape.RAW_CSV = csv_path
            try:
                done = scrape.load_completed_cells()
            finally:
                scrape.RAW_CSV = original
        self.assertNotIn(("aka_white_house", "hotels_com", "2026-07-13", 1), done,
                         "FAIL_ROOM_COUNT_SHORT must re-queue, not mark done")


if __name__ == "__main__":
    unittest.main()
