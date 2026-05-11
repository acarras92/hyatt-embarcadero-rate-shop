from __future__ import annotations

import unittest
from unittest.mock import Mock, patch


class TestEmptyInventoryClassification(unittest.TestCase):
    def test_booking_no_inventory_with_expected_property_is_not_broken(self):
        from validators import classify_empty_inventory_page

        md = (
            "## The Hay - Adams\n"
            "We have no availability here between Wed, Sep 16, 2026 and Thu, Sep 17, 2026.\n"
            "Deluxe Room Not available on our site for your dates"
        )
        verdict = classify_empty_inventory_page(
            md, expected_property_tokens=["hay-adams", "800 16th"]
        )
        self.assertEqual(verdict, "no_inventory")

    def test_no_inventory_copy_without_property_identity_is_broken_url(self):
        from validators import classify_empty_inventory_page

        md = "Unknown Hotel. We have no availability here between these dates."
        verdict = classify_empty_inventory_page(
            md, expected_property_tokens=["white house", "1710 h street"]
        )
        self.assertEqual(verdict, "FAIL_URL_BROKEN")

    def test_broken_page_signature_wins(self):
        from validators import classify_empty_inventory_page

        verdict = classify_empty_inventory_page(
            "404 Not Found - hotel not found",
            expected_property_tokens=["white house"],
        )
        self.assertEqual(verdict, "FAIL_URL_BROKEN")


class TestNoInventoryResumeSentinel(unittest.TestCase):
    def test_no_inventory_row_marks_cell_done(self):
        from pathlib import Path
        import scrape

        csv_path = Path("tests/fixtures/no_inventory_completed.csv")
        original = scrape.RAW_CSV
        scrape.RAW_CSV = csv_path
        try:
            done = scrape.load_completed_cells()
        finally:
            scrape.RAW_CSV = original
        self.assertIn(("hay_adams", "booking", "2026-09-16", 1), done)



class TestAkaPenthouseClassification(unittest.TestCase):
    def test_one_and_two_bedroom_penthouses_keep_PH_with_variant_split(self):
        # internal_code stays "PH" so build_dashboard.ROOM_ORDER and the
        # PH-bucket aggregations keep working; penthouse_variant carries
        # the 1BR/2BR distinction for finer-grained analyses.
        from scrape import classify_room

        one = classify_room("One Bedroom Terrace Penthouse", "aka_white_house")
        two = classify_room("Two Bedroom Terrace Penthouse", "aka_white_house")
        self.assertEqual(one["internal_code"], "PH")
        self.assertEqual(two["internal_code"], "PH")
        self.assertEqual(one["penthouse_variant"], "1BR")
        self.assertEqual(two["penthouse_variant"], "2BR")


class TestSynxisParsing(unittest.TestCase):
    def _mock_session(self, response_payload):
        session = Mock()
        response = Mock()
        response.status_code = 200
        response.headers = {"content-type": "application/json"}
        response.json.return_value = response_payload
        response.text = "{}"
        session.post.return_value = response
        return session

    @patch("synxis_api._ensure_session")
    def test_product_availability_list_parses_all_detail_blocks_and_skips_unavailable(self, ensure):
        import synxis_api

        payload = {
            "ApplicationResults": {},
            "ContentLists": {
                "RoomList": [
                    {"Code": "1BDP", "Name": "One Bedroom Platinum Suite"},
                    {"Code": "2PH", "Name": "Two Bedroom Terrace Penthouse"},
                ],
                "RateList": [
                    {"Code": "NREFd", "Name": "Pay Now and Save", "CategoryCode": "NRF"},
                    {"Code": "BFRd", "Name": "Best Flexible Rate", "CategoryCode": "BAR"},
                ],
            },
            "ProductAvailabilityList": [
                {"Prices": [
                    {"Available": True, "Product": {
                        "Room": {"Code": "1BDP"}, "Rate": {"Code": "NREFd"},
                        "Prices": {"Total": {"Price": {"Total": {"AmountWithFees": 300.0}}}},
                    }},
                    {"Available": False, "Product": {
                        "Room": {"Code": "1BDP"}, "Rate": {"Code": "BFRd"},
                        "Prices": {"Total": {"Price": {"Total": {"AmountWithFees": 999.0}}}},
                    }},
                ]},
                {"Prices": [
                    {"Available": True, "Product": {
                        "Room": {"Code": "2PH"}, "Rate": {"Code": "NREFd"},
                        "Prices": {"Total": {"Price": {"Total": {"AmountWithFees": 1200.0}}}},
                    }},
                ]},
            ],
        }
        ensure.return_value = self._mock_session(payload)
        synxis_api._api_key_cache["https://reservations.example"] = "ApiKey token"

        result = synxis_api.fetch_synxis_direct(
            "2026-07-02", 1,
            base_url="https://reservations.example",
            hotel_id="56224",
            chain_id="27508",
        )
        self.assertTrue(result["success"], result.get("error"))
        rooms = result["data"]["json"]["rooms"]
        by_name = {r["marketing_name"]: r for r in rooms}
        self.assertIn("One Bedroom Platinum Suite", by_name)
        self.assertIn("Two Bedroom Terrace Penthouse", by_name)
        one_plans = by_name["One Bedroom Platinum Suite"]["rate_plans"]
        self.assertEqual(len(one_plans), 1, "unavailable BFRd row must not persist")
        self.assertEqual(one_plans[0]["rate_per_night_usd"], 300.0)
        two_plans = by_name["Two Bedroom Terrace Penthouse"]["rate_plans"]
        self.assertEqual(two_plans[0]["rate_per_night_usd"], 1200.0)


if __name__ == "__main__":
    unittest.main()
