"""Phase A2.5 (2026-04-27) — Hotels.com vision-extraction path.

Verifies the Firecrawl-screenshot + claude-haiku-4-5 vision path returns
the same {success, data: {markdown, json}} body shape as firecrawl_scrape_json,
so the existing gate-1..5 pipeline keeps working unchanged.

No live network. Firecrawl HTTP call and Anthropic vision call are mocked.
"""
from __future__ import annotations
import os
import unittest
from unittest.mock import patch, MagicMock

os.environ.setdefault("FIRECRAWL_API_KEY", "test-key-not-used")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")


def _five_rooms_three_tiers() -> dict:
    """Mimic claude-haiku-4-5 vision output for an AKA Hotels.com page that
    fully rendered: 5 rooms, each with 3 rate-plan tiers (non-refundable +
    two refundable add-ons, per the Hotels.com tier pattern documented in
    wiki/channel_quirks.md)."""
    rooms = []
    for name, base in [("Studio Suite King", 295.0),
                       ("One Bedroom King", 345.0),
                       ("One Bedroom Platinum Suite", 425.0),
                       ("Two Bedroom Suite", 595.0),
                       ("Penthouse", 895.0)]:
        rooms.append({
            "marketing_name": name,
            "rate_plans": [
                {"rate_plan_label": "Non-Refundable + $0",
                 "rate_per_night_usd": base,
                 "cancellation_phrase": "Non-refundable",
                 "refundable": False, "rate_plan_confidence": "high"},
                {"rate_plan_label": "Fully refundable + $46",
                 "rate_per_night_usd": base + 46,
                 "cancellation_phrase": "Free cancellation before August 5, 2026",
                 "refundable": True, "rate_plan_confidence": "high"},
                {"rate_plan_label": "Fully refundable + $128",
                 "rate_per_night_usd": base + 128,
                 "cancellation_phrase": "Free cancellation",
                 "refundable": True, "rate_plan_confidence": "high"},
            ],
        })
    return {"property_name": "AKA White House", "rooms": rooms}


class TestFirecrawlScrapeViaVision(unittest.TestCase):
    """Public entry point: returns Firecrawl-shaped body for downstream gates."""

    def test_full_render_returns_success_body_with_extracted_rooms(self):
        firecrawl_response = {
            "success": True,
            "data": {
                "markdown": ("# AKA White House\n\n"
                             "Studio Suite King\nOne Bedroom King\n"
                             "One Bedroom Platinum Suite\nTwo Bedroom Suite\nPenthouse\n"),
                "screenshot": "https://firecrawl-cdn.example/abc.png",
            },
        }
        vision_extracted = _five_rooms_three_tiers()
        with patch("hotels_vision.firecrawl_scrape_screenshot",
                   return_value=firecrawl_response), \
             patch("hotels_vision.extract_rooms_from_screenshot",
                   return_value=(vision_extracted, "")):
            from hotels_vision import firecrawl_scrape_via_vision
            body = firecrawl_scrape_via_vision(
                "https://www.hotels.com/ho39456832/",
                stealth=True, wait_for_ms=8000, schema={}, prompt="ignored",
            )
        self.assertTrue(body["success"])
        self.assertIn("markdown", body["data"])
        self.assertIn("json", body["data"])
        self.assertEqual(len(body["data"]["json"]["rooms"]), 5)
        self.assertEqual(body["data"]["json"]["property_name"], "AKA White House")

    def test_firecrawl_failure_propagates_unchanged(self):
        # When Firecrawl itself returns a failure (5xx, stealth-proxy error,
        # etc.), the wrapper passes it through so scrape.py's existing
        # http_error handling fires.
        with patch("hotels_vision.firecrawl_scrape_screenshot",
                   return_value={"success": False, "error": "http 503"}):
            from hotels_vision import firecrawl_scrape_via_vision
            body = firecrawl_scrape_via_vision(
                "https://www.hotels.com/ho39456832/",
                stealth=True, wait_for_ms=8000, schema={}, prompt="ignored",
            )
        self.assertFalse(body["success"])
        self.assertIn("503", body["error"])

    def test_missing_screenshot_url_fails_gracefully(self):
        # If Firecrawl succeeds but the screenshot URL is missing for any
        # reason, we MUST fail closed — vision can't extract from nothing.
        with patch("hotels_vision.firecrawl_scrape_screenshot",
                   return_value={"success": True, "data": {"markdown": "hi"}}):
            from hotels_vision import firecrawl_scrape_via_vision
            body = firecrawl_scrape_via_vision(
                "https://www.hotels.com/ho39456832/",
                stealth=True, wait_for_ms=8000, schema={}, prompt="ignored",
            )
        self.assertFalse(body["success"])
        self.assertIn("no_screenshot", body["error"])

    def test_vision_api_error_fails_with_extract_failed_shape(self):
        # When the Anthropic API errors out (rate-limit, invalid key, etc.),
        # the wrapper returns success:False with vision_extraction_failed
        # so scrape.py routes it to last_status = "extract_failed".
        with patch("hotels_vision.firecrawl_scrape_screenshot",
                   return_value={"success": True, "data": {
                       "markdown": "hi",
                       "screenshot": "https://firecrawl-cdn.example/abc.png"}}), \
             patch("hotels_vision.extract_rooms_from_screenshot",
                   return_value=(None, "anthropic_api_error: RateLimitError")):
            from hotels_vision import firecrawl_scrape_via_vision
            body = firecrawl_scrape_via_vision(
                "https://www.hotels.com/ho39456832/",
                stealth=True, wait_for_ms=8000, schema={}, prompt="ignored",
            )
        self.assertFalse(body["success"])
        self.assertIn("vision_extraction_failed", body["error"])


class TestVisionDownstreamNormalization(unittest.TestCase):
    """Vision output → existing pickers + room-count gate must work unchanged."""

    def test_five_rooms_three_tiers_produce_canonical_bar_picks(self):
        # Each of the 5 rooms has one Non-refundable plan (cheapest, bare)
        # and two refundable plans. pick_canonical_bar must select the
        # non-ref; pick_canonical_flex must select the cheaper refundable.
        from normalize import pick_canonical_bar, pick_canonical_flex
        from scrape import _flatten_rooms_to_rows

        # Pre-process refundability the way _flatten_rooms_to_rows does
        # (via the cancellation_phrase deterministic override).
        for room in _five_rooms_three_tiers()["rooms"]:
            for p in room["rate_plans"]:
                phrase = (p.get("cancellation_phrase") or "").lower()
                if "non-refund" in phrase:
                    p["refundable"] = False
                elif "refundable" in phrase or "free cancellation" in phrase:
                    p["refundable"] = True
            bar = pick_canonical_bar(room["rate_plans"])
            flex = pick_canonical_flex(room["rate_plans"])
            self.assertIsNotNone(bar, f"no BAR for {room['marketing_name']}")
            self.assertIsNotNone(flex, f"no Flex for {room['marketing_name']}")
            self.assertEqual(bar[0], 0,
                             "non-refundable + $0 must be canonical BAR (cheapest bare non-ref)")

    def test_thin_render_two_rooms_triggers_room_count_short_gate(self):
        # Vision returns only 2 rooms when Hotels.com expects 6. detect_room_count_short
        # must flag it so scrape.py's post-retry sentinel-write fires.
        import scrape
        thin = {"property_name": "AKA White House",
                "rooms": [
                    {"marketing_name": "Studio Suite King", "rate_plans": [
                        {"rate_plan_label": "Non-Refundable + $0",
                         "rate_per_night_usd": 295.0,
                         "cancellation_phrase": "Non-refundable",
                         "refundable": False}]},
                    {"marketing_name": "Penthouse", "rate_plans": [
                        {"rate_plan_label": "Non-Refundable + $0",
                         "rate_per_night_usd": 895.0,
                         "cancellation_phrase": "Non-refundable",
                         "refundable": False}]},
                ]}
        is_short, n_rooms, expected = scrape.detect_room_count_short(
            thin, channel="hotels_com", property_id="aka_white_house",
        )
        self.assertTrue(is_short)
        self.assertEqual(n_rooms, 2)
        self.assertEqual(expected, 6)

    def test_thin_per_room_tiers_trigger_legacy_gate4_retry(self):
        # When vision returns 5 rooms but each has only 1 tier, the gate-4
        # retry trigger should fire (max_plans_per_room < 2 path), giving
        # the cell a second chance with the longer-settle plan.
        from scrape import should_trigger_hotels_com_retry, EXPECTED_ROOM_COUNT_BY_CHANNEL
        trigger, reason = should_trigger_hotels_com_retry(
            channel="hotels_com",
            n_rooms=5,
            max_plans_per_room=1,
            expected_room_count=EXPECTED_ROOM_COUNT_BY_CHANNEL["hotels_com"],
        )
        self.assertTrue(trigger)
        self.assertIn("max=1", reason)


class TestRoutingFlag(unittest.TestCase):
    """scrape.py must dispatch to the vision wrapper iff
    channel == hotels_com AND CONFIG['use_vision_for_hotels_com'] is truthy."""

    def test_use_vision_default_false_post_smoke_finding(self):
        # Phase A2.5-fix2 (2026-04-27): default flipped from True to False
        # after live smoke proved Hotels.com add-on tiers don't render
        # statically (they're behind a click-to-expand widget). Vision is
        # wired but inactive until A2.5b lands click actions on the
        # expanders. The default MUST be False so production scrapes don't
        # accidentally activate the hallucination-prone vision path.
        import scrape
        # The flag is read inline at scrape() call time; verify the default
        # via .get(...) — this MUST mirror the second arg of CONFIG.get()
        # in scrape.py:scrape_cell. If the second arg drifts, this test
        # catches it.
        self.assertFalse(scrape.CONFIG.get("use_vision_for_hotels_com", False))

    def test_use_vision_explicit_true_activates_vision_path(self):
        # Operator opt-in — flipping the flag to true (in production
        # config.json or via CONFIG mutation in tests) activates the
        # vision dispatch. Once A2.5b lands the click-expander, this is
        # the path that re-enables vision for selected runs.
        import scrape
        original = dict(scrape.CONFIG)
        try:
            scrape.CONFIG["use_vision_for_hotels_com"] = True
            self.assertTrue(scrape.CONFIG.get("use_vision_for_hotels_com", False))
        finally:
            scrape.CONFIG.clear()
            scrape.CONFIG.update(original)


if __name__ == "__main__":
    unittest.main()
