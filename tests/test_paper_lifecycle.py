"""Tests for paper_lifecycle.py trade management."""
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import config


def _setup_temp_db(tmp_dir: str) -> Path:
    db_path = Path(tmp_dir) / "test_trades.db"
    from services.realtime.paper_lifecycle import _init_db
    _init_db(db_path)
    return db_path


def _get_open_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM open_trades")
    count = cur.fetchone()[0]
    conn.close()
    return count


def _get_closed_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM closed_trades")
    count = cur.fetchone()[0]
    conn.close()
    return count


def _get_columns(db_path: Path, table_name: str) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cur.fetchall()]
    conn.close()
    return columns


def _get_open_trades(db_path: Path) -> list[dict]:
    from services.realtime.paper_lifecycle import _get_open_trades
    with patch.object(config, "PAPER_TRADES_DB", db_path):
        return _get_open_trades(db_path)


def test_opens_trade_on_allow_paper():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        with patch.object(config, "PAPER_TRADES_DB", db_path):
            from services.realtime.paper_lifecycle import PaperLifecycle, _insert_open_trade
            pl = PaperLifecycle.__new__(PaperLifecycle)
            pl._last_decision_ts = 0

            decision = {
                "decision": "ALLOW_PAPER",
                "timestamp_ms": int(time.time() * 1000),
                "pattern": "TRAP",
                "direction": "LONG",
                "entry": 50000.0,
                "sl": 49850.0,
                "tp1": 50300.0,
                "tp2": 50600.0,
                "rr": 2.0,
                "tags": [],
            }
            open_trades = []
            trade = pl._open_trade(decision, open_trades)
            assert trade is not None, "Trade should be opened"
            count = _get_open_count(db_path)
            assert count == 1, f"Expected 1 open trade, got {count}"
            trades = _get_open_trades(db_path)
            assert trades[0]["pattern_reason"] == ""
            assert "context_adjusted_confidence" in trades[0]


def test_trade_dna_schema_columns_exist():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        open_columns = _get_columns(db_path, "open_trades")
        closed_columns = _get_columns(db_path, "closed_trades")

        for required in (
            "timeframe",
            "confidence",
            "context_adjusted_confidence",
            "observer_score",
            "delta_at_entry",
            "imbalance_at_entry",
            "cvd_at_entry",
            "body_ratio",
            "micro_event",
            "pattern_reason",
            "entry_reason",
            "sl_reason",
            "tp_reason",
            "session",
            "trend_at_entry",
            "regime_at_entry",
            "opened_at",
            "lineage_chain",
        ):
            assert required in open_columns

        for required in ("closed_at", "exit_price", "r_multiple", "close_reason"):
            assert required in closed_columns


def test_closes_on_sl_hit():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        with patch.object(config, "PAPER_TRADES_DB", db_path):
            from services.realtime.paper_lifecycle import (
                PaperLifecycle, _insert_open_trade, _close_trade_in_db
            )
            now_ms = int(time.time() * 1000)
            trade = {
                "id": "test01",
                "pattern": "TRAP",
                "direction": "LONG",
                "entry": 50000.0,
                "sl": 49850.0,
                "tp1": 50300.0,
                "tp2": 50600.0,
                "rr": 2.0,
                "opened_at": now_ms - 60000,
                "context_json": "{}",
            }
            _insert_open_trade(db_path, trade)
            assert _get_open_count(db_path) == 1

            pl = PaperLifecycle.__new__(PaperLifecycle)
            pl._last_decision_ts = 0

            # Price below SL
            exits = pl._check_exits([trade], 49800.0)
            assert len(exits) == 1
            assert exits[0][1] == "SL_HIT"
            assert exits[0][2] == -1.0

            closed_trade = _close_trade_in_db(db_path, "test01", "SL_HIT", -1.0, 49850.0, now_ms, "stop_loss_hit")
            assert _get_open_count(db_path) == 0
            assert _get_closed_count(db_path) == 1
            assert closed_trade is not None
            assert closed_trade["exit_price"] == 49850.0
            assert closed_trade["close_reason"] == "stop_loss_hit"
            assert closed_trade["r_multiple"] == -1.0
            assert closed_trade["closed_at"]


def test_closes_on_tp1_hit():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        with patch.object(config, "PAPER_TRADES_DB", db_path):
            from services.realtime.paper_lifecycle import (
                PaperLifecycle, _insert_open_trade, _close_trade_in_db
            )
            now_ms = int(time.time() * 1000)
            trade = {
                "id": "test02",
                "pattern": "ABSORPTION",
                "direction": "LONG",
                "entry": 50000.0,
                "sl": 49850.0,
                "tp1": 50300.0,
                "tp2": 50600.0,
                "rr": 2.0,
                "opened_at": now_ms - 60000,
                "context_json": "{}",
            }
            _insert_open_trade(db_path, trade)

            pl = PaperLifecycle.__new__(PaperLifecycle)
            pl._last_decision_ts = 0

            exits = pl._check_exits([trade], 50350.0)  # above TP1, below TP2
            assert len(exits) == 1
            assert exits[0][1] == "TP1_HIT"
            assert exits[0][2] > 0, "R should be positive on TP1"
            assert exits[0][3] == 50300.0
            assert exits[0][4] == "tp1_target_hit"


def test_respects_max_open_trades():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        with patch.object(config, "PAPER_TRADES_DB", db_path):
            from services.realtime.paper_lifecycle import PaperLifecycle
            pl = PaperLifecycle.__new__(PaperLifecycle)
            pl._last_decision_ts = 0

            # Create MAX_OPEN_TRADES fake open trades
            open_trades = []
            for i in range(config.MAX_OPEN_TRADES):
                open_trades.append({
                    "id": f"fake{i}",
                    "pattern": "TRAP",
                    "direction": "LONG",
                    "entry": 50000.0,
                    "sl": 49850.0,
                    "tp1": 50300.0,
                    "tp2": 50600.0,
                    "rr": 2.0,
                    "opened_at": 0,
                    "context_json": "{}",
                })

            decision = {
                "decision": "ALLOW_PAPER",
                "timestamp_ms": int(time.time() * 1000),
                "pattern": "TRAP",
                "direction": "SHORT",  # different direction
                "entry": 50000.0,
                "sl": 50150.0,
                "tp1": 49700.0,
                "tp2": 49400.0,
                "rr": 2.0,
                "tags": [],
            }
            result = pl._open_trade(decision, open_trades)
            assert result is None, "Should not open trade when at MAX_OPEN_TRADES"


def test_respects_max_same_direction():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        with patch.object(config, "PAPER_TRADES_DB", db_path):
            from services.realtime.paper_lifecycle import PaperLifecycle
            pl = PaperLifecycle.__new__(PaperLifecycle)
            pl._last_decision_ts = 0

            open_trades = []
            for i in range(config.MAX_SAME_DIRECTION):
                open_trades.append({
                    "id": f"d{i}",
                    "pattern": f"PAT{i}",
                    "direction": "LONG",
                    "entry": 50000.0,
                    "sl": 49850.0,
                    "tp1": 50300.0,
                    "tp2": 50600.0,
                    "rr": 2.0,
                    "opened_at": 0,
                    "context_json": "{}",
                })

            decision = {
                "decision": "ALLOW_PAPER",
                "timestamp_ms": int(time.time() * 1000),
                "pattern": "REVERSAL",
                "direction": "LONG",
                "entry": 50000.0,
                "sl": 49850.0,
                "tp1": 50300.0,
                "tp2": 50600.0,
                "rr": 2.0,
                "tags": [],
            }
            result = pl._open_trade(decision, open_trades)
            assert result is None, "Should not open when MAX_SAME_DIRECTION reached"


def test_open_trade_persists_trade_dna_fields():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        with patch.object(config, "PAPER_TRADES_DB", db_path):
            from services.realtime.paper_lifecycle import PaperLifecycle
            pl = PaperLifecycle.__new__(PaperLifecycle)
            pl._last_decision_ts = 0

            decision = {
                "decision": "ALLOW_PAPER",
                "timestamp_ms": int(time.time() * 1000),
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
                "observer_score": 4.2,
                "delta_at_entry": 1.1,
                "imbalance_at_entry": 0.2,
                "cvd_at_entry": 3.5,
                "body_ratio": 0.4,
                "micro_event": "PRESSURE_BUILDING_LONG",
                "pattern_reason": "wick_sweep_and_reclaim",
                "entry_reason": "reclaim_after_sweep_low",
                "sl_reason": "below_sweep_low_invalidation",
                "tp_reason": "nearest_equal_high_liquidity",
                "session": "LONDON",
                "trend_at_entry": "TREND_UP",
                "regime_at_entry": "EXPANSION",
                "trend_reason": "swings high=4, low=3",
                "regime_reason": "atr=10, delta_consistency=0.8",
                "lineage_chain": "4.20 -> PRESSURE_BUILDING_LONG -> STOP_HUNT_RECLAIM_LONG -> reclaim_after_sweep_low",
                "tags": [],
            }
            trade = pl._open_trade(decision, [])
            assert trade is not None
            open_trade = _get_open_trades(db_path)[0]
            assert open_trade["pattern_reason"] == "wick_sweep_and_reclaim"
            assert open_trade["entry_reason"] == "reclaim_after_sweep_low"
            assert open_trade["sl_reason"] == "below_sweep_low_invalidation"
            assert open_trade["tp_reason"] == "nearest_equal_high_liquidity"
            assert open_trade["context_adjusted_confidence"] == 0.84
            assert open_trade["lineage_chain"] == decision["lineage_chain"]


def test_blocks_duplicate_same_candle_same_timeframe():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        with patch.object(config, "PAPER_TRADES_DB", db_path):
            from services.realtime.paper_lifecycle import PaperLifecycle
            pl = PaperLifecycle.__new__(PaperLifecycle)
            pl._last_decision_ts = 0

            now_ms = int(time.time() * 1000)
            decision = {
                "decision": "ALLOW_PAPER",
                "timestamp_ms": now_ms,
                "pattern": "REVERSAL",
                "timeframe": "1m",
                "direction": "LONG",
                "entry": 50000.0,
                "sl": 49850.0,
                "tp1": 50300.0,
                "tp2": 50600.0,
                "rr": 2.0,
                "tags": [],
            }

            first_trade = pl._open_trade(decision, [])
            assert first_trade is not None
            second_trade = pl._open_trade(decision, _get_open_trades(db_path))
            assert second_trade is None
            assert _get_open_count(db_path) == 1


def test_blocks_same_pattern_direction_timeframe_during_cooldown():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        with patch.object(config, "PAPER_TRADES_DB", db_path):
            from services.realtime.paper_lifecycle import PaperLifecycle
            pl = PaperLifecycle.__new__(PaperLifecycle)
            pl._last_decision_ts = 0

            now_ms = int(time.time() * 1000)
            first_decision = {
                "decision": "ALLOW_PAPER",
                "timestamp_ms": now_ms - 120000,
                "pattern": "REVERSAL",
                "timeframe": "5m",
                "direction": "LONG",
                "entry": 50000.0,
                "sl": 49850.0,
                "tp1": 50300.0,
                "tp2": 50600.0,
                "rr": 2.0,
                "tags": [],
            }
            second_decision = {
                **first_decision,
                "timestamp_ms": now_ms,
            }

            first_trade = pl._open_trade(first_decision, [])
            assert first_trade is not None
            second_trade = pl._open_trade(second_decision, _get_open_trades(db_path))
            assert second_trade is None
            assert _get_open_count(db_path) == 1


def test_allows_same_pattern_on_new_candle_after_cooldown():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        with patch.object(config, "PAPER_TRADES_DB", db_path):
            from services.realtime.paper_lifecycle import PaperLifecycle
            pl = PaperLifecycle.__new__(PaperLifecycle)
            pl._last_decision_ts = 0

            now_ms = int(time.time() * 1000)
            first_decision = {
                "decision": "ALLOW_PAPER",
                "timestamp_ms": now_ms - 400000,
                "pattern": "REVERSAL",
                "timeframe": "5m",
                "direction": "LONG",
                "entry": 50000.0,
                "sl": 49850.0,
                "tp1": 50300.0,
                "tp2": 50600.0,
                "rr": 2.0,
                "tags": [],
            }
            second_decision = {
                **first_decision,
                "timestamp_ms": now_ms,
            }

            first_trade = pl._open_trade(first_decision, [])
            assert first_trade is not None
            existing = _get_open_trades(db_path)
            existing[0]["opened_at_epoch"] = now_ms - 400000
            second_trade = pl._open_trade(second_decision, existing)
            assert second_trade is not None


def test_closes_trade_when_thesis_broken():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        with patch.object(config, "PAPER_TRADES_DB", db_path):
            from services.realtime.paper_lifecycle import PaperLifecycle, _insert_open_trade
            opened_at_epoch = int(time.time() * 1000) - 60000
            trade = {
                "id": "broken01",
                "pattern": "TRAP",
                "timeframe": "1m",
                "direction": "LONG",
                "entry": 50000.0,
                "sl": 49850.0,
                "tp1": 50300.0,
                "tp2": 50600.0,
                "rr": 2.0,
                "opened_at_epoch": opened_at_epoch,
                "opened_at": opened_at_epoch,
                "context_json": "{}",
            }
            _insert_open_trade(db_path, trade)

            pl = PaperLifecycle.__new__(PaperLifecycle)
            pl._last_decision_ts = 0
            exits = pl._check_exits([trade], 49940.0, {"cvd": -0.7, "body_ratio": 0.7})
            assert len(exits) == 1
            assert exits[0][1] == "THESIS_BROKEN"
            assert exits[0][4] == "thesis_broken"
