"""Phase A2.4 #2 — sentinel rows when canonical pickers find nothing.

Verification step (per brief):
  Unit test with a synthetic plan list containing only refundable plans.
  Assert: pick_canonical_bar() returns None; the caller writes a sentinel
  row with the FAIL_NO_BARE_NON_REF status; the cell-level status is set
  in the run log.
"""
from __future__ import annotations
import os
import unittest

os.environ.setdefault("FIRECRAWL_API_KEY", "test-key-not-used")


class TestSentinelRowOnNoBareNonRef(unittest.TestCase):
    def test_only_refundable_plans_yield_no_bar_picked_and_a_sentinel_row(self):
        from scrape import _flatten_rooms_to_rows, summarize_picker_failures
        from normalize import pick_canonical_bar

        plans = [
            {"rate_plan_label": "Free cancellation",
             "cancellation_phrase": "Free cancellation before Jul 10",
             "rate_per_night_usd": 320.0,
             "bundle_inclusions": None,
             "refundable": True},
            {"rate_plan_label": "Fully refundable",
             "cancellation_phrase": "Fully refundable",
             "rate_per_night_usd": 350.0,
             "bundle_inclusions": None,
             "refundable": True},
        ]
        # Sanity: picker returns None
        self.assertIsNone(pick_canonical_bar(plans))

        extracted = {
            "property_name": "AKA White House",
            "rooms": [
                {"marketing_name": "One Bedroom Platinum Suite", "rate_plans": plans},
            ],
        }
        rows = _flatten_rooms_to_rows(
            extracted, "aka_white_house", "booking", "2026-07-13", 1,
            "https://example/", missed_patterns=[], markdown="",
        )
        # Sentinel row appears
        sentinels = [r for r in rows if r["status"] == "FAIL_NO_BARE_NON_REF"]
        self.assertEqual(len(sentinels), 1, "exactly one FAIL_NO_BARE_NON_REF sentinel per failing room")
        self.assertEqual(sentinels[0]["rate_plan_canonical"], "FAIL_NO_BARE_NON_REF")
        self.assertEqual(sentinels[0]["scraped_marketing_name"], "One Bedroom Platinum Suite")
        # Cell-level surfacing into log: summarize_picker_failures captures it
        failures = summarize_picker_failures(rows)
        kinds = [f["kind"] for f in failures]
        self.assertIn("FAIL_NO_BARE_NON_REF", kinds)

    def test_only_non_refundable_plans_yield_no_flex_picked_and_a_sentinel_row(self):
        from scrape import _flatten_rooms_to_rows, summarize_picker_failures
        from normalize import pick_canonical_flex

        plans = [
            {"rate_plan_label": "Non-refundable",
             "cancellation_phrase": "Non-refundable",
             "rate_per_night_usd": 285.0,
             "bundle_inclusions": None,
             "refundable": False},
        ]
        self.assertIsNone(pick_canonical_flex(plans))

        extracted = {
            "rooms": [
                {"marketing_name": "One Bedroom Platinum Suite", "rate_plans": plans},
            ],
        }
        rows = _flatten_rooms_to_rows(
            extracted, "aka_white_house", "booking", "2026-07-13", 1,
            "https://example/", missed_patterns=[], markdown="",
        )
        sentinels = [r for r in rows if r["status"] == "FAIL_NO_BAR_FLEX"]
        self.assertEqual(len(sentinels), 1)
        failures = summarize_picker_failures(rows)
        self.assertIn("FAIL_NO_BAR_FLEX", [f["kind"] for f in failures])

    def test_both_pickers_succeed_emits_no_sentinel(self):
        from scrape import _flatten_rooms_to_rows, summarize_picker_failures
        plans = [
            {"rate_plan_label": "Non-refundable",
             "cancellation_phrase": "Non-refundable",
             "rate_per_night_usd": 285.0,
             "bundle_inclusions": None},
            {"rate_plan_label": "Free cancellation",
             "cancellation_phrase": "Free cancellation",
             "rate_per_night_usd": 320.0,
             "bundle_inclusions": None},
        ]
        extracted = {
            "rooms": [
                {"marketing_name": "One Bedroom Platinum Suite", "rate_plans": plans},
            ],
        }
        rows = _flatten_rooms_to_rows(
            extracted, "aka_white_house", "booking", "2026-07-13", 1,
            "https://example/", missed_patterns=[], markdown="",
        )
        sentinels = [r for r in rows if r["status"].startswith("FAIL_")]
        self.assertEqual(sentinels, [],
                         "no sentinels when both BAR_NON_REF and BAR_FLEX picked")
        self.assertEqual(summarize_picker_failures(rows), [])

    def test_sentinel_only_cell_is_not_marked_done_by_resume(self):
        # End-to-end resume contract: a cell that produced ONLY sentinel rows
        # has zero ok/extract_incomplete rows → load_completed_cells
        # must not include it in the "done" set, and the cell will be re-queued.
        import csv
        import tempfile
        from pathlib import Path
        import scrape

        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "raw_rates.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["property_id", "channel",
                                                  "arrival_date", "nights", "status"])
                w.writeheader()
                w.writerow({"property_id": "aka_white_house", "channel": "booking",
                            "arrival_date": "2026-07-13", "nights": 1,
                            "status": "FAIL_NO_BARE_NON_REF"})
                w.writerow({"property_id": "aka_white_house", "channel": "booking",
                            "arrival_date": "2026-07-13", "nights": 1,
                            "status": "FAIL_NO_BAR_FLEX"})
            original = scrape.RAW_CSV
            scrape.RAW_CSV = csv_path
            try:
                done = scrape.load_completed_cells()
            finally:
                scrape.RAW_CSV = original
            self.assertNotIn(("aka_white_house", "booking", "2026-07-13", 1), done,
                             "sentinel-only cell must be re-queued (not in done set)")


class TestSentinelTrumpsNonBarRateRows(unittest.TestCase):
    """Phase A2.4b #1 — load_completed_cells must NOT mark a cell complete
    when it has non_bar_rate rows alongside a sentinel. The bug was that
    the cell looked 'done' (because of the non_bar_rate rows) and resume
    would skip it, defeating the sentinel re-queue intent.
    """

    def _write_csv(self, csv_path, rows):
        import csv as _csv
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=["property_id", "channel",
                                               "arrival_date", "nights",
                                               "status"])
            w.writeheader()
            w.writerows(rows)

    def _load_done(self, csv_path):
        import scrape
        original = scrape.RAW_CSV
        scrape.RAW_CSV = csv_path
        try:
            return scrape.load_completed_cells()
        finally:
            scrape.RAW_CSV = original

    def test_four_non_bar_rate_plus_sentinel_is_not_done(self):
        # Brief verification step (#1): synthetic cell with 4 non_bar_rate
        # rows + 1 FAIL_NO_BARE_NON_REF sentinel. Must NOT be in done set.
        import tempfile
        from pathlib import Path
        cell = {"property_id": "aka_white_house", "channel": "booking",
                "arrival_date": "2026-07-13", "nights": 1}
        rows = [{**cell, "status": "non_bar_rate"} for _ in range(4)] + [
            {**cell, "status": "FAIL_NO_BARE_NON_REF"}
        ]
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "raw_rates.csv"
            self._write_csv(csv_path, rows)
            done = self._load_done(csv_path)
        self.assertNotIn(("aka_white_house", "booking", "2026-07-13", 1), done,
                         "non_bar_rate rows must NOT mark cell done when a "
                         "FAIL_NO_BARE_NON_REF sentinel is present")

    def test_ok_row_plus_sentinel_is_not_done(self):
        # Mixed-success cell: some rooms picked BAR (→ ok rows) but at least
        # one room failed picker (→ sentinel). Must still be re-queued.
        import tempfile
        from pathlib import Path
        cell = {"property_id": "aka_white_house", "channel": "direct",
                "arrival_date": "2026-07-13", "nights": 1}
        rows = [
            {**cell, "status": "ok"},
            {**cell, "status": "non_bar_rate"},
            {**cell, "status": "FAIL_NO_BAR_FLEX"},
        ]
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "raw_rates.csv"
            self._write_csv(csv_path, rows)
            done = self._load_done(csv_path)
        self.assertNotIn(("aka_white_house", "direct", "2026-07-13", 1), done,
                         "sentinel must trump ok rows for completion purposes")

    def test_ok_row_with_no_sentinel_is_done(self):
        # Sanity: the gate is not always-empty. ok + non_bar_rate without
        # any sentinel means the cell is fully extracted — mark it done.
        import tempfile
        from pathlib import Path
        cell = {"property_id": "aka_white_house", "channel": "direct",
                "arrival_date": "2026-07-13", "nights": 1}
        rows = [
            {**cell, "status": "ok"},
            {**cell, "status": "non_bar_rate"},
            {**cell, "status": "non_bar_rate"},
        ]
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "raw_rates.csv"
            self._write_csv(csv_path, rows)
            done = self._load_done(csv_path)
        self.assertIn(("aka_white_house", "direct", "2026-07-13", 1), done,
                      "ok + no sentinel → cell is complete")

    def test_only_non_bar_rate_rows_is_not_done(self):
        # Edge case: cell produced only non_bar_rate rows (no ok, no sentinel)
        # — picker failed on every room but somehow no sentinel was written.
        # Under the new gate, this is NOT done — re-queue to be safe.
        import tempfile
        from pathlib import Path
        cell = {"property_id": "aka_white_house", "channel": "booking",
                "arrival_date": "2026-07-13", "nights": 1}
        rows = [{**cell, "status": "non_bar_rate"} for _ in range(3)]
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "raw_rates.csv"
            self._write_csv(csv_path, rows)
            done = self._load_done(csv_path)
        self.assertNotIn(("aka_white_house", "booking", "2026-07-13", 1), done)


if __name__ == "__main__":
    unittest.main()
