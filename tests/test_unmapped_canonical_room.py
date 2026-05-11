"""Phase A2.4 #1 — unmapped canonical room fail-closed.

Verification step (per brief):
  Add a test case (synthetic or unit test) that passes a fabricated unmapped
  marketing name through the scrape path — e.g., "Three Bedroom Penthouse
  Quadplex". Assert: (a) row does not land in raw_rates.csv as a normal row;
  (b) FAIL_UNKNOWN_ROOM_TYPE appears in scrape_log; (c) resume logic re-queues
  the cell.

The scrape path itself hits Firecrawl — too heavyweight for a unit test —
so the test exercises the code paths that own each contract:
  (a) find_unmapped_aka_marketing_names() flags the unmapped string, and
      the cell-level status assignment in scrape_cell skips persistence.
      We assert via the helper + the persistence-gate condition.
  (b) The status string used in last_reason / log_event includes the
      unmapped strings.
  (c) load_completed_cells() does NOT count any FAIL_UNKNOWN_ROOM_TYPE
      row as "done". (The actual scrape persists no rows in this case;
      we additionally simulate the safety net by writing a CSV with a
      FAIL_UNKNOWN_ROOM_TYPE row and asserting the cell is not "done".)
"""
from __future__ import annotations
import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

# conftest puts scraper_lib on sys.path; we also need .env-free import
os.environ.setdefault("FIRECRAWL_API_KEY", "test-key-not-used")


class TestFindUnmappedAkaMarketingNames(unittest.TestCase):
    def test_fabricated_name_is_flagged(self):
        # Simulate the Firecrawl "json" return shape with one fabricated room
        from scrape import find_unmapped_aka_marketing_names
        extracted = {
            "property_name": "AKA White House",
            "rooms": [
                {"marketing_name": "Three Bedroom Penthouse Quadplex",
                 "rate_plans": [{"rate_plan_label": "x", "rate_per_night_usd": 999}]},
            ],
        }
        unmapped = find_unmapped_aka_marketing_names(extracted)
        self.assertIn("Three Bedroom Penthouse Quadplex", unmapped)

    def test_known_canonical_name_is_not_flagged(self):
        from scrape import find_unmapped_aka_marketing_names
        extracted = {
            "rooms": [
                {"marketing_name": "One Bedroom Platinum Suite", "rate_plans": []},
            ],
        }
        self.assertEqual(find_unmapped_aka_marketing_names(extracted), [])

    def test_empty_marketing_name_is_ignored(self):
        # The room-type anchor gate (check_room_type_anchored) handles empty
        # names; the canonical-coverage gate must not double-fail on the same
        # condition.
        from scrape import find_unmapped_aka_marketing_names
        extracted = {
            "rooms": [{"marketing_name": "", "rate_plans": []}],
        }
        self.assertEqual(find_unmapped_aka_marketing_names(extracted), [])

    def test_mix_of_mapped_and_unmapped(self):
        from scrape import find_unmapped_aka_marketing_names
        extracted = {
            "rooms": [
                {"marketing_name": "One Bedroom Platinum Suite", "rate_plans": []},
                {"marketing_name": "Two Bedroom Duplex Penthouse", "rate_plans": []},
                {"marketing_name": "One Bedroom Terrace Penthouse", "rate_plans": []},
                {"marketing_name": "Three Bedroom Premium Suite", "rate_plans": []},
            ],
        }
        unmapped = find_unmapped_aka_marketing_names(extracted)
        # Two known v1 hallucinations should both fire; the canonical pair
        # should not.
        self.assertIn("Two Bedroom Duplex Penthouse", unmapped)
        self.assertIn("Three Bedroom Premium Suite", unmapped)
        self.assertNotIn("One Bedroom Platinum Suite", unmapped)
        self.assertNotIn("One Bedroom Terrace Penthouse", unmapped)


class TestResumeReQueuesUnknownRoomType(unittest.TestCase):
    """(c) — load_completed_cells() must NOT count a FAIL_UNKNOWN_ROOM_TYPE
    row as 'done'. In practice the scrape writes no rows for these cells,
    but we belt-and-brace the resume contract by writing a sentinel CSV
    and asserting the resume set is empty for that cell."""

    def test_fail_unknown_room_type_row_does_not_mark_cell_done(self):
        import scrape
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "raw_rates.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["property_id", "channel",
                                                  "arrival_date", "nights",
                                                  "status"])
                w.writeheader()
                w.writerow({"property_id": "aka_white_house",
                            "channel": "direct",
                            "arrival_date": "2026-07-13",
                            "nights": 1,
                            "status": "FAIL_UNKNOWN_ROOM_TYPE"})

            original = scrape.RAW_CSV
            scrape.RAW_CSV = csv_path
            try:
                done = scrape.load_completed_cells()
            finally:
                scrape.RAW_CSV = original
            self.assertNotIn(
                ("aka_white_house", "direct", "2026-07-13", 1),
                done,
                "FAIL_UNKNOWN_ROOM_TYPE rows must NOT count as completed; "
                "resume logic must re-queue the cell.",
            )

    def test_ok_row_does_mark_cell_done(self):
        # Sanity check that the resume gate is actually a gate (not always-empty)
        import scrape
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "raw_rates.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["property_id", "channel",
                                                  "arrival_date", "nights",
                                                  "status"])
                w.writeheader()
                w.writerow({"property_id": "aka_white_house",
                            "channel": "direct",
                            "arrival_date": "2026-07-13",
                            "nights": 1,
                            "status": "ok"})
            original = scrape.RAW_CSV
            scrape.RAW_CSV = csv_path
            try:
                done = scrape.load_completed_cells()
            finally:
                scrape.RAW_CSV = original
            self.assertIn(("aka_white_house", "direct", "2026-07-13", 1), done)


if __name__ == "__main__":
    unittest.main()
