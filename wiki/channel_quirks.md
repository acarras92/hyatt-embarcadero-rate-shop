# Channel Quirks

Append-only channel-specific lore. Each entry is date-stamped with the
verification run that produced it. See CLAUDE.md for the push policy.

---

## Hyatt brand.com `/shop/rooms/` — WAF blocked for Firecrawl

**Dated:** 2026-05-10 (SFOEM confirmation)
**Status:** Confirmed. Firecrawl cannot reach Hyatt direct.

### Symptoms

- **curl (browser User-Agent):** HTTP 403 in ~74ms. Response body is a 12.8 KB
  generic "Hyatt Hotels and Resorts" denial page. No redirects, no Retry-After.
  Source-Country / Source-Region cookies set, indicating Hyatt fingerprinted the
  IP before responding.
- **Firecrawl scrape:** returns `markdown_len: 0` and an LLM-extractor fallback
  that hallucinates property names from training data — observed names include
  `"Sample Hotel"`, `"Sample Hotel Name"`, `"Example Hotel"`, `"Example Hotel
  Name"`, `"The Grand Hotel"`, `"The Grand Oasis Hotel"`.
- **Speed of rejection** (~74ms on curl) implies an edge-level WAF rule, not an
  application-level decision. The page never actually renders for the scraper.
- **Longer waitFor doesn't help.** Smoke-tested `--direct-wait-for-override
  20000` (20s) and `--firecrawl-timeout-ms 120000` (2 min) — Firecrawl still
  returns in 8–30s with `markdown_len: 0`. There is nothing to wait for; the
  response is empty on arrival.

### Detection signature

100% of Direct cells return `markdown_len: 0` **and** a hallucinated property
name that fails the plausibility guard in `scraper_lib/validators.py`. The
`bot_blocked` outcome category is **not** triggered (Firecrawl doesn't surface
an explicit bot-block signal), so the failures land as `extract_failed` with
`reason: "property_name looks fabricated: '...'" ` or
`"property_name '<wrong-hotel>' contains none of expected tokens [...]"`.

When you see those two signatures together on a Hyatt direct cell, stop the
scrape — no amount of retrying will produce data.

### Speculated root cause

- **TLS fingerprint (JA3):** Firecrawl's HTTP client and curl emit distinctive
  TLS handshakes vs. real Chrome. Hyatt's WAF likely fingerprints on JA3 + JA4.
- **Datacenter IP ranges:** Firecrawl's default scrape fleet is on cloud IPs,
  not residential. Hyatt almost certainly blocks known datacenter ASNs.
- **Header order / canonicalization:** Real browsers send a stable header
  ordering and a specific set of `Sec-Fetch-*` / `Sec-CH-*` hints. Curl + most
  scraper libs don't replicate that exactly.

The 74ms rejection time is too fast for application-level inspection, so it's
almost certainly a CDN-edge rule fired on the cheap fingerprints above.

### Recovery path

**Bulk (large-N):** Chrome MCP harness via the `verification/` pattern — real
Chrome instance running on a residential IP, full JS execution, valid TLS
fingerprint. That's what `verification/chrome_verify_sample_spec.json` +
`verification/apply_chrome_verification.py` are designed for.

**Small-N / one-off (≤ ~10 Sundays):** Manual Claude-in-Chrome capture.
Proven viable on SFOEM: 8 Sundays × ~15 rooms each captured by hand
2026-05-10, loaded into `raw_rates.csv` via
`scraper_lib/seed_chrome_captured_2026_05_10.py`. Time cost: ~30-45 minutes
of operator time vs. the indefinite block on Firecrawl.

**Stealth mode (not validated):** Firecrawl's stealth tier uses residential
proxies. Theoretically should bypass IP fingerprinting, but speed of Hyatt's
edge rejection suggests the WAF rule fires on cheap TLS / header signals that
residential IPs alone won't clear. Not tested on SFOEM (Andrew opted out:
low-confidence + non-trivial cost). Worth a single-cell test on the next deal
if Chrome harness is infeasible.

### Related framework entries

- **F23** anticipated direct-channel bot-walling as a generic risk during
  the scraper-architecture review.
- **F45** confirmed via SFOEM that Hyatt specifically enforces this on
  `/shop/rooms/` — first hard observation post-F23.

### Operational notes

- Don't re-attempt with the same Firecrawl config — the failure is deterministic.
- The pre-flight `--dry-run` won't catch this; dry-run only enumerates cells.
  Smoke-test one live cell before committing budget on any new Hyatt deal.
- Per-cell credit cost on the failure path is ~5 credits (compared to ~8 on
  success), so a 156-cell run still spends ~780 credits to produce zero rows.
  Always smoke-test first.
