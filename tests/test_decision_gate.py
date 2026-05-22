"""Tests for decision_gate context confidence gating."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.realtime.decision_gate import _make_decision, apply_context_multiplier


def test_apply_context_multiplier_boosts_trend_alignment():
    adjusted = apply_context_multiplier(0.5, "LONG", "TREND_UP", "EXPANSION")
    assert adjusted > 0.5


def test_blocks_low_context_confidence():
    geometry = {
        "pattern": "CONTINUATION_LONG",
        "direction": "LONG",
        "timeframe": "1m",
        "entry": 50000.0,
        "sl": 49900.0,
        "tp1": 50200.0,
        "tp2": 50400.0,
        "rr": 2.0,
        "confidence": 0.45,
        "pattern_reason": "trend_pullback_then_cvd_recovery",
        "entry_reason": "cvd_back_with_trend",
        "sl_reason": "below_pullback_low",
        "tp_reason": "next_swing_high_target",
    }
    trend = {"trend": "TREND_DOWN", "swing_highs_count": 3, "swing_lows_count": 3}
    regime = {"regime": "COMPRESSION", "atr": 10.0, "delta_consistency": 0.5}
    latest_score = {"score": 3.2, "delta": 1.2, "imbalance": 0.1}
    micro_event = {"event_type": "PRESSURE_BUILDING_LONG"}
    candle_dna = {"cvd": 4.5, "body_ratio": 0.4}

    decision = _make_decision(geometry, trend, regime, {}, latest_score, micro_event, candle_dna)
    assert decision["decision"] == "BLOCKED"
    assert decision["reason"] == "LOW_CONTEXT_CONFIDENCE"
    assert "context_adjusted_confidence" in decision


def test_decision_includes_trade_dna_fields_on_allow():
    geometry = {
        "pattern": "STOP_HUNT_RECLAIM_LONG",
        "direction": "LONG",
        "timeframe": "1m",
        "entry": 50000.0,
        "sl": 49850.0,
        "tp1": 50300.0,
        "tp2": 50600.0,
        "rr": 2.0,
        "confidence": 0.75,
        "pattern_reason": "wick_sweep_and_reclaim",
        "entry_reason": "reclaim_after_sweep_low",
        "sl_reason": "below_sweep_low_invalidation",
        "tp_reason": "nearest_equal_high_liquidity",
        "current_price": 50000.0,
    }
    trend = {"trend": "TREND_UP", "swing_highs_count": 4, "swing_lows_count": 3}
    regime = {"regime": "EXPANSION", "atr": 12.0, "delta_consistency": 0.8}
    latest_score = {"score": 4.0, "delta": 2.2, "imbalance": 0.2}
    micro_event = {"event_type": "PRESSURE_BUILDING_LONG"}
    candle_dna = {"cvd": 5.5, "body_ratio": 0.42}

    decision = _make_decision(geometry, trend, regime, {}, latest_score, micro_event, candle_dna)
    assert decision["decision"] == "ALLOW_PAPER"
    assert decision["observer_score"] == 4.0
    assert decision["pattern_reason"] == "wick_sweep_and_reclaim"
    assert decision["entry_reason"] == "reclaim_after_sweep_low"
    assert decision["session"]
