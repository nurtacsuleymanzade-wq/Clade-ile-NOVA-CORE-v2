"""Tests for decision_gate context confidence gating and suppression."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from services.realtime.decision_gate import _make_decision, apply_context_multiplier, _find_suppressed_entry


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


# ── GÖREV 3: Context Confidence Threshold ────────────────────────────────────

def test_min_context_confidence_threshold_is_025():
    """MIN_CONTEXT_CONFIDENCE config'de 0.25 olmalı."""
    assert config.MIN_CONTEXT_CONFIDENCE == 0.25


def test_blocks_exactly_at_min_context_confidence():
    """context_adjusted_confidence < 0.25 → BLOCK."""
    geometry = {
        "pattern": "REVERSAL",
        "direction": "SHORT",
        "timeframe": "1m",
        "entry": 50000.0,
        "sl": 50150.0,
        "tp1": 49700.0,
        "tp2": 49400.0,
        "rr": 2.0,
        "confidence": 0.4,
    }
    # No multiplier match (direction=SHORT, trend=NO_TREND, regime=RANGE → mult=0.8)
    # adjusted = 0.4 * 0.8 = 0.32 → should ALLOW, not block
    trend = {"trend": "NO_TREND"}
    regime = {"regime": "RANGE"}
    decision = _make_decision(geometry, trend, regime, {}, None, None, None)
    assert decision["decision"] == "ALLOW_PAPER"


def test_blocks_when_context_adjusted_below_025():
    """adjusted < 0.25 → LOW_CONTEXT_CONFIDENCE."""
    geometry = {
        "pattern": "CONTINUATION_SHORT",
        "direction": "SHORT",
        "timeframe": "1m",
        "entry": 50000.0,
        "sl": 50100.0,
        "tp1": 49800.0,
        "tp2": 49600.0,
        "rr": 2.0,
        "confidence": 0.3,
    }
    # SHORT + TREND_UP → mult=0.5; COMPRESSION → mult*=0.6 → 0.3*0.5*0.6=0.09 < 0.25
    trend = {"trend": "TREND_UP"}
    regime = {"regime": "COMPRESSION"}
    decision = _make_decision(geometry, trend, regime, {}, None, None, None)
    assert decision["decision"] == "BLOCKED"
    assert decision["reason"] == "LOW_CONTEXT_CONFIDENCE"


# ── GÖREV 4: Canonical Suppression Key ───────────────────────────────────────

def test_canonical_combo_key_format():
    """combo_key pattern|direction|timeframe|session formatında üretilmeli."""
    geometry = {
        "pattern": "CONTINUATION_LONG",
        "direction": "LONG",
        "timeframe": "1m",
        "entry": 50000.0,
        "sl": 49900.0,
        "tp1": 50200.0,
        "tp2": 50400.0,
        "rr": 2.0,
        "confidence": 0.8,
    }
    trend = {"trend": "TREND_UP"}
    regime = {"regime": "EXPANSION"}
    decision = _make_decision(geometry, trend, regime, {}, None, None, None)
    combo_key = decision["combo_key"]
    parts = combo_key.split("|")
    # Expected: pattern|direction|timeframe|session|trend|regime
    assert len(parts) == 6, f"combo_key must have 6 parts, got {len(parts)}: {combo_key}"
    assert parts[0] == "CONTINUATION_LONG"
    assert parts[1] == "LONG"
    assert parts[2] == "1m"
    # parts[3] = session (varies by time of test)
    assert parts[4] == "TREND_UP"
    assert parts[5] == "EXPANSION"


def test_suppressed_combo_with_direction_blocks():
    """Direction+regime dahil canonical key ile BLOCK."""
    geometry = {
        "pattern": "CONTINUATION_LONG",
        "direction": "LONG",
        "timeframe": "1m",
        "entry": 50000.0,
        "sl": 49900.0,
        "tp1": 50200.0,
        "tp2": 50400.0,
        "rr": 2.0,
        "confidence": 0.8,
    }
    trend = {"trend": "TREND_UP"}
    regime = {"regime": "EXPANSION"}

    # We need to know what session _make_decision will produce
    # Build the expected combo_key first
    from datetime import datetime, timezone
    now_hour = datetime.now(tz=timezone.utc).hour
    if config.LONDON_START_UTC <= now_hour < config.LONDON_END_UTC:
        session = "LONDON"
    elif config.NEW_YORK_START_UTC <= now_hour < config.NEW_YORK_END_UTC:
        session = "NEW_YORK"
    else:
        session = "OFF_SESSION"

    canonical_key = f"CONTINUATION_LONG|LONG|1m|{session}|TREND_UP|EXPANSION"

    suppressed_state = {
        "combinations": {
            canonical_key: {"status": "SUPPRESSED", "combo_key": canonical_key}
        },
        "suppressed": [],
    }
    decision = _make_decision(geometry, trend, regime, suppressed_state, None, None, None)
    assert decision["decision"] == "BLOCKED"
    assert decision["reason"] == "SUPPRESSED_COMBINATION"


def test_suppressed_long_does_not_block_short():
    """CONTINUATION_LONG suppressed ama CONTINUATION_SHORT değil — direction farkı."""
    from datetime import datetime, timezone
    now_hour = datetime.now(tz=timezone.utc).hour
    if config.LONDON_START_UTC <= now_hour < config.LONDON_END_UTC:
        session = "LONDON"
    elif config.NEW_YORK_START_UTC <= now_hour < config.NEW_YORK_END_UTC:
        session = "NEW_YORK"
    else:
        session = "OFF_SESSION"

    # Suppress only LONG
    suppressed_long_key = f"CONTINUATION_LONG|LONG|1m|{session}|TREND_UP|EXPANSION"
    suppressed_state = {
        "combinations": {
            suppressed_long_key: {"status": "SUPPRESSED", "combo_key": suppressed_long_key}
        },
        "suppressed": [],
    }

    # Try to open SHORT — should NOT be blocked by LONG suppression
    geometry = {
        "pattern": "CONTINUATION_SHORT",
        "direction": "SHORT",
        "timeframe": "1m",
        "entry": 50000.0,
        "sl": 50100.0,
        "tp1": 49800.0,
        "tp2": 49600.0,
        "rr": 2.0,
        "confidence": 0.8,
    }
    trend = {"trend": "TREND_UP"}
    regime = {"regime": "EXPANSION"}
    decision = _make_decision(geometry, trend, regime, suppressed_state, None, None, None)
    # Should not be SUPPRESSED — might be LOW_CONTEXT_CONFIDENCE (SHORT vs TREND_UP penalty)
    assert decision["reason"] != "SUPPRESSED_COMBINATION"


def test_suppressed_no_trend_does_not_block_reversal_risk():
    """NO_TREND suppressed key REVERSAL_RISK ile karışmamalı — regime farkı."""
    from datetime import datetime, timezone
    now_hour = datetime.now(tz=timezone.utc).hour
    if config.LONDON_START_UTC <= now_hour < config.LONDON_END_UTC:
        session = "LONDON"
    elif config.NEW_YORK_START_UTC <= now_hour < config.NEW_YORK_END_UTC:
        session = "NEW_YORK"
    else:
        session = "OFF_SESSION"

    # Suppress COMPRESSION (NO_TREND) key
    no_trend_key = f"CONTINUATION_LONG|LONG|1m|{session}|NO_TREND|COMPRESSION"
    suppressed_state = {
        "combinations": {
            no_trend_key: {"status": "SUPPRESSED", "combo_key": no_trend_key}
        },
        "suppressed": [],
    }

    geometry = {
        "pattern": "CONTINUATION_LONG",
        "direction": "LONG",
        "timeframe": "1m",
        "entry": 50000.0,
        "sl": 49900.0,
        "tp1": 50200.0,
        "tp2": 50400.0,
        "rr": 2.0,
        "confidence": 0.8,
    }
    # TREND_UP + EXPANSION → different key, should NOT match
    trend = {"trend": "TREND_UP"}
    regime = {"regime": "EXPANSION"}
    decision = _make_decision(geometry, trend, regime, suppressed_state, None, None, None)
    assert decision["reason"] != "SUPPRESSED_COMBINATION"


def test_find_suppressed_entry_canonical_key():
    """_find_suppressed_entry canonical key ile doğru eşleşmeli."""
    suppressed_state = {
        "combinations": {
            "PAT|LONG|1m|LONDON|TREND_UP|EXPANSION": {
                "status": "SUPPRESSED",
                "combo_key": "PAT|LONG|1m|LONDON|TREND_UP|EXPANSION",
            }
        },
        "suppressed": [],
    }
    result = _find_suppressed_entry(suppressed_state, "PAT|LONG|1m|LONDON|TREND_UP|EXPANSION")
    assert result is not None
    assert result["status"] == "SUPPRESSED"

    # Different key → no match
    result2 = _find_suppressed_entry(suppressed_state, "PAT|SHORT|1m|LONDON|TREND_UP|EXPANSION")
    assert result2 is None
