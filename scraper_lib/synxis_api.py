"""Synxis (Sabre Hospitality) direct-CRS API adapter.

Replaces Firecrawl LLM extraction for properties whose direct booking engine
is Synxis. Hits the same JSON endpoint the booking-engine SPA calls
(`/gw/product/v1/getProductAvailability`) with the request schema observed
in the live network trace, parses the `ProductAvailabilityDetail.Prices`
array into the project's room/rate_plan shape, and returns the same
`{success, data: {markdown, json}, error}` envelope `firecrawl_scrape_json`
returns so the gate plumbing in `scrape_cell` stays unchanged.

Property-configurable: `base_url`, `hotel_id`, `chain_id` come from
`config.json -> properties[*].synxis`. Onboarding a new Synxis-powered
property is a config-only change.

Auth + session details (verified live 2026-04-28 against AKA White House,
chain=27508 / hotel=56224):

  - The static `WIDGET_API_KEY` token lives in the SPA's server-rendered
    inline runtime-config block (not the JS bundle, which ships a generic
    multi-tenant key map). Probing the bare base_url redirects to the
    marketing site, so we hit a deep-linked URL with `chain` + `hotel`
    query params to force the booking SPA to render. The chain-specific
    token then appears as `"WIDGET_API_KEY":"<base64>"` in inline JSON.
  - The API gateway enforces an `Origin` / `Referer` check; missing them
    returns HTTP 400 `{"Message":"Invalid origin"}`.
  - Imperva/Distil bot-protection serves a 'Pardon Our Interruption'
    challenge HTML for raw POSTs without browser cookies. We warm a
    `requests.Session` with the SPA fetch (which sets `visid_incap_*`,
    `incap_ses_*`, etc.) and reuse it for API calls.

Both the api_key and the warmed session are cached module-level keyed by
`base_url`, so multiple Synxis-powered properties coexist without
re-fetching either.
"""
from __future__ import annotations
import datetime as _dt
import json as _json
import os
import re
import uuid
from collections import defaultdict
from pathlib import Path as _Path
from typing import Optional

import requests


# Module-level caches: paired so a session and the api_key extracted from
# its first response stay together. Keyed by base_url.
_api_key_cache: dict[str, str] = {}
_session_cache: dict[str, requests.Session] = {}

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Token embedding seen in two forms across the booking-engine assets:
#   JS literal: WIDGET_API_KEY:"<base64>"            (in /public/js/client.*.js)
#   JSON value: "WIDGET_API_KEY":"<base64>"          (in server-rendered HTML)
# The SPA HTML form is the one we use because it is chain-scoped (the JS
# bundle ships a generic multi-tenant map that gets the wrong tenant first).
# The optional `"` around the key handles both. The token is base64 with
# `+`/`=` allowed, length 80-400.
_WIDGET_KEY_RE = re.compile(
    r'"?WIDGET_API_KEY"?\s*:\s*"([A-Za-z0-9+/=]{80,400})"'
)


def _ensure_session(
    base_url: str,
    *,
    chain_id: Optional[str] = None,
    hotel_id: Optional[str] = None,
) -> requests.Session:
    """Return a `requests.Session` that has Imperva cookies warmed and is
    paired with a cached `WIDGET_API_KEY` for `base_url`.

    On cache hit returns the cached session immediately. On miss probes a
    deep-linked SPA URL (which loads Imperva cookies into the session
    *and* server-renders the chain-specific WIDGET_API_KEY in inline
    JSON), extracts the key, caches both, and returns the session.

    Raises RuntimeError if the WIDGET_API_KEY can't be located in the
    response (Imperva challenge, chain-selector page, etc.).
    """
    if base_url in _session_cache and base_url in _api_key_cache:
        return _session_cache[base_url]

    session = requests.Session()
    session.headers.update({
        "User-Agent": _USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    })

    if chain_id and hotel_id:
        probe_url = (
            f"{base_url.rstrip('/')}/?chain={chain_id}&hotel={hotel_id}"
            "&level=hotel&adult=2&child=0&rooms=1&locale=en-US&currency=USD"
        )
    else:
        probe_url = base_url

    resp = session.get(probe_url, timeout=20)
    resp.raise_for_status()

    m = _WIDGET_KEY_RE.search(resp.text)
    if not m:
        raise RuntimeError(
            f"Synxis WIDGET_API_KEY not found in server-rendered HTML at "
            f"{probe_url} (response was {len(resp.text)} chars). The page "
            "likely returned an Imperva 'Pardon Our Interruption' challenge "
            "or the chain-selector instead of the booking SPA. Open the URL "
            "in Chrome DevTools -> Network -> filter by "
            "'getProductAvailability' -> inspect the Authorization request "
            "header to retrieve the key manually."
        )

    full_key = f"ApiKey {m.group(1)}"
    _api_key_cache[base_url] = full_key
    _session_cache[base_url] = session
    return session


def get_synxis_api_key(
    base_url: str,
    *,
    chain_id: Optional[str] = None,
    hotel_id: Optional[str] = None,
) -> str:
    """Public accessor — returns the cached `ApiKey <token>` Authorization
    value for `base_url`, populating the cache (and warmed session) if
    needed. Kept as a thin wrapper so external diagnostics can grab the
    key without needing the session.
    """
    _ensure_session(base_url, chain_id=chain_id, hotel_id=hotel_id)
    return _api_key_cache[base_url]


def _invalidate_cache(base_url: str) -> None:
    _session_cache.pop(base_url, None)
    _api_key_cache.pop(base_url, None)


def _amount_with_fees_for_stay(product_block: dict, los: int) -> Optional[float]:
    """Return the total-stay AmountWithFees for a `Product` block, or None
    if the path isn't populated. Synxis exposes three price buckets under
    `Product.Prices`:
      - Daily[]: per-night Price entries (no per-stay fees rolled in)
      - PerNight: average per-night (no per-stay fees rolled in)
      - Total: full-stay totals INCLUDING per-stay destination fees
    The displayed "you'll pay" guest-facing rate matches `Total.Price.
    Total.AmountWithFees`. Confirmed for AKA: 1BDP/NREFd 2026-04-28 LOS1
    => $926.38 (= $880 room + $40 destination fee + $6.38 fee tax).
    """
    prices = product_block.get("Prices", {}) or {}
    total_block = (prices.get("Total", {}) or {}).get("Price", {}) or {}
    total_total = (total_block.get("Total", {}) or {})
    val = total_total.get("AmountWithFees")
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)

    # Fallback: per-night Amount × LOS when Total bucket is missing.
    pernight_block = (prices.get("PerNight", {}) or {}).get("Price", {}) or {}
    pn_total = (pernight_block.get("Total", {}) or {})
    pn_val = pn_total.get("Amount")
    if isinstance(pn_val, (int, float)) and not isinstance(pn_val, bool):
        return float(pn_val) * max(1, los)

    return None


# Refundability matchers — applied in order, first match wins.
# Patterns are deliberately simple substring/word checks rather than
# elaborate regex because the inputs are short, controlled rate names.
# The NRF check runs FIRST so that strings like "non-cancellable" match
# the NRF arm before falling through to the FLEX arm's bare "cancel"
# trigger.
_NRF_PATTERNS = re.compile(
    r"\b(?:non[\s-]?refund\w*|non[\s-]?cancel\w*|no[\s-]?refund\w*)\b",
    re.IGNORECASE,
)
_NRF_NAME_HINT = re.compile(r"\bpay\s+now\b", re.IGNORECASE)
_FLEX_PATTERNS = re.compile(
    r"\b(?:flexible|free\s+cancellation|fully\s+refundable)\b",
    re.IGNORECASE,
)
_FLEX_FALLBACK_CANCEL = re.compile(r"\bcancel\w*\b", re.IGNORECASE)


def _refundability_from_rate_meta(
    rate_meta: dict,
) -> tuple[Optional[bool], Optional[str]]:
    """Return `(refundable, cancellation_phrase)` based on rate metadata.

    Inputs from the Synxis ContentLists.RateList:
      - `category`  — CategoryCode ("NRF" / "BAR" / "PRO" / "DIS" / "PKG")
      - `name`      — rate's display Name (always populated)
      - `descr`     — Details.Description (short copy)
      - `long_descr` — Details.DetailedDescription (full cancellation copy)

    The cancellation_phrase we return is consumed by both
    `_flatten_rooms_to_rows` (which mutates `plan["refundable"]` from it)
    and `normalize.refundability_state` (which IGNORES `plan["refundable"]`
    and re-derives the state from token matches in cancellation_phrase /
    rate_plan_label). For the picker to find BAR_FLEX / BAR_NON_REF
    candidates in Synxis-pathed cells, the phrase MUST contain a token
    from `normalize.{NON_REFUNDABLE,REFUNDABLE}_PHRASE_TOKENS`:

        NON_REFUNDABLE_PHRASE_TOKENS = ('non-refundable', 'non refundable',
                                        'nonrefundable')
        REFUNDABLE_PHRASE_TOKENS     = ('free cancellation',
                                        'fully refundable', 'refundable')

    When Synxis ships a verbatim DetailedDescription containing one of
    those tokens (e.g. NREFd's text contains "non-refundable"), we use
    that verbatim. When the text is absent or token-free (e.g. BFRd's
    short Name "Best Flexible Rate" doesn't match any token), we
    synthesize a token-bearing phrase prefixed with the rate Name so the
    raw_rates.csv cancellation_phrase column still tells the operator
    which Synxis rate the row came from.

    Empirical AKA Synxis rate catalog ([category, code]):
      [NRF, NREFd]    Pay Now and Save             → False
      [NRF, NREF7d]   Pay Now, Stay 7+ and Save!   → False (7+ night LOS)
      [NRF, PKGPARdn] Parking Package              → False (NRF subtype)
      [BAR, BFRd]     Best Flexible Rate           → True  (canonical BAR_FLEX)
      [PKG, PKGPARKd] Parking Package (Flexible)   → True (but disqualified
                                                          by 'package' NON_BAR token)
      [PRO, PROVISA]  Visa Offer                   → None (no clear marker)
      [PRO, PROMCO]   Mastercard Offer             → None
      [DIS, AAA]      AAA                          → None
      [DIS, GOV]      Government Rate              → None
      etc.
    """
    cat = (rate_meta.get("category") or "").upper()
    name = rate_meta.get("name") or ""
    descr = rate_meta.get("descr") or ""
    long_descr = rate_meta.get("long_descr") or ""
    text_pool = long_descr or descr or name

    def _phrase(state_token: str) -> str:
        """Build a cancellation_phrase that:
          - contains the picker-recognized token (`state_token`)
          - preserves the rate's display Name as a debug breadcrumb
          - prefers verbatim text when it already contains a matching token
        """
        # If Synxis ships a verbatim cancellation policy that already
        # carries a matching token, use it as-is. Otherwise synthesize.
        if state_token == "non-refundable":
            if any(t in text_pool.lower() for t in
                   ("non-refundable", "non refundable", "nonrefundable")):
                return text_pool
            return f"Non-refundable ({name})" if name else "Non-refundable"
        # state_token in ("free cancellation", "refundable", ...)
        if any(t in text_pool.lower() for t in
               ("free cancellation", "fully refundable", "refundable")):
            return text_pool
        return f"Refundable ({name})" if name else "Refundable"

    # Rule 1: CategoryCode=NRF is unambiguous and authoritative.
    if cat == "NRF":
        return False, _phrase("non-refundable")

    # Rule 2: explicit non-refundable / non-cancellable language in any
    # of the available text fields.
    if (_NRF_PATTERNS.search(text_pool) or _NRF_PATTERNS.search(name)):
        return False, _phrase("non-refundable")

    # Rule 3: defensive backup — "Pay Now" naming convention.
    if _NRF_NAME_HINT.search(name):
        return False, _phrase("non-refundable")

    # Rule 4: explicit refundable language.
    if (_FLEX_PATTERNS.search(text_pool) or _FLEX_PATTERNS.search(name)):
        return True, _phrase("refundable")

    # Rule 5: bare "cancel" — only after Rule 2 ruled out "non-cancellable".
    if (_FLEX_FALLBACK_CANCEL.search(text_pool)
            or _FLEX_FALLBACK_CANCEL.search(name)):
        return True, _phrase("refundable")

    return None, None


def fetch_synxis_direct(
    check_in: str,
    los: int,
    *,
    base_url: str,
    hotel_id: str,
    chain_id: str,
    adults: int = 2,
    children: int = 0,
    property_name_for_payload: str = "AKA White House",
) -> dict:
    """Fetch full product availability from the Synxis API for one
    check-in date / LOS.

    Returns the same envelope as `firecrawl_scrape_json`:
        {success: bool, data: {markdown: str, json: dict} | None, error: str}

    `data.json` conforms to EXTRACT_SCHEMA (property_name, arrival_date,
    nights, rooms[]). `data.markdown` is always "" — the API path has no
    rendered page; markdown-anchored gates in the caller must skip it.
    """
    # ---- check-out date ----
    try:
        check_out = (
            _dt.date.fromisoformat(check_in) + _dt.timedelta(days=los)
        ).isoformat()
    except Exception as e:
        return {"success": False, "data": None,
                "error": f"Bad check_in/los inputs: {e}"}

    # ---- session + api_key (with one retry on cache invalidation) ----
    for attempt in range(2):
        try:
            session = _ensure_session(
                base_url, chain_id=chain_id, hotel_id=hotel_id,
            )
            api_key = _api_key_cache[base_url]
        except Exception as e:
            return {"success": False, "data": None,
                    "error": f"ApiKey/session init failed: {e}"}

        # ---- request ----
        # Body shape extracted from the SPA bundle:
        #   route "getProductAvailability" expects body wrapped in
        #   `ProductAvailabilityQuery` with `Hotel: {Id}` (singular, NOT
        #   `HotelList`), `Currency.currencyCode` lowercase, ChannelList
        #   `Code` PascalCase, GuestCount `AgeQualifyingCode`/`NumGuests`
        #   PascalCase. `OnlyCheckRequested:false` returns ALL products
        #   for the hotel (vs. true which requires per-product Requested
        #   markers and otherwise returns RequestedNotIncluded).
        #   `ReturnFullContentDetails:true` populates ContentLists.RoomList
        #   and ContentLists.RateList with display names — the only way to
        #   resolve room/rate codes to marketing copy.
        body = {
            "Paging": {"Size": 100},
            "ProductAvailabilityQuery": {
                "OnlyCheckRequested": False,
                "ReturnFullContentDetails": True,
                "Chain": {"Id": chain_id},
                "Hotel": {"Id": hotel_id},
                "Currency": {"currencyCode": "USD"},
                "ChannelList": {
                    "PrimaryChannel": {"Code": "WEB"},
                    "SecondaryChannel": {"Code": "GC"},
                },
                "NumRooms": 1,
                "LoyaltyList": [],
                "RoomStay": {
                    "StartDate": check_in,
                    "EndDate": check_out,
                    "GuestCount": [
                        {"AgeQualifyingCode": "Adult", "NumGuests": adults},
                        {"AgeQualifyingCode": "Child", "NumGuests": children,
                         "Ages": []},
                    ],
                },
            },
        }

        origin = base_url.rstrip("/")
        headers = {
            "Authorization": api_key,
            "activityid": uuid.uuid4().hex[:10],
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": _USER_AGENT,
            "Origin": origin,
            "Referer": f"{origin}/",
        }
        endpoint = f"{origin}/gw/product/v1/getProductAvailability"

        try:
            r = session.post(endpoint, json=body, headers=headers, timeout=30)
        except Exception as e:
            return {"success": False, "data": None,
                    "error": f"Synxis POST failed: {e}"}

        # Imperva/Distil challenge: HTML response on what should be a JSON
        # endpoint. Invalidate the cached session (its cookies expired or
        # were never sufficient) and retry once with a fresh warmup.
        ct = r.headers.get("content-type", "")
        if not ct.startswith("application/json"):
            if attempt == 0:
                _invalidate_cache(base_url)
                continue
            return {"success": False, "data": None,
                    "error": (
                        f"Synxis POST returned non-JSON (content-type "
                        f"{ct!r}); likely Imperva challenge after retry: "
                        f"{(r.text or '')[:300]}"
                    )}

        if r.status_code >= 400:
            return {"success": False, "data": None,
                    "error": f"Synxis POST {r.status_code}: {(r.text or '')[:600]}"}

        try:
            resp = r.json()
        except Exception as e:
            return {"success": False, "data": None,
                    "error": f"Synxis JSON parse failed: {e}"}

        break  # successful JSON response, exit retry loop

    # ---- optional debug dump ----
    if os.environ.get("SYNXIS_DEBUG_DUMP"):
        _Path(os.environ["SYNXIS_DEBUG_DUMP"]).write_text(
            _json.dumps(resp, indent=2), encoding="utf-8"
        )

    # ---- application-level error envelope ----
    app_results = resp.get("ApplicationResults") or {}
    app_errors = app_results.get("Error")
    if app_errors:
        # Synxis returns Success/Error siblings under ApplicationResults.
        # When present, Error is a list of detail dicts.
        return {"success": False, "data": None,
                "error": f"Synxis ApplicationResults error: "
                         f"{_json.dumps(app_errors)[:500]}"}

    # ---- parse content lists for display-name lookups ----
    content = resp.get("ContentLists") or {}
    room_lookup: dict[str, str] = {}
    for room in (content.get("RoomList") or []):
        if not isinstance(room, dict):
            continue
        code = room.get("Code")
        if not code:
            continue
        # Prefer the short marketing Name; fall back to the longer
        # Details.Description if Name isn't populated for some chain
        # configs, then to the raw code.
        name = (room.get("Name")
                or (room.get("Details") or {}).get("Description")
                or code)
        room_lookup[str(code)] = str(name)

    rate_lookup: dict[str, dict] = {}
    for rate in (content.get("RateList") or []):
        if not isinstance(rate, dict):
            continue
        code = rate.get("Code")
        if not code:
            continue
        details = rate.get("Details") or {}
        name = (rate.get("Name")
                or details.get("DisplayName")
                or details.get("Description")
                or code)
        rate_lookup[str(code)] = {
            "name": str(name),
            "category": str(rate.get("CategoryCode") or ""),
            "rate_class": str(details.get("RateClass") or ""),
            # DetailedDescription carries the verbatim cancellation copy.
            # _refundability_from_rate_meta uses it (when present) to
            # decide refundability and to populate cancellation_phrase
            # so the downstream flatten step's mutation matches what it
            # would for a Firecrawl-extracted rate plan.
            "descr": str(details.get("Description") or ""),
            "long_descr": str(details.get("DetailedDescription") or ""),
        }

    # ---- iterate Prices and build rooms/plans ----
    details: list[dict] = []
    detail = resp.get("ProductAvailabilityDetail")
    if isinstance(detail, dict):
        details.append(detail)
    elif isinstance(detail, list):
        details.extend(d for d in detail if isinstance(d, dict))

    # Older / multi-hotel responses use ProductAvailabilityList. Parse every
    # detail block, not just the first, so room/rate rows are not silently lost
    # when Synxis splits availability across list entries.
    pal = resp.get("ProductAvailabilityList") or []
    if isinstance(pal, list):
        details.extend(d for d in pal if isinstance(d, dict))
    elif isinstance(pal, dict):
        details.append(pal)

    prices: list[dict] = []
    for detail_block in details:
        for price in (detail_block.get("Prices") or []):
            if isinstance(price, dict):
                prices.append(price)

    rooms_map: dict[str, list[dict]] = defaultdict(list)
    for entry in prices:
        if not isinstance(entry, dict):
            continue
        if entry.get("Available") is False:
            # Synxis can include unavailable products with stale/display prices.
            # Do not persist those as bookable rate rows.
            continue
        product = entry.get("Product") or {}
        room_code = (product.get("Room") or {}).get("Code")
        rate_code = (product.get("Rate") or {}).get("Code")
        if not room_code or not rate_code:
            # Aggregate / summary rows without an attached Product —
            # skip; we only care about per-room/per-rate prices.
            continue

        total_with_fees = _amount_with_fees_for_stay(product, los)
        if total_with_fees is None:
            # No usable price for this product/rate combination — skip
            # rather than emit a $0 row that would trip plausibility
            # rules downstream.
            continue

        rate_meta = rate_lookup.get(str(rate_code), {
            "name": rate_code, "category": "", "rate_class": "",
            "descr": "", "long_descr": "",
        })
        per_night = total_with_fees / max(1, los)
        refundable, cancellation_phrase = _refundability_from_rate_meta(rate_meta)

        rooms_map[str(room_code)].append({
            "rate_plan_label": rate_meta["name"],
            "rate_per_night_usd": round(per_night, 2),
            "total_stay_usd": round(total_with_fees, 2),
            "refundable": refundable,
            # cancellation_phrase is consumed by _flatten_rooms_to_rows'
            # mutation pass: it normalizes refundable on rate-plan dicts
            # before pick_canonical_bar / pick_canonical_flex run. Setting
            # it here keeps Synxis-path rate plans consistent with the
            # Firecrawl path, where cancellation_phrase is captured
            # verbatim from the booking page.
            "cancellation_phrase": cancellation_phrase or "",
            "availability_status": (
                "available" if bool(entry.get("Available", False))
                else "sold_out"
            ),
        })

    rooms: list[dict] = []
    for room_code, rate_plans in rooms_map.items():
        rooms.append({
            "marketing_name": room_lookup.get(room_code, room_code),
            "rate_plans": rate_plans,
        })

    extracted = {
        "property_name": property_name_for_payload,
        "arrival_date": check_in,
        "nights": los,
        "rooms": rooms,
    }
    return {"success": True, "data": {"markdown": "", "json": extracted},
            "error": ""}


# --- CLI test block ---
if __name__ == "__main__":
    test_dates = ["2026-04-28", "2026-07-02", "2027-01-20"]
    for date in test_dates:
        result = fetch_synxis_direct(
            date, 1,
            base_url="https://reservations.stayaka.com",
            hotel_id="56224",
            chain_id="27508",
        )
        print(f"\n=== {date} ===")
        if result["success"]:
            data_json = result["data"]["json"]
            rooms = data_json.get("rooms", [])
            print(f"Rooms returned: {len(rooms)}")
            for room in rooms:
                print(f"  {room['marketing_name']}")
                for rp in room["rate_plans"]:
                    refundable = rp.get("refundable")
                    refund_tag = (
                        "NRF" if refundable is False
                        else "FLEX" if refundable is True
                        else "?"
                    )
                    print(f"    [{refund_tag}] {rp['rate_plan_label']}: "
                          f"${rp['rate_per_night_usd']}/night, "
                          f"stay ${rp['total_stay_usd']}, "
                          f"{rp['availability_status']}")
        else:
            print(f"FAILED: {result['error']}")
