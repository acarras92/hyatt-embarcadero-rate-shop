"""Tests for `scripts/render_new_deal.py` — the per-deal scaffold render
driver (Resolution 22, fix for Resolution 19).

Five tests:

1. AKA-byte-match-via-driver — render against `aka_params()`, confirm the 8
   AKA-equivalent files byte-match the live AKA repo. The driver must
   route through identical formatters as `regression_aka_render.py` —
   this test is the cross-check on Resolution 22 step 1's helper
   extraction.
2. Minimal-valid (Resolution 11 minimal) — empty SKU map + all SKU-
   derived empty + slug_map empty. Driver writes the complete output
   tree; no unrendered markers; all .py AST-parses; config.json
   JSON-parses (the driver itself validates these — the test asserts
   the file set and that no exception was raised).
3. Missing required parameter — drop a key, expect
   `ConfigPreflightError` with the key named.
4. Resolution 11 violation — empty SKUs + non-empty `view_pairs` raises.
5. Resolution 11 satisfied (Andrew's add) — empty SKUs + empty SKU-
   derived does NOT raise; driver completes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Wire up scripts/ + scraper_lib/ paths the same way the driver does at
# runtime. The repo is not installed; tests import via path manipulation.
SKILL_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _render_helpers import (  # noqa: E402
    render_lighthouse_drop_columns,
    render_lighthouse_property_slug_map,
    render_room_order_label,
    render_tier_steps,
    render_view_pairs,
)
from regression_aka_render import RENDER_PAIRS, aka_params, classify_drift  # noqa: E402
from render_new_deal import (  # noqa: E402
    ConfigPreflightError,
    render_new_deal_scaffold,
)

AKA_REPO = Path(
    r"C:/Users/acarr/OneDrive/Documents/Claude/Projects/aka-white-house-rate-shop"
)


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n")


def _aka_minimal_resolution_11_config() -> dict:
    """Take aka_params() and zero out all SKU-derived fields per
    Resolution 11. Used by tests 2 and 5."""
    config = dict(aka_params())  # shallow copy is fine — we replace top-level keys
    config["subject_canonical_skus"] = {}
    config["view_pairs"] = []
    config["tier_steps"] = []
    config["room_order_label"] = []
    config["marketing_name_by_channel"] = {}
    # Recompute the rendered blocks for the now-empty raw fields.
    config["view_pairs_rendered"] = render_view_pairs([])
    config["tier_steps_rendered"] = render_tier_steps([])
    config["room_order_label_rendered"] = render_room_order_label([])
    return config


# =============================================================================
# Test 1 — AKA byte-match via the new driver
# =============================================================================
@pytest.mark.skipif(
    not AKA_REPO.exists(),
    reason="AKA reference repo not present locally",
)
def test_aka_byte_match_via_driver(tmp_path):
    """Render against AKA params via the new driver; confirm byte-match
    against the live AKA repo for the 8 RENDER_PAIRS files. This is the
    cross-check that the new driver and `regression_aka_render.py` route
    through identical formatters — if they diverge, this test fails
    before the regression test would notice."""
    config = aka_params()
    summary = render_new_deal_scaffold(config, tmp_path)

    # Use the same classification the regression test uses — ZERO_DRIFT or
    # EXPECTED_DRIFT (narrative TODO slots) is acceptable; UNEXPECTED_DRIFT
    # is the gate.
    drift_summary: list[tuple[str, str]] = []
    for _tpl_rel, src_rel in RENDER_PAIRS:
        rendered = _normalize((tmp_path / src_rel).read_text(encoding="utf-8"))
        source = _normalize((AKA_REPO / src_rel).read_text(encoding="utf-8"))
        verdict, _diff = classify_drift(src_rel, rendered, source)
        drift_summary.append((src_rel, verdict))
        assert verdict in ("ZERO_DRIFT", "EXPECTED_DRIFT"), (
            f"Driver-rendered {src_rel} produced {verdict}; "
            f"AKA byte-match contract requires ZERO_DRIFT or EXPECTED_DRIFT."
        )

    # Cross-check against the regression-test verdict counts: post-driver
    # we should see exactly the same ZERO/EXPECTED distribution that
    # `py scripts/regression_aka_render.py` reports for the 8 RENDER_PAIRS
    # files (ZERO_DRIFT=4, EXPECTED_DRIFT=4 — 5th EXPECTED is the
    # canonical_maps shell handled outside RENDER_PAIRS).
    n_zero = sum(1 for _, v in drift_summary if v == "ZERO_DRIFT")
    n_expected = sum(1 for _, v in drift_summary if v == "EXPECTED_DRIFT")
    assert n_zero == 4, f"expected 4 ZERO_DRIFT, got {n_zero}: {drift_summary}"
    assert n_expected == 4, (
        f"expected 4 EXPECTED_DRIFT, got {n_expected}: {drift_summary}"
    )

    # lighthouse_ingest.py is rendered (Resolution 15) but not in
    # RENDER_PAIRS (Resolution 19) — assert it landed and is in the
    # validated set.
    ingest_path = tmp_path / "scripts" / "lighthouse_ingest.py"
    assert ingest_path in summary["rendered"]
    assert ingest_path in summary["validated"]
    assert ingest_path.exists()


# =============================================================================
# Test 2 — Minimal-valid Resolution-11 config produces complete tree
# =============================================================================
@pytest.mark.skipif(
    not AKA_REPO.exists(),
    reason="AKA reference repo not present (needed for config.json passthrough)",
)
def test_minimal_valid_config_produces_complete_tree(tmp_path):
    """Resolution 11 minimal — empty SKUs, all SKU-derived empty.
    Driver writes the complete output tree; post-render validation
    (AST-parse + JSON-parse + unrendered-marker scan) must pass for
    every rendered file (otherwise the driver would have raised)."""
    config = _aka_minimal_resolution_11_config()
    summary = render_new_deal_scaffold(config, tmp_path)

    expected_rendered = [
        "redeploy.py",
        "CLAUDE.md",
        "scraper_lib/scrape.py",
        "scraper_lib/config.json",
        "scraper_lib/canonical_maps.py",
        "analytics_lighthouse.py",
        "build_dashboard.py",
        "dashboard.js",
        "index.html",
        "scripts/lighthouse_ingest.py",
    ]
    for rel in expected_rendered:
        assert (tmp_path / rel).exists(), f"Missing rendered: {rel}"

    expected_copied_samples = [
        "scraper_lib/normalize.py",
        "scraper_lib/sentinel.py",
        "scraper_lib/synxis_api.py",
        "tests/conftest.py",
        "tests/test_sentinel.py",
        "verification/apply_chrome_verification.py",
        "scripts/screenshot_dashboard.py",
        "scripts/phase2_anchor_validate.py",
    ]
    for rel in expected_copied_samples:
        assert (tmp_path / rel).exists(), f"Missing copied: {rel}"

    # No __pycache__ / .pyc artifacts should have been copied.
    pycache_hits = list(tmp_path.rglob("__pycache__"))
    assert not pycache_hits, f"__pycache__ leaked into scaffold: {pycache_hits}"
    pyc_hits = list(tmp_path.rglob("*.pyc"))
    assert not pyc_hits, f".pyc files leaked into scaffold: {pyc_hits}"

    # Validation set equals rendered set in the happy path.
    assert summary["validated"] == summary["rendered"]
    assert len(summary["rendered"]) == len(expected_rendered)
    assert len(summary["copied"]) >= len(expected_copied_samples)


# =============================================================================
# Q7 — re-render leaves unrelated files alone (no rmtree, per-file clobber)
# =============================================================================
@pytest.mark.skipif(
    not AKA_REPO.exists(),
    reason="AKA reference repo not present (needed for config.json passthrough)",
)
def test_rerender_preserves_unrelated_files(tmp_path):
    """Q7 contract: the driver clobbers files it writes, but unrelated
    files in output_dir are left alone. A re-render into the same
    output_dir must (a) update the rendered+copied files, (b) leave
    user-placed sibling files intact."""
    config = _aka_minimal_resolution_11_config()

    # Pre-seed an unrelated top-level file and an unrelated nested file in
    # a directory the driver does NOT write into (avoids any path collision
    # with the scaffold tree).
    user_note = tmp_path / "user_notes.md"
    user_note.write_text("hand-edited notes — keep me\n", encoding="utf-8")
    user_dir = tmp_path / "user_data"
    user_dir.mkdir()
    user_data = user_dir / "scratch.txt"
    user_data.write_text("scratch content\n", encoding="utf-8")

    # First render — confirm the scaffold lands and unrelated files survive.
    render_new_deal_scaffold(config, tmp_path)
    assert user_note.read_text(encoding="utf-8") == "hand-edited notes — keep me\n"
    assert user_data.read_text(encoding="utf-8") == "scratch content\n"
    assert (tmp_path / "redeploy.py").exists()

    # Capture mtime of one rendered file to confirm re-render actually
    # rewrites it (clobber semantics, not skip-if-exists).
    redeploy = tmp_path / "redeploy.py"
    first_bytes = redeploy.read_bytes()
    # Mutate the rendered file so we can detect that re-render restores it.
    redeploy.write_text("// tampered\n", encoding="utf-8")
    assert redeploy.read_text(encoding="utf-8") == "// tampered\n"

    # Re-render — the tampered rendered file must be restored, and the
    # unrelated user files must still be intact.
    render_new_deal_scaffold(config, tmp_path)
    assert redeploy.read_bytes() == first_bytes, (
        "Q7: re-render must clobber files the driver writes."
    )
    assert user_note.read_text(encoding="utf-8") == "hand-edited notes — keep me\n", (
        "Q7: re-render must leave unrelated files alone."
    )
    assert user_data.read_text(encoding="utf-8") == "scratch content\n", (
        "Q7: re-render must leave unrelated nested files alone."
    )


# =============================================================================
# Test 3 — Missing required parameter raises
# =============================================================================
def test_missing_required_parameter_raises(tmp_path):
    """Pre-flight raises ConfigPreflightError with the missing key named.
    Doesn't depend on the AKA repo because pre-flight runs before any
    template render."""
    config = dict(aka_params())
    del config["subject_slug"]

    with pytest.raises(ConfigPreflightError, match="subject_slug"):
        render_new_deal_scaffold(config, tmp_path)


def test_missing_rendered_key_raises(tmp_path):
    """Same fail-loud surface for missing rendered keys."""
    config = dict(aka_params())
    del config["dashboard_js_compset_block"]

    with pytest.raises(ConfigPreflightError, match="dashboard_js_compset_block"):
        render_new_deal_scaffold(config, tmp_path)


# =============================================================================
# Test 4 — Resolution 11 contract violation raises
# =============================================================================
def test_resolution_11_violation_raises(tmp_path):
    """Empty subject_canonical_skus with non-empty view_pairs is the
    canonical Resolution 11 failure mode — pre-flight raises."""
    config = dict(aka_params())
    config["subject_canonical_skus"] = {}
    # view_pairs left non-empty intentionally — that's the violation.

    with pytest.raises(ConfigPreflightError, match="Resolution 11"):
        render_new_deal_scaffold(config, tmp_path)


# =============================================================================
# Test 5 — Resolution 11 contract satisfied does NOT raise
# =============================================================================
@pytest.mark.skipif(
    not AKA_REPO.exists(),
    reason="AKA reference repo not present (needed for config.json passthrough)",
)
def test_resolution_11_satisfied_does_not_raise(tmp_path):
    """Inverse of test 4 — empty SKUs WITH all SKU-derived emptied is
    the documented Resolution 11 happy path. Pre-flight must not raise."""
    config = _aka_minimal_resolution_11_config()

    # Should not raise.
    summary = render_new_deal_scaffold(config, tmp_path)

    assert summary["rendered"], "expected at least one rendered file"
    assert summary["copied"], "expected at least one copied file"


# =============================================================================
# Slug-map pre-flight (Resolution 22 mirror of Resolution 20) — non-AKA path
# =============================================================================
def _sfoem_shaped_config() -> dict:
    """Take aka_params() and overlay the SFOEM-shaped slug map + drop
    columns from RESOLUTIONS.md > Resolution 20 (the verbatim SFOEM
    XLSX header → canonical slug pairs that the SFOEM dry-run pass 2
    locked in). Subject + comp slugs are AKA's so the rest of the
    config stays renderable; only the Lighthouse-routing fields go
    SFOEM-shape. Used by the slug-map invariant tests."""
    config = dict(aka_params())
    # Use AKA slugs as canonical so the slug_map values reference real
    # subject + comp names. SFOEM-shaped headers (verbatim XLSX strings).
    slug_map = {
        "AKA White House (verbatim)": "aka_white_house",
        "Capital Hilton DC": "capital_hilton",
        "Hay-Adams Hotel": "hay_adams",
        "The Jefferson DC": "jefferson",
        "St. Regis Washington DC": "st_regis",
        "Willard InterContinental": "willard",
    }
    drop_columns = ["Park Central Hotel New York"]
    config["lighthouse_property_slug_map"] = slug_map
    config["lighthouse_drop_columns"] = drop_columns
    config["lighthouse_property_slug_map_rendered"] = (
        render_lighthouse_property_slug_map(slug_map)
    )
    config["lighthouse_drop_columns_rendered"] = (
        render_lighthouse_drop_columns(drop_columns)
    )
    return config


def test_slug_map_subject_missing_raises(tmp_path):
    config = _sfoem_shaped_config()
    # Drop subject from slug_map values → orphan
    sm = dict(config["lighthouse_property_slug_map"])
    sm["AKA White House (verbatim)"] = "wrong_slug"
    config["lighthouse_property_slug_map"] = sm

    with pytest.raises(ConfigPreflightError, match="aka_white_house"):
        render_new_deal_scaffold(config, tmp_path)


def test_slug_map_comp_missing_raises(tmp_path):
    config = _sfoem_shaped_config()
    sm = dict(config["lighthouse_property_slug_map"])
    del sm["Hay-Adams Hotel"]  # hay_adams no longer in values
    config["lighthouse_property_slug_map"] = sm

    with pytest.raises(ConfigPreflightError, match="hay_adams"):
        render_new_deal_scaffold(config, tmp_path)


def test_slug_map_duplicate_value_raises(tmp_path):
    config = _sfoem_shaped_config()
    sm = dict(config["lighthouse_property_slug_map"])
    # Two headers map to the same canonical slug
    sm["Capital Hilton DC (alt header)"] = "capital_hilton"
    config["lighthouse_property_slug_map"] = sm

    with pytest.raises(ConfigPreflightError, match="duplicate slug"):
        render_new_deal_scaffold(config, tmp_path)


def test_slug_map_drop_overlap_raises(tmp_path):
    config = _sfoem_shaped_config()
    # Same header is both routed and dropped
    config["lighthouse_drop_columns"] = ["Capital Hilton DC"]

    with pytest.raises(ConfigPreflightError, match="both slug_map and drop_columns"):
        render_new_deal_scaffold(config, tmp_path)


@pytest.mark.skipif(
    not AKA_REPO.exists(),
    reason="AKA reference repo not present (needed for config.json passthrough)",
)
def test_slug_map_sfoem_shape_passes(tmp_path):
    """Non-AKA-pattern config (slug_map non-empty + drop_columns non-empty)
    renders cleanly. This is the path the AKA byte-match test does NOT
    exercise — the new pre-flight validator's happy path. Cross-check
    that the rendered lighthouse_ingest.py has the SFOEM-shaped
    LIGHTHOUSE_PROPERTY_SLUG dict baked in (not the empty AKA degenerate
    block)."""
    config = _sfoem_shaped_config()
    summary = render_new_deal_scaffold(config, tmp_path)
    assert summary["rendered"]

    ingest_text = (tmp_path / "scripts" / "lighthouse_ingest.py").read_text(
        encoding="utf-8"
    )
    # Slug map present
    assert "AKA White House (verbatim)" in ingest_text
    assert '"aka_white_house"' in ingest_text
    # Drop columns present
    assert "Park Central Hotel New York" in ingest_text
    # Empty-degenerate form NOT present
    assert "LIGHTHOUSE_PROPERTY_SLUG: dict[str, str] = {}" not in ingest_text
    assert "LIGHTHOUSE_DROP_COLUMNS: list[str] = []" not in ingest_text
