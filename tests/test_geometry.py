"""Tests for geometry_engine.py computation."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.realtime.geometry_engine import compute_geometry_from_data


def make_zones(highs=None, lows=None, price=50000.0) -> dict:
    zones = []
    for h in (highs or []):
        zones.append({"type": "equal_highs", "price": h})
    for l in (lows or []):
        zones.append({"type": "equal_lows", "price": l})
    return {"current_price": price, "zones": zones}


def test_sl_below_sweep_low_for_long():
    pattern = {"pattern": "STOP_HUNT", "direction": "LONG", "confidence": 0.8}
    zones = make_zones(lows=[49800.0], highs=[50500.0, 51000.0], price=50000.0)
    geo = compute_geometry_from_data(pattern, zones, 50000.0)
    assert geo is not None, "Geometry should be computed"
    assert geo["sl"] < 49800.0, f"SL {geo['sl']} should be below sweep low 49800"
    assert geo["direction"] == "LONG"
    assert geo["entry_reason"] == "reclaim_after_sweep_low"
    assert geo["sl_reason"] == "below_sweep_low_invalidation"
    assert geo["tp_reason"] == "nearest_equal_high_liquidity"


def test_sl_above_sweep_high_for_short():
    pattern = {"pattern": "STOP_HUNT", "direction": "SHORT", "confidence": 0.8}
    zones = make_zones(highs=[50200.0], lows=[49500.0, 49000.0], price=50000.0)
    geo = compute_geometry_from_data(pattern, zones, 50000.0)
    assert geo is not None, "Geometry should be computed"
    assert geo["sl"] > 50200.0, f"SL {geo['sl']} should be above sweep high 50200"
    assert geo["direction"] == "SHORT"


def test_tp_at_nearest_zone_level_long():
    pattern = {"pattern": "ABSORPTION", "direction": "LONG", "confidence": 0.7}
    zones = make_zones(lows=[49800.0], highs=[50300.0, 51000.0], price=50000.0)
    geo = compute_geometry_from_data(pattern, zones, 50000.0)
    assert geo is not None
    # TP1 should be the nearest high above entry
    assert geo["tp1"] <= geo["tp2"], "TP1 should be <= TP2"
    assert geo["tp1"] > geo["entry"], "TP1 should be above entry for LONG"
    assert geo["pattern_reason"]


def test_tp_at_nearest_zone_level_short():
    pattern = {"pattern": "ABSORPTION", "direction": "SHORT", "confidence": 0.7}
    zones = make_zones(highs=[50200.0], lows=[49700.0, 49200.0], price=50000.0)
    geo = compute_geometry_from_data(pattern, zones, 50000.0)
    assert geo is not None
    assert geo["tp1"] >= geo["tp2"], "TP1 should be >= TP2 for SHORT"
    assert geo["tp1"] < geo["entry"], "TP1 should be below entry for SHORT"


def test_rr_computed_correctly():
    pattern = {"pattern": "STOP_HUNT", "direction": "LONG", "confidence": 0.75}
    zones = make_zones(lows=[49500.0], highs=[50500.0, 51000.0], price=50000.0)
    geo = compute_geometry_from_data(pattern, zones, 50000.0)
    assert geo is not None
    expected_rr = abs(geo["tp1"] - geo["entry"]) / abs(geo["entry"] - geo["sl"])
    assert abs(geo["rr"] - expected_rr) < 0.01, f"RR mismatch: {geo['rr']} vs {expected_rr}"


def test_returns_none_when_rr_below_min():
    pattern = {"pattern": "STOP_HUNT", "direction": "LONG", "confidence": 0.75}
    # sweep_low=49900 → entry≈49930, sl≈49885, risk≈45
    # tp1=49950 → reward≈20, RR≈0.44 < 1.5 → should return None
    zones = make_zones(lows=[49900.0], highs=[49950.0], price=50000.0)
    geo = compute_geometry_from_data(pattern, zones, 50000.0)
    assert geo is None, f"Expected None for RR < 1.5, got {geo}"


def test_returns_none_for_none_pattern():
    pattern = {"pattern": "NONE", "direction": "NEUTRAL"}
    zones = make_zones(lows=[49800.0], highs=[50500.0], price=50000.0)
    geo = compute_geometry_from_data(pattern, zones, 50000.0)
    assert geo is None


def test_returns_none_when_no_price():
    pattern = {"pattern": "STOP_HUNT", "direction": "LONG", "confidence": 0.75}
    zones = make_zones(lows=[49800.0], highs=[50500.0])
    geo = compute_geometry_from_data(pattern, zones, None)
    assert geo is None


def test_default_pattern_still_has_reason_fields():
    pattern = {"pattern": "TRAP", "direction": "LONG", "confidence": 0.6}
    zones = make_zones(lows=[49700.0], highs=[50550.0, 50900.0], price=50000.0)
    geo = compute_geometry_from_data(pattern, zones, 50000.0)
    assert geo is not None
    assert geo["entry_reason"]
    assert geo["sl_reason"]
    assert geo["tp_reason"]
