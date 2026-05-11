"""URL smoke-test harness — Resolution 9 of the rm-dashboard-rollout skill.

Every booking URL template (registry-sourced AND user-provided) is smoke-tested
against a known-good arrival date before being locked into ``config.json``.
The skill renders the URL with the test date, runs one Firecrawl extract,
surfaces the result (room count + extracted rates + status), and asks the
user for explicit confirmation before proceeding.

Why this exists
---------------
Registry entries can be wrong on first authoring. The 2026-05-07 Hyatt
registry-entry correction caught a draft ``/shop/?arrive=`` template before
it was locked into config — a Chrome network-panel check on
``hyatt.com/shop/rooms/{slug}?checkinDate=...`` revealed the right format.
Trusting the registry blindly produces a build pipeline that scrapes the
wrong endpoint for hours before failing — exactly the silent
under-extraction failure mode that Resolution 5 (`expected_room_count`
recon) is designed to prevent.

Verify-before-lock applies the same defensive pattern at the URL layer:
~30 seconds of user-confirmation per channel × per property at scaffold
time prevents hours of rework downstream.

Contract
--------
``smoke_test_url()`` issues a single Firecrawl extract against the rendered
URL and returns a structured ``SmokeTestResult`` the skill surfaces to the
user. The result distinguishes:

  - ``ok``                    — URL works, expected room count present.
  - ``url_broken``            — 404 / 5xx / bot-block / DataDome / Imperva /
                                page lacks expected-property identity.
  - ``no_inventory``          — URL works, page renders, but no inventory
                                for the test date. Acceptable iff
                                ``expected_present=False`` (caller sets
                                this when the test date is intentionally
                                a sold-out cell).
  - ``extraction_incomplete`` — extracted ``room_count_extracted`` is
                                less than ``expected_room_count``.
  - ``bot_blocked``           — bot-block signature in markdown.

The skill's invocation flow calls ``smoke_test_url()`` on every URL
template before writing ``config.json``. The result is presented to the
user with the prompt:

    Does this look right? [confirm / edit template / abort]

User confirms → template is locked in. User edits → re-run smoke test.
User aborts → skill stops; no config.json is written.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests

# Lazy import path setup — validators.py lives next to this module when this
# file is copied into a per-deal repo's scraper_lib/.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from validators import (  # noqa: E402
    BROKEN_URL_SIGNATURES,
    NO_INVENTORY_SIGNATURES,
    check_bot_block,
    check_plausibility,
    classify_empty_inventory_page,
    VERDICT_FAIL_URL_BROKEN,
    VERDICT_PASS_NO_INVENTORY,
)


SmokeStatus = str  # one of the literals below; aliased for type-hint readability

STATUS_OK: SmokeStatus = "ok"
STATUS_URL_BROKEN: SmokeStatus = "url_broken"
STATUS_NO_INVENTORY: SmokeStatus = "no_inventory"
STATUS_EXTRACTION_INCOMPLETE: SmokeStatus = "extraction_incomplete"
STATUS_BOT_BLOCKED: SmokeStatus = "bot_blocked"


@dataclass
class SmokeTestResult:
    """Structured output of a single smoke-test extract.

    The skill surfaces every field except ``raw_extract`` to the user; the
    raw extract is retained for debugging when status is ``url_broken`` or
    ``extraction_incomplete``.
    """

    status: SmokeStatus
    room_count_extracted: int
    sample_rates: list[dict[str, Any]]
    render_url_used: str
    elapsed_ms: int
    notes: list[str] = field(default_factory=list)
    raw_extract: Optional[dict[str, Any]] = None
    bot_block_signature: Optional[str] = None
    http_status: Optional[int] = None

    def is_pass(self) -> bool:
        """The skill should accept this template iff is_pass() is True OR
        the user explicitly waives a non-fatal warning (extraction_incomplete
        or no_inventory with expected_present=False)."""
        return self.status == STATUS_OK


# Same env / endpoint conventions as scraper_lib/scrape.py so callers don't
# need to plumb a separate Firecrawl client.
_FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v1/extract"
_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_WAIT_FOR_MS = 8000

# Minimal extract schema for smoke testing — enough to count rooms and capture
# a few sample rates, but cheaper than the full production EXTRACT_SCHEMA.
_SMOKE_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "property_name": {"type": "string"},
        "rooms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "marketing_name": {"type": "string"},
                    "rate_per_night_usd": {"type": "number"},
                },
            },
        },
    },
    "required": ["rooms"],
}

_SMOKE_EXTRACT_PROMPT = (
    "Extract the visible room types and their headline nightly rates for "
    "this booking page. For each room shown, capture marketing_name verbatim "
    "and the lowest displayed nightly USD rate. If the page is a captcha, "
    "404, or has no inventory for the requested dates, return rooms:[]."
)


def smoke_test_url(
    rendered_url: str,
    expected_room_count: int,
    property_label: str,
    *,
    expected_property_tokens: Optional[list[str]] = None,
    expected_arrival_date: Optional[str] = None,
    channel: Optional[str] = None,
    expected_present: bool = True,
    wait_for_ms: int = _DEFAULT_WAIT_FOR_MS,
    stealth: bool = False,
    firecrawl_api_key: Optional[str] = None,
) -> SmokeTestResult:
    """Run one Firecrawl extract against ``rendered_url`` and return a
    ``SmokeTestResult`` the skill can surface to the user.

    Inputs:
      rendered_url             — the URL with arrival/depart already
                                 substituted in (no ``{arrive}`` placeholders).
      expected_room_count      — the room count the user verified via
                                 Chrome MCP for this channel × property.
      property_label           — display label for log readability ("Capital
                                 Hilton — direct" etc).
      expected_property_tokens — case-insensitive substrings that must
                                 appear in the page for it to identify as
                                 the right property. Defaults to None →
                                 identity guard skipped.
      expected_arrival_date    — ISO YYYY-MM-DD; ignored if None.
      channel                  — channel id (``"direct"``, ``"booking"``, ...);
                                 enables Booking-specific bare-integer rate
                                 anchoring in the plausibility gate.
      expected_present         — True (default) when the test date should
                                 have inventory. Set False when smoke-testing
                                 a known sold-out date intentionally.
      wait_for_ms              — Firecrawl waitFor parameter (Synxis needs
                                 ≥15000ms; OTA channels typically <8000ms).
      stealth                  — pass through to Firecrawl proxy="stealth".
      firecrawl_api_key        — explicit key; falls back to
                                 ``FIRECRAWL_API_KEY`` env var.

    Returns: ``SmokeTestResult``. The skill prompts the user for confirm /
    edit / abort based on result.status.

    Side effects: one Firecrawl extract call (5 credits basic, 25 credits
    stealth).
    """
    api_key = firecrawl_api_key or os.environ.get("FIRECRAWL_API_KEY") or ""
    if not api_key:
        return SmokeTestResult(
            status=STATUS_URL_BROKEN,
            room_count_extracted=0,
            sample_rates=[],
            render_url_used=rendered_url,
            elapsed_ms=0,
            notes=[
                "FIRECRAWL_API_KEY not set; cannot smoke-test — fix env "
                "and re-run before locking config.json."
            ],
        )

    payload: dict[str, Any] = {
        "url": rendered_url,
        "schema": _SMOKE_EXTRACT_SCHEMA,
        "prompt": _SMOKE_EXTRACT_PROMPT,
        "waitFor": wait_for_ms,
    }
    if stealth:
        payload["proxy"] = "stealth"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    started = time.monotonic()
    notes: list[str] = []
    http_status: Optional[int] = None
    try:
        resp = requests.post(
            _FIRECRAWL_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
        http_status = resp.status_code
        if resp.status_code in (404, 410) or 500 <= resp.status_code < 600:
            return SmokeTestResult(
                status=STATUS_URL_BROKEN,
                room_count_extracted=0,
                sample_rates=[],
                render_url_used=rendered_url,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                notes=[f"HTTP {resp.status_code} from Firecrawl proxy"],
                http_status=http_status,
            )
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as exc:
        return SmokeTestResult(
            status=STATUS_URL_BROKEN,
            room_count_extracted=0,
            sample_rates=[],
            render_url_used=rendered_url,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            notes=[f"Firecrawl request failed: {exc!s}"],
            http_status=http_status,
        )
    elapsed_ms = int((time.monotonic() - started) * 1000)

    data = body.get("data") or {}
    extract = data.get("json") or data.get("extract") or {}
    markdown = data.get("markdown") or ""

    # Bot-block check on raw markdown — runs before LLM trust per Rule 1.
    bot_sig = check_bot_block(markdown)
    if bot_sig:
        return SmokeTestResult(
            status=STATUS_BOT_BLOCKED,
            room_count_extracted=0,
            sample_rates=[],
            render_url_used=rendered_url,
            elapsed_ms=elapsed_ms,
            notes=[f"bot-block signature matched: {bot_sig!r}"],
            raw_extract=extract,
            bot_block_signature=bot_sig,
            http_status=http_status,
        )

    rooms = extract.get("rooms") or []
    if not isinstance(rooms, list):
        rooms = []

    # Empty extract → distinguish PASS_NO_INVENTORY from FAIL_URL_BROKEN.
    if not rooms:
        cls = classify_empty_inventory_page(
            markdown,
            expected_property_tokens=expected_property_tokens or [],
        )
        if cls == VERDICT_FAIL_URL_BROKEN:
            return SmokeTestResult(
                status=STATUS_URL_BROKEN,
                room_count_extracted=0,
                sample_rates=[],
                render_url_used=rendered_url,
                elapsed_ms=elapsed_ms,
                notes=[
                    "empty extract + broken-URL signature OR missing "
                    "expected-property identity in markdown"
                ],
                raw_extract=extract,
                http_status=http_status,
            )
        if cls == VERDICT_PASS_NO_INVENTORY:
            note = (
                "URL works, page renders, no inventory for the test date — "
                + ("expected (expected_present=False)" if not expected_present
                   else "BUT expected_present=True; pick a different test "
                        "date or verify the cell is genuinely sold out")
            )
            return SmokeTestResult(
                status=STATUS_NO_INVENTORY,
                room_count_extracted=0,
                sample_rates=[],
                render_url_used=rendered_url,
                elapsed_ms=elapsed_ms,
                notes=[note],
                raw_extract=extract,
                http_status=http_status,
            )
        # Empty extract with no clear signal — default to url_broken so the
        # skill prompts the user for verification rather than silently
        # accepting a placeholder URL.
        return SmokeTestResult(
            status=STATUS_URL_BROKEN,
            room_count_extracted=0,
            sample_rates=[],
            render_url_used=rendered_url,
            elapsed_ms=elapsed_ms,
            notes=[
                "empty extract with no inventory signal AND no broken-URL "
                "signature — treating as url_broken (placeholder URL?). "
                "Verify in browser before locking template."
            ],
            raw_extract=extract,
            http_status=http_status,
        )

    # Plausibility gate (rate bounds, identity guard, rate-anchor check).
    plausibility_passed, plausibility_reason = check_plausibility(
        extract,
        expected_property_tokens=expected_property_tokens or [],
        expected_arrival_date=expected_arrival_date or "",
        markdown=markdown,
        channel=channel,
    )
    if not plausibility_passed:
        notes.append(f"plausibility: {plausibility_reason}")

    # Sample rates (first 5 rooms) for the user-facing summary.
    sample_rates: list[dict[str, Any]] = []
    for r in rooms[:5]:
        if not isinstance(r, dict):
            continue
        sample_rates.append({
            "marketing_name": r.get("marketing_name"),
            "rate_per_night_usd": r.get("rate_per_night_usd"),
        })

    room_count = len(rooms)
    if room_count < expected_room_count:
        notes.append(
            f"expected {expected_room_count} rooms for "
            f"{property_label}, extracted {room_count} — possible "
            "compression, anti-bot truncation, or template drift"
        )
        return SmokeTestResult(
            status=STATUS_EXTRACTION_INCOMPLETE,
            room_count_extracted=room_count,
            sample_rates=sample_rates,
            render_url_used=rendered_url,
            elapsed_ms=elapsed_ms,
            notes=notes,
            raw_extract=extract,
            http_status=http_status,
        )

    # Happy path — at least the expected room count and no bot block.
    return SmokeTestResult(
        status=STATUS_OK,
        room_count_extracted=room_count,
        sample_rates=sample_rates,
        render_url_used=rendered_url,
        elapsed_ms=elapsed_ms,
        notes=notes,
        raw_extract=extract,
        http_status=http_status,
    )


def format_user_facing_summary(result: SmokeTestResult) -> str:
    """Render a SmokeTestResult into a 6-line summary block the skill prints
    to the user when prompting for confirm / edit / abort.

    Format:
        URL:     {render_url_used}
        Status:  {status} ({elapsed_ms} ms)
        Rooms:   {room_count_extracted} extracted
        Sample:  {marketing_name_1}: ${rate_1}, ...
        Notes:   {notes joined with "; "}
        Decide:  [confirm / edit template / abort]
    """
    sample = ", ".join(
        f"{r.get('marketing_name', '?')}: ${r.get('rate_per_night_usd', '—')}"
        for r in result.sample_rates[:3]
    ) or "—"
    notes = "; ".join(result.notes) if result.notes else "—"
    return (
        f"URL:     {result.render_url_used}\n"
        f"Status:  {result.status} ({result.elapsed_ms} ms)\n"
        f"Rooms:   {result.room_count_extracted} extracted\n"
        f"Sample:  {sample}\n"
        f"Notes:   {notes}\n"
        f"Decide:  [confirm / edit template / abort]"
    )
