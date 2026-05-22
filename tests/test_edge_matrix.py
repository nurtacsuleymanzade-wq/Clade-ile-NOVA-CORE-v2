"""Tests for edge_matrix.py grouping and computation."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.research.edge_matrix import compute_edge_matrix_from_trades


def make_trade(pattern="TRAP", direction="LONG", result="TP1_HIT", R=1.5,
               opened_at=1700000000000, duration_seconds=600,
               tags=None, timeframe="1m", session="LONDON", trend="TREND_UP", regime="EXPANSION") -> dict:
    tags = tags or ["trend_trend_up", "regime_expansion"]
    return {
        "id": f"t_{pattern}_{result}",
        "pattern": pattern,
        "timeframe": timeframe,
        "direction": direction,
        "entry": 50000.0,
        "sl": 49900.0,
        "tp1": 50150.0,
        "tp2": 50300.0,
        "rr": 1.5,
        "opened_at": "2026-05-22T03:00:00Z",
        "opened_at_epoch": opened_at,
        "closed_at": "2026-05-22T03:10:00Z",
        "closed_at_epoch": opened_at + duration_seconds * 1000,
        "result": result,
        "R": R,
        "r_multiple": R,
        "duration_seconds": duration_seconds,
        "context": {"tags": tags},
        "session": session,
        "trend_at_entry": trend,
        "regime_at_entry": regime,
    }


def test_grouping_works_correctly():
    trades = [
        make_trade("TRAP", "LONG", "TP1_HIT", 1.5),
        make_trade("TRAP", "LONG", "SL_HIT", -1.0),
        make_trade("ABSORPTION", "SHORT", "TP1_HIT", 2.0),
    ]
    matrix = compute_edge_matrix_from_trades(trades)
    # Should have pattern-level groupings
    assert "pattern:TRAP" in matrix, "Should group by pattern"
    assert "pattern:ABSORPTION" in matrix


def test_winrate_calculation_correct():
    # 2 wins, 1 loss → 2/3 winrate
    trades = [
        make_trade("TRAP", "LONG", "TP1_HIT", 1.5),
        make_trade("TRAP", "LONG", "TP2_HIT", 2.0),
        make_trade("TRAP", "LONG", "SL_HIT", -1.0),
    ]
    matrix = compute_edge_matrix_from_trades(trades)
    stats = matrix["pattern:TRAP"]
    assert stats["sample_count"] == 3
    assert abs(stats["winrate"] - 2 / 3) < 0.01, f"Winrate {stats['winrate']} expected ~0.667"


def test_expectancy_calculation():
    # 3 wins at 1.5R, 1 loss at -1R → expectancy = (0.75 * 1.5) - (0.25 * 1) = 1.125 - 0.25 = 0.875
    trades = [
        make_trade("REVERSAL", "LONG", "TP1_HIT", 1.5),
        make_trade("REVERSAL", "LONG", "TP1_HIT", 1.5),
        make_trade("REVERSAL", "LONG", "TP1_HIT", 1.5),
        make_trade("REVERSAL", "LONG", "SL_HIT", -1.0),
    ]
    matrix = compute_edge_matrix_from_trades(trades)
    stats = matrix["pattern:REVERSAL"]
    assert stats["expectancy"] > 0, "Positive expectancy expected"
    assert abs(stats["expectancy"] - 0.875) < 0.05, f"Expectancy {stats['expectancy']} expected ~0.875"


def test_suppression_triggers_at_negative_expectancy():
    # 1 win, 9 losses → negative expectancy, sample_count >= 20 after 20 trades
    trades = (
        [make_trade("COMPRESSION", "LONG", "TP1_HIT", 1.5)] * 2
        + [make_trade("COMPRESSION", "LONG", "SL_HIT", -1.0)] * 18
    )
    matrix = compute_edge_matrix_from_trades(trades)
    stats = matrix["pattern:COMPRESSION"]
    assert stats["sample_count"] == 20
    assert stats["expectancy"] < 0, f"Expected negative expectancy, got {stats['expectancy']}"
    # Suppression decision is in model_promoter, but expectancy is negative
    assert stats["winrate"] < 0.5


def test_combination_keys_generated():
    trades = [
        make_trade("TRAP", "LONG", "TP1_HIT", 1.5, tags=["trend_trend_up", "regime_expansion"]),
    ]
    matrix = compute_edge_matrix_from_trades(trades)
    # Should have pattern+trend combination and canonical combo key
    keys = list(matrix.keys())
    pattern_trend_keys = [k for k in keys if "pattern:TRAP" in k and "trend:" in k]
    assert len(pattern_trend_keys) > 0, f"Expected pattern+trend key, got keys: {keys}"
    canonical_keys = [k for k in keys if k.startswith("TRAP|1m|LONDON|TREND_UP")]
    assert len(canonical_keys) == 1


def test_empty_trades_returns_empty_matrix():
    matrix = compute_edge_matrix_from_trades([])
    assert matrix == {}, "Empty trades should return empty matrix"


def test_avg_R_positive_when_winning():
    trades = [
        make_trade("STOP_HUNT", "SHORT", "TP1_HIT", 2.0),
        make_trade("STOP_HUNT", "SHORT", "TP2_HIT", 3.0),
    ]
    matrix = compute_edge_matrix_from_trades(trades)
    stats = matrix["pattern:STOP_HUNT"]
    assert stats["avg_R"] > 0, "avg_R should be positive for winning trades"
    assert abs(stats["avg_R"] - 2.5) < 0.01


def test_timeout_excluded_from_edge_sample_but_counted():
    trades = [
        make_trade("TRAP", "LONG", "TP1_HIT", 1.5),
        make_trade("TRAP", "LONG", "TIMEOUT", 0.2),
    ]
    matrix = compute_edge_matrix_from_trades(trades)
    stats = matrix["TRAP|1m|LONDON|TREND_UP"]
    assert stats["sample_count"] == 1
    assert stats["timeout_count"] == 1


def test_status_fields_present_on_canonical_combo():
    trades = [make_trade("TRAP", "LONG", "TP1_HIT", 1.5) for _ in range(30)]
    matrix = compute_edge_matrix_from_trades(trades)
    stats = matrix["TRAP|1m|LONDON|TREND_UP"]
    assert stats["status"] == "ACTIVE"
    assert stats["promote_reason"] is not None
