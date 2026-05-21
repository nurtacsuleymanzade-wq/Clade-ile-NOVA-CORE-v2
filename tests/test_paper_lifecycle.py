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

            _close_trade_in_db(db_path, "test01", "SL_HIT", -1.0, now_ms)
            assert _get_open_count(db_path) == 0
            assert _get_closed_count(db_path) == 1


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
