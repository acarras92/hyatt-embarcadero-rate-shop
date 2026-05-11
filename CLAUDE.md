# Hyatt Regency San Francisco Embarcadero rate-shop — repo context

<!-- narrative slot — author at narrative-regen step after first dashboard build:
     describe subject (operator, market), channel mix, and how the comp set was
     selected. Echo the AKA pattern: 3 channels + N-property Lighthouse comp set,
     subject vs comps for RM positioning. -->
This repo backs hospitality acquisitions diligence on **Hyatt Regency San Francisco Embarcadero**.
It scrapes nightly rates across the configured channels plus a 8-property
comp set, and renders the dashboard at `index.html`. Embarcadero is the subject;
comps exist to position Embarcadero's RM behavior.

## Folder layout

- `scraper_lib/` — Firecrawl scraper. `scrape.py` (extractor + per-cell
  retry orchestration), `validators.py` (Rules 1-7 plausibility gate),
  `schema.py` (raw_rates.csv columns), `canonical_maps.py` (room-type
  string → canonical ID), `normalize.py` (rate-plan picker — one
  BAR_NON_REF + one BAR_FLEX per cell).
- `analysis/` — analytical post-processing of `raw_rates.csv` plus the
  generated `.md` write-ups. **No** scraping logic lives here.
- `verification/` — Chrome verification loop. `chrome_verify_sample_spec.json`
  (input spec produced here), `README.md` (Cowork-side harness contract),
  `apply_chrome_verification.py` (ingest report → regression fixture +
  next-prompt bug list).
- `wiki/channel_quirks.md` — append-only channel-specific lore. Each
  entry date-stamped with the verification run that produced it.
- `tests/fixtures/` — regression fixtures (incl. `chrome_truth.csv`).
- `manual_validation/`, `debug/`, `cell_output/` — historical artefacts.
- `raw_rates.csv` — canonical scrape output (schema v2, 2026-04-26+).

## Canonical rules

- Room-type strings must map via `scraper_lib/canonical_maps.py`. Any
  unmapped string is a `FAIL_UNKNOWN_ROOM_TYPE` validator hit, not a
  silent passthrough.
- Per-cell BAR is picked by `scraper_lib/normalize.py:pick_canonical_bar()` —
  bare (no bundles) + non-refundable + cheapest. Flex picked in parallel.
- Verification loop contract: see `verification/README.md`. Cowork-side
  Chrome harness verifies; this side ingests via `apply_chrome_verification.py`.

## Three things this scraper must NEVER do

1. Emit a `room_type` that isn't verbatim from the source HTML.
2. Pick a bundled rate (parking, breakfast, etc.) as the canonical BAR.
3. Treat cancellation-policy premium spread as a "discount."

## Push policy

Push to `origin/main`: schema migrations, `canonical_maps.py`,
`verification/` harness + spec, `wiki/`, `CLAUDE.md`. **Hold local until
Andrew reviews:** any analytical re-run that rewrites narrative,
`raw_rates.csv` updates from re-scrapes, smoke-test outputs.
