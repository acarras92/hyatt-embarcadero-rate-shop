# Chrome verification harness — Cowork-side contract

<!-- VERIFICATION_TRACKER_BEGIN -->
## Verification run tracker

**Last run:** 2026-04-26 — `GREEN` (10/10 PASS)
**Consecutive GREEN runs:** 1 of 3 needed for Phase C eligibility (unattended weekly `/schedule` wiring).

Run history (most recent first):
- 2026-04-26 — `GREEN` — 10/10 PASS — scraper `96e7b54`

<!-- VERIFICATION_TRACKER_END -->

Closed-loop verification: this side (claude-code, in this repo) produces an
input spec; Cowork (which has the Claude-in-Chrome MCP) runs the live
browser checks; this side ingests the report and either ships the scrape
or files a fix prompt for the next claude-code session.

```
claude-code (here)            Cowork (Chrome MCP)            claude-code (here)
─────────────────────         ─────────────────────         ──────────────────────
chrome_verify_sample_spec.json ─── (Andrew copies) ───►  Chrome harness runs spec
                                                          ▼
                                                         chrome_verification_report.json
                                                          ▼
                                          (Andrew copies back, or sync via Drive)
                                                          ▼
                                                       apply_chrome_verification.py
                                                          ▼
                                                       tests/fixtures/chrome_truth.csv (regression)
                                                       chrome_bias_summary.md (next prompt input)
```

## Why Cowork?

The Claude-in-Chrome MCP is Cowork-only. Claude Code can't drive a real
browser. The harness logic (navigate → wait → parse → diff) lives in
Cowork; this repo only handles spec generation and report ingestion.

## Inputs

- `verification/chrome_verify_sample_spec.json` — the spec produced by
  Phase B1. Contains the stratified sample (4 canonical SKUs × 2+
  channels each + LOS=7 weekly regression), per-cell `expected{}` block,
  fee-handling notes, verdict thresholds, and lazy-load guidance.

## Process (Cowork session)

For each cell in the spec:

1. **Navigate** to `cell.channel_url` via `mcp__Claude_in_Chrome__navigate`.
2. **Wait** for the rate ladder to render. Per-channel minimums:
   - Direct (stayaka.com / Synxis): >= 8s after URL settle.
   - Booking.com: >= 5s after URL settle.
   - Hotels.com: >= 8s after URL settle, then SCROLL the rate-ladder
     section to bottom, then wait >= 4s more for lazy-loaded rows to
     paint. If `<3` rate rows visible after scroll+wait, retry the wait
     once at >= 8s before recording verdict.
3. **Read page** via `mcp__Claude_in_Chrome__get_page_text` (or DOM-tree
   read if structured access is needed).
4. **Locate** the room matching `cell.expected.marketing_name_verbatim`.
   - Match must be a verbatim substring of the page text (no
     normalization, no abbreviation tolerance).
   - If not found, record `room_type_canonical_present: false` and
     `verdict: FAIL_NOT_FOUND`.
   - If a string from `cell.expected.anti_marketing_names` (when
     present) appears in the page text, record `verdict: FAIL_HALLUCINATED_REGRESSION`
     and stop — this is a real regression to flag immediately.
5. **Within that room block**, identify the BAR_NON_REF rate. Rules
   (mirroring `scraper_lib/normalize.py:pick_canonical_bar()`):
   - Bare (no "Includes parking" / "Includes WiFi" / etc. text on the
     row).
   - Non-refundable (label contains "non-refundable", "prepay", "pay
     now", "advance purchase", or shown as a non-refundable plan in
     the channel's plan ladder).
   - Not a card-issuer / member offer (Visa Offer, Mastercard Offer,
     Genius, Marriott Bonvoy, Hilton Honors, World of Hyatt, AAA, etc.).
   - Lowest nightly rate among the survivors.
6. **Compute all-in pre-tax nightly rate**:
   - **Direct (stayaka.com)**: the displayed Pay Now rate IS all-in
     (resort fee already bundled). No further math.
   - **Booking.com**: displayed `nightly base` + `Resort fee $X/night`
     (visible in the price breakdown when the row is expanded).
   - **Hotels.com**: displayed `nightly base` + `Resort fee $X/night`
     shown separately in the price details. Do NOT use the "$XXX total"
     headline figure — that includes tax, which we don't compare on.
7. **Capture** alongside the BAR_NON_REF row:
   - `promo_banner_text` — any "% off" / "Save up to N%" / "Pay Now and
     Save" / "reduced weekly rate" copy visible in or near the rate row
     (cell-level scan is fine).
   - `weekly_rate_disclosure` — Booking-style "Weekly rate - $X. You're
     getting a reduced weekly rate..." paragraph if present.
   - `strikethrough_orig_rate` — original price shown struck-through
     adjacent to the displayed rate, if any.
   - `bundle_inclusions` — comma-list of "Includes X" items on the
     BAR row (should be empty if the picker landed correctly on a bare
     plan).
   - `member_rate_gated` — true if a "sign in to see Genius prices"
     gate is shown without the gated rate visible.
   - `min_rate_rows_visible` (Hotels.com) — total count of distinct rate
     rows visible in the room's rate ladder.
8. **Diff** captured values against `cell.expected{}` and emit verdict.

## Outputs

- `chrome_verification_report.json` — one entry per cell:
  ```json
  {
    "cell_id": "01_direct_1br_platinum_20260713_los1",
    "verdict": "PASS",          // PASS | WARN | FAIL | FAIL_NOT_FOUND | FAIL_HALLUCINATED_REGRESSION
    "actual": {
      "bar_non_ref_nightly_allin": 342.0,
      "promo_banner_text": "Pay Now and enjoy savings up to 15pct",
      "promo_banner_pct": 15,
      "weekly_rate_disclosure": null,
      "bundle_inclusions": null,
      "marketing_name_actual": "One Bedroom Platinum Suite",
      "member_rate_gated": null
    },
    "diffs": [],                  // [] when PASS; populated when WARN/FAIL
    "screenshots": ["..."],       // optional, paths under verification/screenshots/
    "captured_at": "2026-04-26T18:42:11Z"
  }
  ```
- `chrome_bias_summary.md` — aggregate per (channel, field):
  - PASS / WARN / FAIL counts
  - Sample diffs (top 3 per FAIL bucket)
  - Run-level verdict (GREEN / YELLOW / RED — see below)
  - When YELLOW or RED, formatted as a Phase A1-style bug list so the
    next claude-code session can use it as the input prompt.

## Verdict thresholds

### Cell-level

| Verdict                   | Trigger |
|---------------------------|---------|
| `PASS`                    | All `expected` fields match within tolerance (rate ±2%, banner text contains expected substring case-insensitive) |
| `WARN`                    | Rate matches but a banner / strikethrough / weekly-disclosure field is missing where expected. Likely v2 extractor coverage gap, not a wrong rate. |
| `FAIL`                    | Rate off by >2% OR `room_type_canonical` mismatch OR an unexpected hallucinated string appears |
| `FAIL_NOT_FOUND`          | Could not locate the expected room on the page (page changed, cell broken, or wait insufficient) |
| `FAIL_HALLUCINATED_REGRESSION` | A string from `cell.expected.anti_marketing_names` appeared in page text — immediate regression flag |

### Run-level (aggregate over all cells)

| Verdict   | Trigger | Action |
|-----------|---------|--------|
| `GREEN`   | every (channel, field) pair >= 95% PASS | Ship the v2 scrape; mark this run-id in `tests/fixtures/chrome_truth.csv` |
| `YELLOW`  | any (channel, field) at 80-94% PASS | File a fix ticket; do NOT use latest scrape for IC narrative until resolved |
| `RED`     | any (channel, field) at < 80% PASS | Block the scrape; loop `chrome_bias_summary.md` back to claude-code as the next prompt |

### Stop condition for the verification loop overall

3 consecutive run-level `GREEN` verifications, OR explicit Andrew sign-off.

## What this side does with the report

`verification/apply_chrome_verification.py` (Phase B3) reads
`chrome_verification_report.json` and emits:
- `tests/fixtures/chrome_truth.csv` — locked-in PASS rows become regression
  ground truth. Future scraper runs are checked against this fixture in CI.
- `chrome_bias_summary.md` — per (channel, field) bias breakdown. When the
  run is YELLOW or RED, this file is structured as a bug list to feed the
  next claude-code session as input prompt.

## Known issues (open follow-ons, not blocking live verification)

### Phase A2.4b second-pass correctness fixes — **RESOLVED (2026-04-27)**

A second reviewer pass on commit 8c586ce (Phase A2.4 docs) surfaced 3
residual correctness gaps + 1 latent design question. All 3 critical
gaps landed as fail-closed code paths in Phase A2.4b (commits Phase
A2.4b-#1 through Phase A2.4b-#3). Test count: 59 (47 base + 12 new),
all green; smoke harness 6/6 PASS unchanged.

| #  | Item                                                                  | Status     |
|----|-----------------------------------------------------------------------|------------|
| #1 | `load_completed_cells` sentinel requeue (non_bar_rate trumped)        | RESOLVED  |
| #2 | `FAIL_ROOM_COUNT_SHORT` on Hotels.com gate-4 retry exhaustion         | RESOLVED  |
| #3 | `write_chrome_truth` recomputes all fixture fields (not just critical) | RESOLVED  |
| #4 | TODO: per-channel partition in critical-field coverage gate           | DEFERRED  |

#4 is documented as a `TODO` comment above the coverage gate loop in
`overall_verdict()`. Tighten the partition once the spec grows beyond
~15 cells or adds a 4th channel; the current 10-cell × 3-channel spec
doesn't have meaningful per-channel-coverage holes worth gating on.

### Phase A2.4 fail-closed correctness pass — **RESOLVED (2026-04-26)**

10-item code review of v2 (post-Phase B5 GREEN) flagged 7 critical
correctness gaps + 1 important + 1 medium + 1 docs-only. All 7 critical
gaps landed as fail-closed code paths in Phase A2.4 (commits Phase A2.4-#1
through Phase A2.4-#7) plus Phase A2.4-#4 (a/b/c). Status:

| #  | Item                                                               | Status     |
|----|--------------------------------------------------------------------|------------|
| #1 | Unmapped canonical room → FAIL_UNKNOWN_ROOM_TYPE, no rows persisted | RESOLVED  |
| #2 | Sentinel rows when pick_canonical_bar/flex returns None             | RESOLVED  |
| #3 | Tri-state refundability (REFUNDABLE/NON_REFUNDABLE/UNKNOWN)         | RESOLVED  |
| #4 | apply_chrome_verification.py fail-closed (a + b + c)                | RESOLVED  |
| #5 | BAR rate anchor (Rule 9): every BAR_NON_REF/BAR_FLEX must anchor    | RESOLVED  |
| #6 | Promo collect-then-rank + member_or_card_offer_text column          | RESOLVED  |
| #7 | Hotels.com gate-4 retry on n_rooms < expected_room_count            | RESOLVED  |

Quality bar: each fix has a unit test under `tests/`; full suite runs
green (47 tests). Smoke harness still passes 6/6 cells against
`tests/fixtures/smoke_expected_v2.json`.

Open follow-ons (out of scope for A2.4):
- Phase A2.5: Hotels.com lazy-load via Firecrawl screenshot + vision.
- Friend's item #5 (classify_rate_plan bucket coverage): documented as
  AKA-specific scope; revisit on multi-property skill expansion.

### Hotels.com per-room rate-tier expander render fix — **PARTIAL (Phase A2.3)**

**Status:** Phase A2.3 (2026-04-26) landed two surgical fixes but did
NOT resolve the underlying lazy-load non-determinism. Full fix deferred
to Phase A2.5 (Firecrawl screenshot + Anthropic vision API).

**What A2.3 fixed:**
1. **Gate-4 retry trigger** — was `min(plans_per_room) < 3`, which
   always fired on cells containing the Penthouse (legitimately 2 tiers
   per `CHROME_VERIFICATION_2026-04-26.md` line 83). The retry path
   produced a totals-headline collapse that overwrote the better
   attempt's good data. Now triggers on `max(plans_per_room) < 2` —
   only when no room expanded at all.
2. **EXTRACT_PROMPT add-on math** — the LLM saw the 4 tier rows
   (`Non-Refundable + $0` / `Fully refundable + $46` / `+$81` / `+$128`)
   but emitted all four plans at the base nightly rate, erasing the
   pay-timing/cancellation premium spread. Prompt now instructs:
   `rate_per_night_usd = base_nightly + add_on_amount` per tier.
3. **Multi-scroll action plan** — 20 scrolls × 800ms cadence (initial)
   / 1500ms cadence (retry) replaces v2.2's 2-scroll sequence.
   Architecturally sound; **rendering is stochastic at this combo**.

**What A2.3 did NOT fix — open problem:**

Markdown extraction is **non-deterministic** against Hotels.com's
lazy-loaded rate ladder. Empirical evidence from 4 combos × 100cr on
2026-04-26:

| Combo | waitFor | Scrolls | Per-scroll wait | Result |
|-------|---------|---------|----|--------|
| 1 | 45s | 8 | 1500ms | HTTP error — Firecrawl's `waitFor + sum(wait actions) <= 60s` cap rejected |
| 2 | 8s | 20 | 800ms | **Attempt 1 rendered the full tier ladder in markdown** (proving multi-scroll IS the right axis); attempt 2 (gate-4 retry, since superseded) collapsed to totals headline |
| 3 | 8s | 20 | 800ms (re-run) | 2-plan summary only ("Standard Rate" + "Non-refundable" at base rate); tier ladder did NOT render |
| 4 | 8s | 12 | 1800ms | Page didn't even hydrate — markdown 3113c, query-string dates ignored, EXTRACT_FAILED via rate-anchor |

Same params (combo #2 vs #3) produced different markdown. Slower
cadence (#4) made it worse — the page seems to rely on a settle window
that's not deterministic from the puppeteer side.

**Phase A2.5 plan (next session):**

Use **Firecrawl screenshot mode + Anthropic vision API** for Hotels.com
cells only. Rationale: the rendered page IS visually correct in Cowork
Chrome; the gap is Firecrawl's markdown extraction timing. Capturing
the rendered page as image and letting Claude vision read the rate
ladder bypasses the lazy-load XHR timing problem entirely. Direct +
Booking continue on markdown extraction (they work reliably).

Cost: ~50cr per Hotels.com cell (2× current); only 8 Hotels.com cells
in the matrix so total cell cost is bounded. Worth the spend for
deterministic rate-tier capture.

**What's still working post-A2.3 (don't regress):**
- All 5 AKA SKUs canonical-map correctly on Hotels.com.
- Lead-in BAR_NON_REF rates ($295 / $312 / $422 / $524) match Chrome
  verification exactly.
- E2 anchoring + Rule 8 + Rule 7 pass.
- BAR_NON_REF picker lands on correct lead-in rate when the page
  renders at least 2 plans (the common case).

**Reproduction (current state, post-A2.3):**
```cmd
cd "RM Review\scrape_2026-04-26_part2"
py scrape.py --cells aka_white_house:hotels_com:2026-07-13:1 --credit-ceiling 50
```
Expected (most runs): 10 rows = 5 rooms × 2 plans each (Standard +
Non-refundable at base rate). Occasionally (combo #2-style success):
20 rows = 5 rooms × ~4 plans each with full tier spread. Penthouse
always 2 plans — that's correct.

---

## Out of scope (Phase C)

The Cowork harness is currently invoked manually by Andrew. Once 3
consecutive runs land GREEN, consider scheduling it via the `/schedule`
skill (weekly cadence, stratified 8-12 cells, results emailed if RED).
This converts the loop from "Andrew shuttles between Cowork and Claude
Code" to "system runs itself; Andrew reads `chrome_bias_summary.md` when
something breaks." See `CC_PROMPT_V2_SCRAPER_FIXES.md` Phase C note.
