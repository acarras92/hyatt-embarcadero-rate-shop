# Analysis 2 — View Premium Through Time

Pairs analyzed: 1BR Platinum (1BD vs 1BDPRC) and 2BR Platinum (2BD vs 2BDPRC).

**Methodology (revised 2026-04-26): rate-plan-matched comparison.** Manual spot-check found that the prior unmatched approach compared different rate-plan types across rooms within the same cell (e.g. non-view at Standard Rate vs view at Fenced early-decision rate), producing artifactual negative premiums. The new approach: for each (channel × date) cell, find rate_plan_labels common to both rooms; pick the canonical match (prefer 'Standard Rate' if both rooms have it; otherwise the lowest-pair-min plan); use that plan's rate for each room. Refundable=True only.
