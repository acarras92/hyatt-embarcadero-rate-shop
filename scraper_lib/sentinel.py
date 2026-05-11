"""Lighthouse cell-value normalizer — string sentinels → typed enum.

Why this exists
---------------
Lighthouse `Rates` sheet cells interleave numeric rates with string sentinels
(`Sold out`, `No flex`, `--`, `LOS2`, `LOS3`, `Room n/a`, `3rd party only`,
`1 guest only`). The downstream `analytics_lighthouse.py` consumes a long-
format CSV with `availability_status` already enum-typed and `rate_usd` as
nullable float, which means the XLSX→CSV preprocessing step must do the
sentinel→enum mapping. Coercing sentinels with bare `float()` either
TypeErrors or silently NaN-coerces; both produce contaminated downstream
aggregations with no audit trail.

This module is the pure, deal-agnostic, cell-level mapper. The Lighthouse-
specific XLSX walker (header detection, property-column discovery, drop-
columns, anchor-validate) lives in the per-deal `scripts/lighthouse_ingest.py`
that imports `normalize_cell` from here.

Sentinel taxonomy (Resolution 15, 2026-05-08)
---------------------------------------------
Lifted verbatim from the battle-tested AKA `Lighthouse/lighthouse_parser.py`
`_normalize_cell()` after a count of every observed sentinel in real
SFOEM exports (3 LOS files × 366 forward dates × 10 properties) returned
zero unknown strings against this taxonomy. The taxonomy IS the contract
the downstream analytics already consumes — see
`analytics_lighthouse.py._exclusions_breakdown()`.

Output enum values:
    available           — numeric rate; rate_usd populated.
    blank               — None, NaN, empty string, or numeric zero. (Zero is
                          treated as blank because Lighthouse doesn't post
                          $0 BAR; a 0 in the cell means "missing" not "free".)
    sold_out            — verbatim "Sold out" (case-insensitive).
    no_flex             — verbatim "No flex".
    room_na             — "Room n/a" or "Room na" — the property doesn't
                          offer a comparable room type for this date.
    not_loaded          — "--" — rate not yet loaded by the property; distinct
                          from sold_out (genuinely no rate published, vs
                          inventory exhausted).
    third_party_only    — "3rd party only" — Brand.com signal that the
                          property is not selling Direct on this date.
    one_guest_only      — "1 guest only" — Booking.com inventory mismatch
                          for the requested 2-guest occupancy.
    los_restricted      — "LOS{n}" pattern (LOS2, LOS3, ...). Sets
                          los_restriction=int(n) on the row; the n captures
                          the minimum-LOS the property is gating to.

Unknown strings get tagged as f"unknown_sentinel:{raw}" so they surface in
diagnostics rather than silently coercing to NaN. Callers should pass an
`unknown_sentinels` Counter and surface its non-empty state at end of parse.

Why not extend the taxonomy
---------------------------
Real-data audit (SFOEM 2026-05-08): zero unknown strings observed across the
LOS=1, LOS=3, LOS=7 files. The existing taxonomy is complete for the deals
the skill targets. Adding speculative values (CLOSED_TO_ARRIVAL, MIN_LOS,
MAX_LOS) would diverge from the AKA byte-match regression without serving
any observed data. Future deals that DO surface a new string will tag it as
unknown_sentinel and the parse log will surface it for the analyst to add
explicitly.
"""
from __future__ import annotations

import re
from typing import Counter, Iterable, Mapping, Optional, Tuple

import pandas as pd

# ---------------------------------------------------------------------------
# Sentinel string sets (case-insensitive matches except NOT_LOADED and LOS).
# Single source of truth — duplicated as a runtime guard nowhere else.
# ---------------------------------------------------------------------------

SOLD_OUT_VALUES = {"sold out"}
NO_FLEX_VALUES = {"no flex"}
ROOM_NA_VALUES = {"room n/a", "room na"}
NOT_LOADED_VALUES = {"--"}  # exact match (not lowercased) — distinct from "—"
THIRD_PARTY_ONLY_VALUES = {"3rd party only"}
ONE_GUEST_ONLY_VALUES = {"1 guest only"}
LOS_RE = re.compile(r"^LOS(\d+)$", re.IGNORECASE)

# Enum values exposed for type-narrowing in callers / tests. Mirrors the
# `_exclusions_breakdown` keys in the analytics layer (less "available" since
# that one is the happy path).
AVAILABILITY_STATUSES = (
    "available", "blank", "sold_out", "no_flex", "room_na", "not_loaded",
    "third_party_only", "one_guest_only", "los_restricted",
)


def normalize_cell(
    raw_value,
    unknown_sentinels: Optional[Counter] = None,
) -> Tuple[Optional[float], str, Optional[int]]:
    """Map one raw cell to (rate_usd, availability_status, los_restriction).

    Parameters
    ----------
    raw_value
        Whatever openpyxl/pandas returned for the cell — float, int, str,
        None, or NaN. Never mutated.
    unknown_sentinels
        Optional Counter; if provided, unrecognized non-numeric strings are
        counted in place. Caller is expected to surface the Counter's state
        at end of parse so new sentinels become visible rather than silently
        absorbed as `unknown_sentinel:{raw}` tags.

    Returns
    -------
    (rate_usd, availability_status, los_restriction)
        rate_usd: float when status == "available", else None.
        availability_status: one of AVAILABILITY_STATUSES, OR
                             f"unknown_sentinel:{raw}" for diagnostic surfacing.
        los_restriction: int (the n in LOS{n}) when status == "los_restricted",
                         else None.

    Behavior parity
    ---------------
    Byte-for-byte compatible with AKA's
    `Projects/AKA White House/Lighthouse/lighthouse_parser._normalize_cell`,
    which is the implicit upstream contract `analytics_lighthouse.py` was
    built against. A change here that would diverge from that parser is a
    contract break — re-validate against AKA before shipping.
    """
    if raw_value is None or (isinstance(raw_value, float) and pd.isna(raw_value)):
        return None, "blank", None
    if isinstance(raw_value, (int, float)):
        # Lighthouse doesn't post $0 BAR — zero means "missing", not "free".
        if raw_value == 0:
            return None, "blank", None
        return float(raw_value), "available", None
    if isinstance(raw_value, str):
        s = raw_value.strip()
        if not s:
            return None, "blank", None
        s_low = s.lower()
        if s_low in SOLD_OUT_VALUES:
            return None, "sold_out", None
        if s_low in NO_FLEX_VALUES:
            return None, "no_flex", None
        if s_low in ROOM_NA_VALUES:
            return None, "room_na", None
        if s in NOT_LOADED_VALUES:
            return None, "not_loaded", None
        if s_low in THIRD_PARTY_ONLY_VALUES:
            return None, "third_party_only", None
        if s_low in ONE_GUEST_ONLY_VALUES:
            return None, "one_guest_only", None
        m = LOS_RE.match(s)
        if m:
            return None, "los_restricted", int(m.group(1))
        if unknown_sentinels is not None:
            unknown_sentinels[s] = unknown_sentinels.get(s, 0) + 1
        return None, f"unknown_sentinel:{s}", None
    if unknown_sentinels is not None:
        key = repr(raw_value)
        unknown_sentinels[key] = unknown_sentinels.get(key, 0) + 1
    return None, f"unknown_sentinel:{raw_value!r}", None


def to_float_or_none(v) -> Optional[float]:
    """Coerce a metadata-column cell (Market demand / Market OTB) to float.

    Used by the ingest walker for non-rate columns whose contract is
    "numeric or missing" — no sentinel taxonomy needed.
    """
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Lighthouse slug-map drift detection (Resolution 20, 2026-05-09)
# ---------------------------------------------------------------------------

class LighthouseSlugMapDriftError(Exception):
    """Raised when an XLSX column is neither in the slug map, the drop
    list, nor a known metadata column.

    Why this is fail-loud (Finding 26, 2026-05-08 SFOEM dry-run pass 2)
    -------------------------------------------------------------------
    The previous behavior was `log.warning(...)` + silent skip. The
    SFOEM dry-run discovered that 5 of 9 drafted slug-map keys would
    have failed silent dict lookups against the actual XLSX headers
    due to verbatim-string drift between the parameter-collection
    drafts and the export's true column names — uppercase variants
    (`SoMa` vs `SOMA`), suffix changes (`Union Square` appended on
    the export), and brand suffixes (`by IHG`). Under silent-skip, the
    long-format CSV would simply not contain those properties and the
    downstream comp-set verdicts would render with phantom-empty rows.
    Raising at parse time forces the analyst to reconcile the slug
    map against the export before the dashboard is built — better to
    fail at ingest than to ship a dashboard missing comps.
    """


_DEFAULT_LIGHTHOUSE_METADATA_COLS = frozenset(
    {"Day", "Date", "My OTB", "Market OTB", "Market demand"}
)


def validate_lighthouse_slug_map_coverage(
    headers: Iterable,
    slug_map: Mapping[str, str],
    drop_columns: Iterable[str],
    metadata_cols: Optional[Iterable[str]] = None,
) -> None:
    """Confirm every XLSX header maps to slug_map, drop_columns, or known
    metadata. Raises `LighthouseSlugMapDriftError` listing every unmapped
    column when at least one is found.

    Parameters
    ----------
    headers
        Iterable of column headers parsed from the XLSX `Rates` sheet
        header row. Non-string entries and pandas-synthetic
        `Unnamed: N` placeholders are silently ignored.
    slug_map
        Lighthouse property header → canonical slug (i.e.
        `LIGHTHOUSE_PROPERTY_SLUG`).
    drop_columns
        Headers explicitly dropped at the parser layer (host-account
        column, `My OTB`, etc.).
    metadata_cols
        Known non-property metadata columns. Defaults to
        `{"Day", "Date", "My OTB", "Market OTB", "Market demand"}`.

    Notes
    -----
    Pure function: no I/O, no logging side effects. The caller (the
    rendered `lighthouse_ingest.py`) decides whether to translate the
    raised exception into a process-level fail or a structured report.
    """
    drop_columns_set = set(drop_columns)
    metadata_set = (
        set(metadata_cols) if metadata_cols is not None
        else set(_DEFAULT_LIGHTHOUSE_METADATA_COLS)
    )
    unmapped: list[str] = []
    for h in headers:
        if not isinstance(h, str):
            continue
        if h.startswith("Unnamed:"):
            continue
        if h in metadata_set:
            continue
        if h in drop_columns_set:
            continue
        if h in slug_map:
            continue
        unmapped.append(h)
    if unmapped:
        raise LighthouseSlugMapDriftError(
            f"Lighthouse XLSX contains {len(unmapped)} unmapped "
            f"column(s): {unmapped!r}. Either add them to "
            f"lighthouse_property_slug_map in config.json or to "
            f"lighthouse_drop_columns. Failing loudly per Finding 26 "
            f"(silent dict lookups would otherwise drop these "
            f"properties from the long-format CSV without a trace)."
        )


class LighthouseSlugMapPreflightError(Exception):
    """Raised at scaffold time when `lighthouse_property_slug_map` is
    structurally invalid for the deal's canonical slug set, before any
    XLSX has been seen.

    Why this is a sibling of LighthouseSlugMapDriftError (Resolution 22,
    2026-05-09)
    -----------------------------------------------------------------
    Resolution 20 catches drift between the slug_map and the actual XLSX
    headers — but only at ingest time, after the analyst has dropped
    Lighthouse exports into the deal directory. Resolution 22's per-deal
    render driver runs earlier — at scaffold render time — and can
    catch a different class of slug-map errors that don't need an XLSX
    to detect:

    - subject_slug not present in slug_map.values() — the rendered
      ingest will not emit the subject row even after a perfect XLSX.
    - comp_set slug missing from slug_map.values() — same problem for a
      named comp.
    - Duplicate values in slug_map — silent two-headers-into-one-slug
      collision.
    - slug_map keys overlapping drop_columns — column can't be both
      routed and dropped.
    - Orphan slug values (not subject, not in comp_set) — config drift.

    Pre-render check is a no-op when slug_map is empty (the documented
    degenerate AKA pattern from Resolution 18).
    """


def validate_slug_map_pre_render(
    slug_map: Mapping[str, str],
    drop_columns: Iterable[str],
    subject_slug: str,
    comp_slugs: Iterable[str],
) -> None:
    """Pre-render mirror of `validate_lighthouse_slug_map_coverage`.

    Runs at scaffold time before any XLSX is uploaded. Validates the
    slug map against the deal's *canonical* slug set (subject + comps)
    rather than against XLSX headers (the ingest-time validator catches
    header drift; this catches config-time drift).

    Skip-rule: empty slug_map is a no-op — covers the degenerate AKA
    pattern (Resolution 18, where Lighthouse column headers happen to
    match canonical slugs byte-for-byte).

    Parameters
    ----------
    slug_map
        Lighthouse property header → canonical slug.
    drop_columns
        Headers explicitly dropped at the parser layer.
    subject_slug
        Canonical slug for the subject property.
    comp_slugs
        Canonical slugs for every comp in `comp_set`.

    Raises
    ------
    LighthouseSlugMapPreflightError
        On any structural problem listed in the class docstring. The
        message names every offending slug / header so the analyst can
        reconcile config.json without running the ingest.
    """
    if not slug_map:
        return

    canonical_slugs = {subject_slug, *comp_slugs}
    slug_values = list(slug_map.values())
    drop_set = set(drop_columns)

    problems: list[str] = []

    if subject_slug not in slug_values:
        problems.append(
            f"subject_slug {subject_slug!r} not present in "
            f"lighthouse_property_slug_map.values() — the ingest will "
            f"not emit a subject row."
        )

    missing_comps = [c for c in comp_slugs if c not in slug_values]
    if missing_comps:
        problems.append(
            f"comp slug(s) missing from lighthouse_property_slug_map.values(): "
            f"{missing_comps!r}."
        )

    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for header, slug in slug_map.items():
        if slug in seen:
            duplicates.append(
                f"{slug!r} mapped from both {seen[slug]!r} and {header!r}"
            )
        else:
            seen[slug] = header
    if duplicates:
        problems.append(
            f"duplicate slug values (silent collision risk): {duplicates!r}."
        )

    overlap = [h for h in slug_map.keys() if h in drop_set]
    if overlap:
        problems.append(
            f"headers appearing in both slug_map and drop_columns "
            f"(can't be both routed and dropped): {overlap!r}."
        )

    orphan_slugs = [s for s in slug_values if s not in canonical_slugs]
    if orphan_slugs:
        problems.append(
            f"slug_map values not in canonical_slugs "
            f"(subject + comp_set): {orphan_slugs!r}."
        )

    if problems:
        raise LighthouseSlugMapPreflightError(
            "lighthouse_property_slug_map pre-flight failed:\n  - "
            + "\n  - ".join(problems)
        )
