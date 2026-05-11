# Analysis 2 — View Premium Through Time

Pairs analyzed: 1BR Platinum (1BD vs 1BDPRC) and 2BR Platinum (2BD vs 2BDPRC).

**Methodology (revised 2026-04-26): rate-plan-matched comparison.** Manual spot-check found that the prior unmatched approach compared different rate-plan types across rooms within the same cell (e.g. non-view at Standard Rate vs view at Fenced early-decision rate), producing artifactual negative premiums. The new approach: for each (channel × date) cell, find rate_plan_labels common to both rooms; pick the canonical match (prefer 'Standard Rate' if both rooms have it; otherwise the lowest-pair-min plan); use that plan's rate for each room. Refundable=True only.

## Pattern observations

- GM-claimed view premium: **+$50 flat**.
- The rate-plan-matched empirical premium is materially below the GM's claim in both 1BR and 2BR architectures, but unlike the prior unmatched analysis, **there are no negative-premium cells**. Across all measured cells the view room is priced ≥ the non-view room.
- Direct channel encodes a clean +$18 view-premium rule on both 1BR and 2BR pairs. OTAs (Booking, Hotels.com) sometimes show $0 premium but never negative once rate-plan-matched.
- The story for the IC memo: the GM's +$50 figure is wrong in degree (~3× overstated), but the discipline gap is smaller and cleaner than the prior analysis suggested.
