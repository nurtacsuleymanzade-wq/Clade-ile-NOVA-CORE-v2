"""Tests for research reporter output."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.research.reporter import _build_report, _format_telegram


def make_open_trade() -> dict:
    now_ms = int(time.time() * 1000)
    return {
        "pattern": "STOP_HUNT_RECLAIM_LONG",
        "timeframe": "1m",
        "direction": "LONG",
        "entry": 50000.0,
        "sl": 49850.0,
        "tp1": 50300.0,
        "tp2": 50600.0,
        "rr": 2.0,
        "confidence": 0.7,
        "context_adjusted_confidence": 0.84,
        "pattern_reason": "wick_sweep_and_reclaim",
        "entry_reason": "reclaim_after_sweep_low",
        "sl_reason": "below_sweep_low_invalidation",
        "tp_reason": "nearest_equal_high_liquidity",
        "observer_score": 4.2,
        "micro_event": "PRESSURE_BUILDING_LONG",
        "opened_at_epoch": now_ms,
        "session": "LONDON",
        "trend_at_entry": "TREND_UP",
        "regime_at_entry": "EXPANSION",
    }


def make_closed_trade(direction="LONG", result="TP1_HIT", r_multiple=1.5) -> dict:
    trade = make_open_trade()
    trade.update({
        "direction": direction,
        "result": result,
        "exit_price": 50300.0 if result != "SL_HIT" else 49850.0,
        "r_multiple": r_multiple,
        "duration_seconds": 900,
        "closed_at_epoch": trade["opened_at_epoch"] + 900000,
    })
    return trade


def test_report_includes_long_short_breakdown_and_suppressed():
    open_trades = [make_open_trade()]
    closed_trades = [
        make_closed_trade("LONG", "SL_HIT", -1.0),
        make_closed_trade("SHORT", "TP1_HIT", 1.5),
    ]
    edge_data = {
        "canonical_combos": {
            "STOP_HUNT_RECLAIM_LONG|1m|LONDON|TREND_UP": {
                "combo_key": "STOP_HUNT_RECLAIM_LONG|1m|LONDON|TREND_UP",
                "pattern": "STOP_HUNT_RECLAIM_LONG",
                "timeframe": "1m",
                "session": "LONDON",
                "trend": "TREND_UP",
                "regime": "EXPANSION",
                "sample_count": 31,
                "winrate": 0.52,
                "expectancy": 0.22,
                "status": "ACTIVE",
            },
            "CONTINUATION_LONG|1m|OFF_SESSION|TREND_DOWN": {
                "combo_key": "CONTINUATION_LONG|1m|OFF_SESSION|TREND_DOWN",
                "pattern": "CONTINUATION_LONG",
                "timeframe": "1m",
                "session": "OFF_SESSION",
                "trend": "TREND_DOWN",
                "regime": "COMPRESSION",
                "sample_count": 31,
                "winrate": 0.083,
                "expectancy": -0.83,
                "status": "SUPPRESSED",
            },
        },
        "best_combo": {
            "combo_key": "STOP_HUNT_RECLAIM_LONG|1m|LONDON|TREND_UP",
            "pattern": "STOP_HUNT_RECLAIM_LONG",
            "session": "LONDON",
            "trend": "TREND_UP",
            "sample_count": 31,
            "winrate": 0.52,
            "expectancy": 0.22,
            "status": "ACTIVE",
        },
        "worst_combo": {
            "combo_key": "CONTINUATION_LONG|1m|OFF_SESSION|TREND_DOWN",
            "pattern": "CONTINUATION_LONG",
            "session": "OFF_SESSION",
            "trend": "TREND_DOWN",
            "sample_count": 31,
            "winrate": 0.083,
            "expectancy": -0.83,
            "status": "SUPPRESSED",
        },
    }
    suppressed_data = {
        "sample_building": [],
        "active": [edge_data["best_combo"]],
        "suppressed": [{
            **edge_data["worst_combo"],
            "regime": "COMPRESSION",
        }],
        "combinations": {},
    }
    trend = {"trend": "TREND_UP", "swing_highs_count": 4, "swing_lows_count": 3}
    regime = {"regime": "EXPANSION", "atr": 12.0, "delta_consistency": 0.75}
    decision = {"current_price": 50000.0}
    zones = {"current_price": 50000.0}

    report = _build_report(open_trades, closed_trades, {"current_price": 50000.0}, edge_data, suppressed_data, trend, regime, decision, zones)
    text = _format_telegram(report)

    assert "LONG:" in text
    assert "SHORT:" in text
    assert "SUPPRESSED KOMBİNASYONLAR" in text
    assert "Zincir:" in text
