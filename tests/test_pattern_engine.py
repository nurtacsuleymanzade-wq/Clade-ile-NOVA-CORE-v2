"""Tests for pattern_engine.py detection logic."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.realtime.pattern_engine import detect_pattern_from_data


def make_candle(**kwargs) -> dict:
    defaults = {
        "timestamp_ms": 0,
        "open_score": 0.0,
        "close_score": 0.0,
        "high_score": 5.0,
        "low_score": -5.0,
        "body_ratio": 0.5,
        "upper_wick": 0.2,
        "lower_wick": 0.2,
        "cvd": 0.0,
        "volume": 0.01,
        "absorption": 0.0,
        "trapped": False,
        "displacement": 2.0,
        "sample_count": 60,
    }
    return {**defaults, **kwargs}


def make_micro_event(**kwargs) -> dict:
    defaults = {
        "timestamp_ms": 0,
        "event_type": "PRESSURE_BUILDING_LONG",
        "direction": "LONG",
        "continuity": 0.8,
        "momentum": 0.5,
        "consistency": 0.7,
        "avg_score": 4.0,
        "sample_count": 30,
    }
    return {**defaults, **kwargs}


def make_regime(**kwargs) -> dict:
    defaults = {
        "regime": "EXPANSION",
        "atr": 100.0,
        "volume_avg": 0.01,
        "delta_consistency": 0.6,
    }
    return {**defaults, **kwargs}


def test_trap_detected():
    micro = make_micro_event(event_type="NEUTRAL_NOISE", direction="NEUTRAL")
    candles = [
        make_candle(cvd=0.05, open_score=3.0, close_score=-2.0,
                    trapped=True, body_ratio=0.2),
        make_candle(cvd=-0.05, open_score=2.0, close_score=-3.0,
                    trapped=True, body_ratio=0.2),
    ]
    result = detect_pattern_from_data(micro, candles)
    assert result["pattern"] == "TRAP", f"Expected TRAP, got {result['pattern']}"
    assert result["direction"] in ("LONG", "SHORT")


def test_absorption_detected():
    micro = make_micro_event()
    regime = make_regime(volume_avg=0.005)  # low vol_avg so our 0.01 volume is above
    candles = [
        make_candle(absorption=0.5, volume=0.01, displacement=1.0, cvd=0.03),
        make_candle(absorption=0.6, volume=0.012, displacement=1.2, cvd=0.02),
    ]
    result = detect_pattern_from_data(micro, candles, regime=regime)
    assert result["pattern"] == "ABSORPTION", f"Expected ABSORPTION, got {result['pattern']}"


def test_stop_hunt_detected():
    micro = make_micro_event()
    candles = [
        make_candle(cvd=0.05, upper_wick=0.1, lower_wick=0.1, body_ratio=0.5),
        make_candle(cvd=-0.06, upper_wick=0.1, lower_wick=0.5, body_ratio=0.2),
    ]
    result = detect_pattern_from_data(micro, candles)
    assert result["pattern"] == "STOP_HUNT", f"Expected STOP_HUNT, got {result['pattern']}"
    assert result["direction"] == "LONG"


def test_returns_none_pattern_when_insufficient_data():
    micro = make_micro_event()
    result = detect_pattern_from_data(micro, [])
    assert result["pattern"] == "NONE"

    result2 = detect_pattern_from_data(micro, [make_candle()])
    assert result2["pattern"] == "NONE"


def test_compression_detected():
    micro = make_micro_event(direction="NEUTRAL", event_type="NEUTRAL_NOISE")
    regime = make_regime(regime="COMPRESSION", volume_avg=0.02)
    candles = [
        make_candle(cvd=0.001, volume=0.01, displacement=0.5),
        make_candle(cvd=0.002, volume=0.012, displacement=0.4),
    ]
    result = detect_pattern_from_data(micro, candles, regime=regime)
    assert result["pattern"] == "COMPRESSION"


def test_continuation_detected():
    micro = make_micro_event(direction="LONG", continuity=0.75)
    trend = {"trend": "TREND_UP"}
    regime = make_regime(regime="TREND", volume_avg=0.02)
    candles = [
        make_candle(cvd=0.05, close_score=3.0, volume=0.015),
        make_candle(cvd=0.03, close_score=2.5, volume=0.01),  # pullback, lower cvd
    ]
    result = detect_pattern_from_data(micro, candles, trend=trend, regime=regime)
    assert result["pattern"] == "CONTINUATION"
    assert result["direction"] == "LONG"
