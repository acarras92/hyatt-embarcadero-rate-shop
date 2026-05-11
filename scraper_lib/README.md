# scraper_lib — Canonical Rule-7-equipped scraper

This folder versions the **canonical, anti-hallucination-equipped** copies of
the Firecrawl-based scraper that produced this repo's `raw_rates.csv`. The
working copies ran from a sibling project folder (`RM Review/scrape_2026-04-24/`)
which is not under git. The files here exist to keep the fix recoverable if
that working folder is ever lost or moved.

## Files

- `validators.py` — Plausibility gate with **Rule 7 (rate-anchor check)**
  added 2026-04-25 after manual PDF validation found that ~49% of comp cells
  in the original 2026-04-24 run had LLM-fabricated rates (rates that the LLM
  invented onto pages that displayed no rate ladder). Rule 7 rejects any
  extract where 0 of N unique extracted rates appear as `$<rate>` tokens in
  the source markdown.
- `scrape.py` — Firecrawl scraper. The only post-Rule-7 change here is the
  one-line caller update at the `check_plausibility(...)` site (passes
  `markdown=md` instead of `markdown_len=len(md)`).

## Usage

These files are **not wired in at runtime** — the rate-shop dashboard
(`build_dashboard.py`) only consumes `raw_rates.csv` and doesn't invoke the
scraper directly. To run a fresh scrape:

1. Copy `scraper_lib/scrape.py`, `scraper_lib/validators.py`, plus a
   `config.json` (matching the property × channel × date matrix) into a
   working directory.
2. Drop a `.env` with `FIRECRAWL_API_KEY=...` in the same directory.
3. Create a `cell_output/` subdirectory for the per-attempt response cache.
4. Run as documented in `scrape.py`'s docstring (e.g.
   `python scrape.py --cells <prop>:<channel>:<arrival>:<los>` for single
   cells, or `python scrape.py --full` for the full matrix).

The scraper writes rows to `./raw_rates.csv` in the working directory; sync
that file back into the rate-shop repo when the run completes.

## Why Rule 7 exists

The original plausibility gates (Rules 1-6) check that the extracted JSON's
property name and arrival date match the requested cell, and that rates fall
within plausibility bounds. They do **not** check that extracted rates have
any source-of-truth in the page content. Firecrawl's LLM extractor "completes
the schema" when it encounters a page without rate data — fabricating
plausible-looking numbers rather than returning empty `rate_plans[]`.
Observed patterns:

- **Booking returning "no availability"** but LLM filling 2-6 plausible
  discount rates per room (Hay-Adams Booking 2026-09-16: $130 / $150 / $180
  / $200 invented onto a page where the actual rate columns said "Not
  available on our site for your dates").
- **Direct booking engines returning generic property/rooms-overview pages**
  without rates (IHG Willard direct: scraper got `### Henry Augustus Willard
  Suite` as a heading + a `SELECT DATES` button image, fabricated $365-$600
  rate band across all 4 rooms including the actual $4,239 top-tier suite).
- **Placeholder direct URLs** that returned generic homepages for the entire
  2026-04-24 run (Hay-Adams direct, Jefferson direct, St. Regis direct,
  Willard direct — 100% of cells fabricated for these URL templates).

Rule 7 catches all three by requiring that at least one extracted rate
appear as a `$<rate>` token in the source markdown. The check is
conservative (1 of N anchored = pass) but sufficient — clean cells in our
universe show 30-100% anchor rates; fabricated cells show 0%.

See `debug/hayadams_booking_20260916/findings.md` and
`debug/willard_direct_20260512/findings.md` in the repo root for the original
forensic write-up, plus `manual_validation/purge_report.md` for the
retroactive-cleanup record (2,059 of 4,911 rows removed, 41.9%).

## Cell-outcome verdict enum (Resolution 8 — rm-dashboard-rollout)

`validators.py` exposes `EXPECTED_VERDICT_VALUES` as the canonical enum for
per-cell outcome categorization (consumed by `verification/apply_chrome_verification.py`
and by any harness that diffs scraper output against expected behavior).

Members:

| Value                  | Meaning                                                          |
| ---------------------- | ---------------------------------------------------------------- |
| `PASS`                 | Rows extracted; cell agrees with spec.                           |
| `FAIL_EXPECTED`        | Cell failed in a way the spec explicitly expected (e.g. a known sold-out date used as a fail-fixture). |
| `FAIL_KNOWN_GAP`       | Cell failed because of a documented gap (CRS down, etc.) tracked in `wiki/channel_quirks.md`. |
| `NO_DATA`              | Cell produced no rows and the harness has no further classification — fallthrough only. |
| `PASS_NO_INVENTORY`    | URL works, page renders for the expected property, cell is legitimately empty (sold out / "no rooms available"). Not a bug. |
| `FAIL_URL_BROKEN`      | URL itself is the problem: 404 / 5xx / bot-block / DataDome / Imperva / page lacks expected-property identity. Cell needs URL recon, not a re-run. |

`PASS_NO_INVENTORY` and `FAIL_URL_BROKEN` are the load-bearing additions
made during skill packaging (Resolution 8 of the rm-dashboard-rollout
RESOLUTIONS.md). Prior to that change, both outcomes collapsed to
`NO_DATA`, hiding a class of silent failure where a booking URL drifted
out of sync with the property's CRS. After Resolution 8, the
`apply_chrome_verification.py` row-writer emits
`expected_verdict_resolved` per cell so regression categorization is
precise.

The `validators.classify_chrome_cell_verdict()` helper is the single
decision point that maps Chrome-harness evidence (bot-block signature,
HTTP status, empty-page classification, row count, spec expectation) to
one of these values; consumers should call it rather than re-implementing
the precedence rules.
