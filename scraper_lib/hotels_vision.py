"""Hotels.com extraction via Firecrawl screenshot + Anthropic vision (Phase A2.5).

Why this exists
---------------
Phase A2.3 proved that Firecrawl's markdown serializer is non-deterministic
against Hotels.com's per-room rate-tier expander: 4 multi-scroll combos,
~100 cr each, none reliably reproduced the full ladder. The wiki's
2026-04-26 A2.3 entry called it: "Phase A2.5 will move Hotels.com to
Firecrawl screenshot + Anthropic vision API to bypass the markdown-render-
timing problem entirely."

Why not Playwright-DOM-first
----------------------------
Phase A2.5 probe (2026-04-27): headless Chromium hits ERR_HTTP2_PROTOCOL_ERROR
at the network layer. Headed Chromium with playwright-stealth loaded the
property page on first visit but degraded to a soft-404 within minutes —
fingerprint-triggered. Operating a persistent Chrome profile to maintain
a clean session is high-friction; Firecrawl's stealth proxy already passes
Hotels.com's bot check today (the only failure was the markdown serializer).
Vision on the rendered screenshot reads what a user sees, regardless of
how Firecrawl's serializer collapsed the DOM.

Output contract
---------------
firecrawl_scrape_via_vision returns the SAME shape as firecrawl_scrape_json:
    {"success": bool, "data": {"markdown": str, "json": dict|None},
     "error": str (when success=False)}
so the existing gate-1..5 pipeline in scrape.py applies unchanged. Markdown
still anchors gate-3 (room-type verbatim) and gate-5 (variance); json is
the vision-extracted rooms.
"""
from __future__ import annotations
import os, json, time, base64, io
from typing import Optional
import requests

import anthropic
from PIL import Image

# Keys read lazily inside each function (not at module import) because
# scrape.py imports this module BEFORE it calls load_dotenv() on its .env.
# Reading at module-level captured "" and broke production runs.
def _firecrawl_key() -> str:
    return os.environ.get("FIRECRAWL_API_KEY") or ""

def _anthropic_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY") or ""

VISION_MODEL = "claude-haiku-4-5"
VISION_MAX_TOKENS = 4096

# Anthropic vision rejects images where any dimension exceeds 8000 px.
# Hotels.com full-page screenshots commonly exceed this on the long axis
# (rate ladder + reviews + footer). Resize to fit with safety margin.
ANTHROPIC_MAX_DIM = 7800

# Tool schema mirrors EXTRACT_SCHEMA["properties"] in scrape.py — kept in
# sync so the same downstream code (_flatten_rooms_to_rows, picker, gates)
# consumes vision output and Firecrawl-LLM output identically.
ROOMS_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "property_name": {"type": "string"},
        "arrival_date": {"type": "string"},
        "nights": {"type": "integer"},
        "rooms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "marketing_name": {"type": "string"},
                    "sub_description": {"type": "string"},
                    "bed_config": {"type": "string"},
                    "occupancy_max": {"type": "integer"},
                    "rate_plans": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "rate_plan_label": {"type": "string"},
                                "rate_per_night_usd": {"type": "number"},
                                "total_stay_usd": {"type": "number"},
                                "refundable": {"type": "boolean"},
                                "is_genius_member_rate": {"type": "boolean"},
                                "includes_breakfast": {"type": "boolean"},
                                "rate_plan_confidence": {"type": "string"},
                                "availability_status": {"type": "string"},
                                "cancellation_phrase": {"type": "string"},
                                "strikethrough_orig_rate": {"type": "number"},
                            },
                            "required": ["rate_plan_label", "rate_per_night_usd"],
                        },
                    },
                },
                "required": ["marketing_name", "rate_plans"],
            },
        },
    },
    "required": ["property_name", "rooms"],
}


def firecrawl_scrape_screenshot(
    url: str, *, wait_for_ms: int, actions: Optional[list[dict]] = None,
    timeout_ms: int = 0, http_retries: int = 2,
) -> dict:
    """Firecrawl: stealth-proxy fetch returning markdown + full-page screenshot.

    Returns the raw Firecrawl response. screenshot URL lives at
    response["data"]["screenshot"] when success=True.
    """
    fc_key = _firecrawl_key()
    if not fc_key:
        raise RuntimeError("FIRECRAWL_API_KEY missing from .env")
    payload = {
        "url": url,
        # screenshot@fullPage captures the entire scrolled page as a single
        # image; markdown is kept so gates 1, 3, 5 still have their anchor.
        "formats": ["markdown", "screenshot@fullPage"],
        "waitFor": wait_for_ms,
        "proxy": "stealth",
    }
    if actions:
        payload["actions"] = actions
    if timeout_ms > 0:
        payload["timeout"] = timeout_ms
    elif wait_for_ms > 15000:
        payload["timeout"] = 2 * wait_for_ms

    headers = {"Authorization": f"Bearer {fc_key}",
               "Content-Type": "application/json"}
    last_err = ""
    for attempt in range(http_retries + 1):
        try:
            r = requests.post("https://api.firecrawl.dev/v1/scrape",
                              headers=headers, json=payload, timeout=240)
            if r.status_code >= 500 and attempt < http_retries:
                time.sleep(3 + attempt * 3)
                last_err = f"http {r.status_code}"
                continue
            ct = r.headers.get("content-type", "")
            if ct.startswith("application/json"):
                return r.json()
            return {"success": False, "error": r.text[:2000]}
        except Exception as ex:
            last_err = f"exc: {ex}"
            if attempt < http_retries:
                time.sleep(5)
                continue
            return {"success": False, "error": last_err}
    return {"success": False, "error": last_err}


def _fetch_screenshot_bytes(screenshot_ref: str) -> tuple[Optional[bytes], str]:
    """Resolve Firecrawl's screenshot field to raw image bytes. Firecrawl
    has historically returned either an HTTP URL or a `data:image/png;base64,...`
    URI; handle both. Returns (bytes, "") on success, (None, "reason") on
    failure."""
    if not screenshot_ref:
        return None, "empty_screenshot_ref"
    if screenshot_ref.startswith("data:"):
        # data:image/png;base64,iVBOR...
        try:
            _, b64 = screenshot_ref.split(",", 1)
            return base64.b64decode(b64), ""
        except Exception as ex:
            return None, f"data_uri_decode_failed: {ex}"
    if screenshot_ref.startswith(("http://", "https://")):
        try:
            r = requests.get(screenshot_ref, timeout=60)
            if r.status_code != 200:
                return None, f"screenshot_fetch_http_{r.status_code}"
            return r.content, ""
        except Exception as ex:
            return None, f"screenshot_fetch_exc: {ex}"
    return None, f"unknown_screenshot_ref_scheme: {screenshot_ref[:30]!r}"


def _prepare_image_for_anthropic(raw_bytes: bytes) -> tuple[Optional[str], str, str]:
    """Open image bytes, downscale if either dimension exceeds Anthropic's
    8000 px limit (with safety margin), re-encode as PNG, return base64.

    Returns (b64_str, media_type, ""). On failure: (None, "", "reason").
    Hotels.com full-page screenshots are commonly ~1280 × 12000+ px; this
    resize is the difference between a working call and a 400 from
    image.source.base64.data dimension validation.
    """
    try:
        img = Image.open(io.BytesIO(raw_bytes))
    except Exception as ex:
        return None, "", f"pillow_open_failed: {ex}"
    w, h = img.size
    longest = max(w, h)
    if longest > ANTHROPIC_MAX_DIM:
        scale = ANTHROPIC_MAX_DIM / longest
        new_w = max(1, int(w * scale))
        new_h = max(1, int(h * scale))
        img = img.resize((new_w, new_h), Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/png", ""


def extract_rooms_from_screenshot(
    screenshot_url: str, *, prompt: str, model: str = VISION_MODEL,
    max_tokens: int = VISION_MAX_TOKENS,
) -> tuple[Optional[dict], str]:
    """Vision call: download the Firecrawl screenshot, downscale to fit
    Anthropic's 8000 px limit, send as base64 image to claude-haiku-4-5
    with a structured-output tool. Returns (extracted_dict, error_str).

    On success: (dict, ""). On failure: (None, "reason"). Caller maps
    failure to the existing extract_failed status.
    """
    ant_key = _anthropic_key()
    if not ant_key:
        return None, "ANTHROPIC_API_KEY missing from .env"

    raw, fetch_err = _fetch_screenshot_bytes(screenshot_url)
    if raw is None:
        return None, f"fetch: {fetch_err}"
    b64, media_type, prep_err = _prepare_image_for_anthropic(raw)
    if b64 is None:
        return None, f"prepare: {prep_err}"

    client = anthropic.Anthropic(api_key=ant_key)
    image_block = {"type": "image",
                   "source": {"type": "base64",
                              "media_type": media_type,
                              "data": b64}}

    tool = {"name": "submit_room_extraction",
            "description": ("Submit the structured room + rate-plan extraction "
                            "from the Hotels.com property page screenshot."),
            "input_schema": ROOMS_TOOL_SCHEMA}

    try:
        resp = client.messages.create(
            model=model, max_tokens=max_tokens,
            tools=[tool],
            tool_choice={"type": "tool", "name": "submit_room_extraction"},
            messages=[{"role": "user", "content": [
                image_block,
                {"type": "text", "text": prompt},
            ]}],
        )
    except Exception as ex:
        return None, f"anthropic_api_error: {type(ex).__name__}: {ex}"

    # Tool-use forces structured JSON in tool_use blocks.
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            data = block.input
            if isinstance(data, dict):
                return data, ""
    return None, "no_tool_use_block_in_response"


def firecrawl_scrape_via_vision(
    url: str, *, stealth: bool, wait_for_ms: int, schema: dict, prompt: str,
    actions: Optional[list[dict]] = None, timeout_ms: int = 0,
    http_retries: int = 2,
) -> dict:
    """Drop-in for firecrawl_scrape_json on Hotels.com cells.

    `stealth` and `schema` are accepted for signature parity but ignored:
    Firecrawl screenshot is always fetched with stealth proxy, and the
    schema is hard-coded inside the vision tool (kept in sync with
    EXTRACT_SCHEMA via ROOMS_TOOL_SCHEMA above).
    """
    fc_resp = firecrawl_scrape_screenshot(
        url, wait_for_ms=wait_for_ms, actions=actions,
        timeout_ms=timeout_ms, http_retries=http_retries,
    )
    if not fc_resp.get("success"):
        return fc_resp  # already shaped {success: False, error: ...}

    data = fc_resp.get("data") or {}
    md = data.get("markdown") or ""
    screenshot_url = data.get("screenshot") or ""
    if not screenshot_url:
        return {"success": False,
                "error": "firecrawl_returned_no_screenshot_url",
                "data": {"markdown": md, "json": None}}

    extracted, err = extract_rooms_from_screenshot(
        screenshot_url, prompt=prompt,
    )
    if extracted is None:
        return {"success": False, "error": f"vision_extraction_failed: {err}",
                "data": {"markdown": md, "json": None}}

    return {"success": True,
            "data": {"markdown": md, "json": extracted,
                     "screenshot_url": screenshot_url}}
