"""One-shot loader: writes raw_rates.csv from Chrome-captured Hyatt Direct rates.

Background: Hyatt brand.com WAF-blocks Firecrawl (HTTP 403, ~74ms TTR).
On 2026-05-10 Andrew captured Sunday rates via Claude-in-Chrome for 8 dates.
This script encodes the captured data and emits rows matching raw_rates.csv
schema v2, reusing scrape.classify_room() so mapped_* fields are produced
the same way Firecrawl-path rows would be.

Run from repo root:  py scraper_lib/seed_chrome_captured_2026_05_10.py
"""

from __future__ import annotations

import csv
import datetime
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from canonical_maps import lookup_canonical_room  # noqa: E402
from schema import NEW_COLUMN_DEFAULTS, RAW_HEADER  # noqa: E402
from scrape import classify_room  # noqa: E402

PROPERTY_ID = "hr_embarcadero"
CHANNEL = "direct"
LOS = 1
CAPTURE_TS_UTC = "2026-05-10T22:00:00+00:00"

CONFIG_PATH = HERE / "config.json"
# raw_rates.csv lives at repo root — that's where build_dashboard.py reads from.
# (scrape.py writes to scraper_lib/raw_rates.csv — pre-existing path-drift bug;
# not touching scrape.py here since it can't run anyway while Hyatt WAF blocks.)
RAW_CSV_PATH = HERE.parent / "raw_rates.csv"


# Captured Sunday rates from Chrome session 2026-05-10.
# Key = arrival date (ISO). Value = list of (marketing_name, rate_usd).
# 2026-06-14 sold out entirely -> emits a no_inventory cell sentinel.
# Suites absent on dates below are sold out at the per-SKU level (no row).
CAPTURED: dict[str, list[tuple[str, int]]] = {
    "2026-05-17": [
        ("1 King Bed", 289),
        ("2 Queen Beds", 289),
        ("Accessible 2 Queen Beds with Tub", 289),
        ("1 King Bed Bay View", 309),
        ("2 Queen Beds Bay View", 309),
        ("1 King Bed Bay View Balcony", 339),
        ("2 Queen Beds City View Balcony", 339),
        ("1 King Water View", 359),
        ("1 King Bay View High Floor Balcony", 389),
        ("1 King Bed Club Access", 409),
        ("2 Double Beds Club Access", 444),
        ("Bay View Studio Suite", 609),
        ("Balcony Suite", 809),
        ("Luxury Suite", 1009),
        ("Presidential Suite", 1109),
    ],
    "2026-05-24": [
        ("1 King Bed", 289),
        ("Accessible 1 King Bed with Shower", 289),
        ("2 Queen Beds", 289),
        ("Accessible 2 Queen Beds with Tub", 289),
        ("Accessible 2 Queen Beds with Shower", 289),
        ("1 King Bed Bay View", 309),
        ("2 Queen Beds Bay View", 309),
        ("1 King Bed Bay View Balcony", 339),
        ("2 Queen Beds City View Balcony", 339),
        ("1 King Water View", 359),
        ("1 King Bay View High Floor Balcony", 389),
        ("1 King Bed Club Access", 409),
        ("2 Double Beds Club Access", 444),
        ("Bay View Studio Suite", 609),
        ("Balcony Suite", 809),
        ("Luxury Suite", 1009),
        ("Presidential Suite", 1109),
    ],
    "2026-05-31": [
        ("1 King Bed", 289),
        ("2 Queen Beds", 289),
        ("Accessible 2 Queen Beds with Tub", 289),
        ("Accessible 2 Queen Beds with Shower", 289),
        ("1 King Bed Bay View", 309),
        ("2 Queen Beds Bay View", 309),
        ("1 King Bed Bay View Balcony", 339),
        ("2 Queen Beds City View Balcony", 339),
        ("1 King Water View", 359),
        ("1 King Bay View High Floor Balcony", 389),
        ("1 King Bed Club Access", 409),
        ("2 Double Beds Club Access", 444),
        ("Bay View Studio Suite", 639),
        ("Balcony Suite", 839),
        ("Luxury Suite", 1039),
        ("Presidential Suite", 1139),
    ],
    "2026-06-07": [
        ("1 King Bed", 289),
        ("2 Queen Beds", 289),
        ("Accessible 2 Queen Beds with Tub", 289),
        ("Accessible 2 Queen Beds with Shower", 289),
        ("1 King Bed Bay View", 309),
        ("2 Queen Beds Bay View", 309),
        ("1 King Bed Bay View Balcony", 329),
        ("2 Queen Beds City View Balcony", 329),
        ("1 King Water View", 349),
        ("1 King Bay View High Floor Balcony", 379),
        ("1 King Bed Club Access", 369),
        ("2 Double Beds Club Access", 404),
        ("Bay View Studio Suite", 739),
        ("Balcony Suite", 939),
        ("Luxury Suite", 1139),
    ],
    "2026-06-21": [
        ("1 King Bed", 289),
        ("Accessible 1 King Bed with Tub", 289),
        ("1 King Bed Bay View", 311),
        ("1 King Bed Bay View Balcony", 329),
        ("1 King Water View", 349),
        ("1 King Bay View High Floor Balcony", 379),
        ("1 King Bed Club Access", 425),
        ("Bay View Studio Suite", 739),
        ("Balcony Suite", 939),
        ("Luxury Suite", 1139),
        ("Presidential Suite", 1239),
    ],
    "2026-06-28": [
        ("1 King Bed", 289),
        ("Accessible 1 King Bed with Tub", 289),
        ("Accessible 1 King Bed with Shower", 289),
        ("2 Queen Beds", 289),
        ("Accessible 2 Queen Beds with Tub", 289),
        ("1 King Bed Bay View", 309),
        ("2 Queen Beds Bay View", 309),
        ("1 King Bed Bay View Balcony", 329),
        ("2 Queen Beds City View Balcony", 329),
        ("1 King Water View", 349),
        ("1 King Bed Club Access", 370),
        ("1 King Bay View High Floor Balcony", 379),
        ("2 Double Beds Club Access", 405),
        ("Bay View Studio Suite", 739),
        ("Balcony Suite", 939),
        ("Luxury Suite", 1139),
    ],
    "2026-07-05": [
        ("1 King Bed", 319),
        ("Accessible 1 King Bed with Tub", 319),
        ("Accessible 1 King Bed with Shower", 319),
        ("2 Queen Beds", 319),
        ("Accessible 2 Queen Beds with Tub", 319),
        ("1 King Bed Bay View", 369),
        ("1 King Bed Bay View Balcony", 389),
        ("2 Queen Beds City View Balcony", 389),
        ("1 King Water View", 409),
        ("1 King Bay View High Floor Balcony", 439),
        ("1 King Bed Club Access", 469),
        ("2 Double Beds Club Access", 504),
        ("Bay View Studio Suite", 569),
        ("Balcony Suite", 769),
        ("Presidential Suite", 1069),
    ],
}

SOLD_OUT_DATES: tuple[str, ...] = ("2026-06-14",)


def _direct_url_template() -> str:
    cfg = json.loads(CONFIG_PATH.read_text())
    prop = next(p for p in cfg["properties"] if p["id"] == PROPERTY_ID)
    return prop["direct_url_template"]


def _source_url(template: str, arrival_iso: str, los: int) -> str:
    arr = datetime.date.fromisoformat(arrival_iso)
    dep = arr + datetime.timedelta(days=los)
    return template.format(arrive=arrival_iso, depart=dep.isoformat())


def _build_rate_row(arrival: str, name: str, rate: int, url_template: str) -> dict:
    canonical = lookup_canonical_room(name)
    if canonical is None:
        raise SystemExit(f"FAIL: '{name}' not in canonical_maps.ROOM_TYPE_CANONICAL")
    cls = classify_room(name, PROPERTY_ID)
    row: dict = {
        "property_id": PROPERTY_ID,
        "channel": CHANNEL,
        "arrival_date": arrival,
        "nights": LOS,
        "scraped_property_name": "Hyatt Regency San Francisco Embarcadero",
        "scraped_marketing_name": name,
        "sub_description": "",
        "bed_config": "",
        "occupancy_max": None,
        "rate_plan_label": "Standard Rate",
        "rate_per_night_usd": rate,
        "total_stay_usd": rate * LOS,
        "refundable": True,
        "is_genius_member_rate": False,
        "includes_breakfast": None,
        "rate_plan_confidence": "manual_chrome",
        "availability_status": "available",
        "is_bar": True,
        "mapped_internal_code": cls["internal_code"],
        "mapped_tier": cls["tier"],
        "mapped_view": cls["view"],
        "mapped_bedrooms": cls["bedrooms"],
        "mapped_ada": cls["ada"],
        "mapping_source": cls["mapping_source"],
        "status": "ok",
        "missed_patterns": "",
        "scrape_timestamp_utc": CAPTURE_TS_UTC,
        "source_url": _source_url(url_template, arrival, LOS),
    }
    row.update(NEW_COLUMN_DEFAULTS)
    row["room_type_canonical"] = canonical
    row["rate_plan_canonical"] = "BAR_FLEX"
    row["penthouse_variant"] = cls["penthouse_variant"]
    return row


def _build_no_inventory_row(arrival: str, url_template: str) -> dict:
    row: dict = {
        "property_id": PROPERTY_ID,
        "channel": CHANNEL,
        "arrival_date": arrival,
        "nights": LOS,
        "scraped_property_name": "",
        "scraped_marketing_name": "",
        "sub_description": "",
        "bed_config": "",
        "occupancy_max": None,
        "rate_plan_label": "",
        "rate_per_night_usd": None,
        "total_stay_usd": None,
        "refundable": None,
        "is_genius_member_rate": None,
        "includes_breakfast": None,
        "rate_plan_confidence": "",
        "availability_status": "no_inventory",
        "is_bar": False,
        "mapped_internal_code": "",
        "mapped_tier": "",
        "mapped_view": "",
        "mapped_bedrooms": None,
        "mapped_ada": None,
        "mapping_source": "",
        "status": "no_inventory",
        "missed_patterns": "fully sold out (chrome capture 2026-05-10)",
        "scrape_timestamp_utc": CAPTURE_TS_UTC,
        "source_url": _source_url(url_template, arrival, LOS),
    }
    row.update(NEW_COLUMN_DEFAULTS)
    row["rate_plan_canonical"] = "NO_INVENTORY"
    return row


def main() -> None:
    url_template = _direct_url_template()
    rows: list[dict] = []

    for arrival in sorted({*CAPTURED.keys(), *SOLD_OUT_DATES}):
        if arrival in SOLD_OUT_DATES:
            rows.append(_build_no_inventory_row(arrival, url_template))
            continue
        for name, rate in CAPTURED[arrival]:
            rows.append(_build_rate_row(arrival, name, rate, url_template))

    with open(RAW_CSV_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RAW_HEADER, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    n_rate = sum(1 for r in rows if r["status"] == "ok")
    n_no_inv = sum(1 for r in rows if r["status"] == "no_inventory")
    print(f"Wrote {RAW_CSV_PATH}")
    print(f"  rate rows: {n_rate}")
    print(f"  no_inventory rows: {n_no_inv}")
    print(f"  total rows: {len(rows)}")
    print(f"  dates covered: {sorted({r['arrival_date'] for r in rows})}")
    print(f"  unique SKUs: {sorted({r['room_type_canonical'] for r in rows if r['room_type_canonical']})}")


if __name__ == "__main__":
    main()
