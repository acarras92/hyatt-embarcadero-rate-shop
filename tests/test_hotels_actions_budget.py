"""Phase A2.5b-#2b — Firecrawl scroll/wait budget for Hotels.com.

Firecrawl caps `waitFor + sum(wait actions) <= 60_000ms`. Hotels.com
runs with waitFor=8000ms, so the action plan's wait budget must stay
under 52_000ms. These tests pin that arithmetic so we don't accidentally
push a plan over the cap by tweaking one constant in isolation.
"""
from __future__ import annotations
import os
import unittest

os.environ.setdefault("FIRECRAWL_API_KEY", "test-key-not-used")

# Hotels.com waitFor (matches scrape._scrape_cell channel-specific setting).
HOTELS_WAIT_FOR_MS = 8000
FIRECRAWL_BUDGET_MS = 60000
ACTION_BUDGET_MS = FIRECRAWL_BUDGET_MS - HOTELS_WAIT_FOR_MS  # 52_000
# Firecrawl rejects requests with more than 50 total actions ("Number of
# actions cannot exceed 50"). Each scroll+wait pair counts as 2 actions.
FIRECRAWL_ACTION_CAP = 50


def _sum_action_waits(actions: list[dict]) -> int:
    return sum(a.get("milliseconds", 0)
               for a in actions if a.get("type") == "wait")


class TestHotelsActionsBudget(unittest.TestCase):
    def test_initial_plan_under_firecrawl_cap(self):
        from scrape import _build_hotels_actions
        total = _sum_action_waits(_build_hotels_actions(retry=False))
        self.assertLess(total, ACTION_BUDGET_MS,
                        f"initial plan waits {total}ms exceed budget {ACTION_BUDGET_MS}ms")

    def test_retry_plan_under_firecrawl_cap(self):
        from scrape import _build_hotels_actions
        total = _sum_action_waits(_build_hotels_actions(retry=True))
        self.assertLess(total, ACTION_BUDGET_MS,
                        f"retry plan waits {total}ms exceed budget {ACTION_BUDGET_MS}ms")

    def test_initial_plan_has_re_traverse_pass(self):
        # Phase A2.5b-#2b: lower rooms' IntersectionObserver often fires only
        # on second viewport entry, so the plan must include both directions.
        from scrape import _build_hotels_actions
        actions = _build_hotels_actions(retry=False)
        directions = [a["direction"] for a in actions if a.get("type") == "scroll"]
        self.assertIn("up", directions, "initial plan must include scroll-up re-traverse")
        self.assertIn("down", directions)
        # Down scrolls should outnumber up scrolls (we end at the bottom).
        self.assertGreater(directions.count("down"), directions.count("up"))

    def test_retry_plan_has_re_traverse_pass(self):
        from scrape import _build_hotels_actions
        actions = _build_hotels_actions(retry=True)
        directions = [a["direction"] for a in actions if a.get("type") == "scroll"]
        self.assertIn("up", directions, "retry plan must include scroll-up re-traverse")
        self.assertGreater(directions.count("down"), directions.count("up"))

    def test_retry_plan_is_at_least_as_long_as_initial(self):
        # Retry path is the more aggressive plan; total wait must be >= initial.
        from scrape import _build_hotels_actions
        initial = _sum_action_waits(_build_hotels_actions(retry=False))
        retry = _sum_action_waits(_build_hotels_actions(retry=True))
        self.assertGreaterEqual(retry, initial,
                                f"retry ({retry}ms) must be >= initial ({initial}ms)")

    def test_initial_plan_under_action_count_cap(self):
        # Phase A2.5b regression: live smoke 2026-04-27 returned HTTP_ERROR
        # "Number of actions cannot exceed 50" when the plan had 77 actions.
        # Pin the cap so we don't re-introduce that.
        from scrape import _build_hotels_actions
        actions = _build_hotels_actions(retry=False)
        self.assertLessEqual(
            len(actions), FIRECRAWL_ACTION_CAP,
            f"initial plan has {len(actions)} actions — exceeds Firecrawl cap of {FIRECRAWL_ACTION_CAP}"
        )

    def test_retry_plan_under_action_count_cap(self):
        from scrape import _build_hotels_actions
        actions = _build_hotels_actions(retry=True)
        self.assertLessEqual(
            len(actions), FIRECRAWL_ACTION_CAP,
            f"retry plan has {len(actions)} actions — exceeds Firecrawl cap of {FIRECRAWL_ACTION_CAP}"
        )

    def test_initial_plan_scroll_count_meaningful(self):
        # Even with the 50-action cap we still need enough scrolls (>= 21)
        # to traverse a 7-room page twice (down → partial-up → down). Old
        # 20-scroll unidirectional plan was empirically non-deterministic.
        from scrape import _build_hotels_actions
        scrolls = [a for a in _build_hotels_actions(retry=False)
                   if a.get("type") == "scroll"]
        self.assertGreaterEqual(
            len(scrolls), 21,
            f"initial plan has {len(scrolls)} scrolls — too few for re-traverse"
        )


if __name__ == "__main__":
    unittest.main()
