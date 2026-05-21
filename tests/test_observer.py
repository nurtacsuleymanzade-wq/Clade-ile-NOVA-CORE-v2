"""Tests for observer.py score computation."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.realtime.observer import Observer


def make_observer() -> Observer:
    return Observer()


def test_score_between_minus10_and_plus10():
    obs = make_observer()
    # No data -> neutral score
    record = obs._compute_score()
    assert -10.0 <= record["score"] <= 10.0, f"Score out of range: {record['score']}"


def test_neutral_score_when_no_data():
    obs = make_observer()
    record = obs._compute_score()
    assert abs(record["score"]) < 5.0, f"Expected near-neutral score, got {record['score']}"
    assert record["data_quality"] == 0.0


def test_score_with_buy_pressure():
    obs = make_observer()
    import time
    now_ms = int(time.time() * 1000)
    # Inject strong buy trades
    for i in range(10):
        obs._agg_trades.append({
            "price": 50000.0 + i,
            "qty": 0.5,
            "is_buyer_maker": False,  # buy
            "ts": now_ms - i * 50,
        })
    obs._book_ticker = {"bid": 50010.0, "ask": 50011.0, "bid_qty": 5.0, "ask_qty": 1.0}
    record = obs._compute_score()
    assert -10.0 <= record["score"] <= 10.0
    assert record["score"] > 0, "Expected positive score with buy pressure"


def test_score_with_sell_pressure():
    obs = make_observer()
    import time
    now_ms = int(time.time() * 1000)
    for i in range(10):
        obs._agg_trades.append({
            "price": 50000.0 - i,
            "qty": 0.5,
            "is_buyer_maker": True,  # sell
            "ts": now_ms - i * 50,
        })
    obs._book_ticker = {"bid": 49989.0, "ask": 49990.0, "bid_qty": 1.0, "ask_qty": 5.0}
    record = obs._compute_score()
    assert -10.0 <= record["score"] <= 10.0
    assert record["score"] < 0, "Expected negative score with sell pressure"


def test_large_lot_detection():
    obs = make_observer()
    import time
    now_ms = int(time.time() * 1000)
    # Large buy lot
    obs._agg_trades.append({
        "price": 50000.0,
        "qty": 2.0,  # > LARGE_LOT_THRESHOLD (1.0)
        "is_buyer_maker": False,
        "ts": now_ms - 100,
    })
    record = obs._compute_score()
    assert record["large_lot"] is True, "Expected large_lot=True for qty=2.0"


def test_large_lot_not_triggered_on_small():
    obs = make_observer()
    import time
    now_ms = int(time.time() * 1000)
    obs._agg_trades.append({
        "price": 50000.0,
        "qty": 0.1,
        "is_buyer_maker": False,
        "ts": now_ms - 100,
    })
    record = obs._compute_score()
    assert record["large_lot"] is False, "Expected large_lot=False for small qty"


def test_score_keys_present():
    obs = make_observer()
    record = obs._compute_score()
    required_keys = ["timestamp_ms", "score", "dominant", "delta", "imbalance", "absorption", "large_lot", "data_quality"]
    for key in required_keys:
        assert key in record, f"Missing key: {key}"
