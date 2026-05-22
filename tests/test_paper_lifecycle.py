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
            "min_exit_delay_seconds",
            "exit_armed_at",
            "mae",
            "mfe",
        ):
            assert required in open_columns, f"Missing column: {required}"

        for required in ("closed_at", "exit_price", "r_multiple", "close_reason"):
            assert required in closed_columns, f"Missing closed column: {required}"


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
                # exit_armed_at = 0 → treated as immediately armed (backward compat)
            }
            _insert_open_trade(db_path, trade)
            assert _get_open_count(db_path) == 1

            pl = PaperLifecycle.__new__(PaperLifecycle)
            pl._last_decision_ts = 0

            # Price below SL — trade has exit_armed_at=0 so is_armed=True
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
            # Arming fields should be set
            assert open_trade["min_exit_delay_seconds"] == 30  # STOP_HUNT_RECLAIM
            assert open_trade["exit_armed_at"] > 0


# ── GÖREV 1: Arming Phase Tests ──────────────────────────────────────────────

def test_stop_hunt_sl_blocked_within_arming_window():
    """STOP_HUNT trade açıldıktan 1 saniye içinde SL çalışmamalı."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        from services.realtime.paper_lifecycle import PaperLifecycle
        pl = PaperLifecycle.__new__(PaperLifecycle)

        now_ms = int(time.time() * 1000)
        # opened 1 second ago, armed in 29 more seconds
        trade = {
            "id": "sh01",
            "pattern": "STOP_HUNT_RECLAIM_LONG",
            "direction": "LONG",
            "entry": 50000.0,
            "sl": 49850.0,
            "tp1": 50300.0,
            "tp2": 50600.0,
            "opened_at_epoch": now_ms - 1000,
            "exit_armed_at": now_ms + 29000,  # armed in future
            "min_exit_delay_seconds": 30,
            "context_json": "{}",
            "mae": 0,
            "mfe": 0,
        }
        # price at SL, but adverse_move=150 < emergency_threshold(entry*0.015=750)
        exits = pl._check_exits([trade], 49850.0)
        assert len(exits) == 0, "SL must not fire within arming window without emergency"


def test_stop_hunt_sl_allowed_after_arming():
    """STOP_HUNT trade 30 saniye sonra SL çalışmalı."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        from services.realtime.paper_lifecycle import PaperLifecycle
        pl = PaperLifecycle.__new__(PaperLifecycle)

        now_ms = int(time.time() * 1000)
        # exit_armed_at is 1 second in the past → armed
        trade = {
            "id": "sh02",
            "pattern": "STOP_HUNT_RECLAIM_LONG",
            "direction": "LONG",
            "entry": 50000.0,
            "sl": 49850.0,
            "tp1": 50300.0,
            "tp2": 50600.0,
            "opened_at_epoch": now_ms - 31000,
            "exit_armed_at": now_ms - 1000,  # armed 1 second ago
            "min_exit_delay_seconds": 30,
            "context_json": "{}",
            "mae": 0,
            "mfe": 0,
        }
        exits = pl._check_exits([trade], 49800.0)
        assert len(exits) == 1
        assert exits[0][1] == "SL_HIT"
        assert exits[0][4] == "stop_loss_hit"


def test_continuation_exit_blocked_before_60s():
    """CONTINUATION trade 30 saniyede exit olmamalı."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        from services.realtime.paper_lifecycle import PaperLifecycle
        pl = PaperLifecycle.__new__(PaperLifecycle)

        now_ms = int(time.time() * 1000)
        trade = {
            "id": "cont01",
            "pattern": "CONTINUATION_LONG",
            "direction": "LONG",
            "entry": 50000.0,
            "sl": 49850.0,
            "tp1": 50300.0,
            "tp2": 50600.0,
            "opened_at_epoch": now_ms - 30000,  # 30s ago
            "exit_armed_at": now_ms + 30000,    # 60s total, 30s remaining
            "min_exit_delay_seconds": 60,
            "context_json": "{}",
            "mae": 0,
            "mfe": 0,
        }
        # SL triggered but not armed
        exits = pl._check_exits([trade], 49849.0)
        assert len(exits) == 0, "CONTINUATION exit must be blocked before 60s arming"


def test_continuation_exit_allowed_after_60s():
    """CONTINUATION trade 60 saniye sonra exit olabilmeli."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        from services.realtime.paper_lifecycle import PaperLifecycle
        pl = PaperLifecycle.__new__(PaperLifecycle)

        now_ms = int(time.time() * 1000)
        trade = {
            "id": "cont02",
            "pattern": "CONTINUATION_LONG",
            "direction": "LONG",
            "entry": 50000.0,
            "sl": 49850.0,
            "tp1": 50300.0,
            "tp2": 50600.0,
            "opened_at_epoch": now_ms - 61000,
            "exit_armed_at": now_ms - 1000,    # armed
            "min_exit_delay_seconds": 60,
            "context_json": "{}",
            "mae": 0,
            "mfe": 0,
        }
        exits = pl._check_exits([trade], 49800.0)
        assert len(exits) == 1
        assert exits[0][1] == "SL_HIT"


def test_emergency_bypass_fires_sl_before_arming():
    """ATR*1.2 adverse hareketi arming bypass edip SL çalıştırmalı."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        from services.realtime.paper_lifecycle import PaperLifecycle
        pl = PaperLifecycle.__new__(PaperLifecycle)

        now_ms = int(time.time() * 1000)
        entry = 50000.0
        trade = {
            "id": "emg01",
            "pattern": "STOP_HUNT_RECLAIM_LONG",
            "direction": "LONG",
            "entry": entry,
            "sl": 49850.0,
            "tp1": 50300.0,
            "tp2": 50600.0,
            "opened_at_epoch": now_ms - 1000,
            "exit_armed_at": now_ms + 29000,  # not yet armed
            "min_exit_delay_seconds": 30,
            "context_json": "{}",
            "mae": 0,
            "mfe": 0,
        }
        # regime_atr = 100, emergency_threshold = 100 * 1.2 = 120
        # price at 49870 → adverse_move = 50000 - 49870 = 130 > 120 → emergency
        price = 49870.0
        exits = pl._check_exits([trade], price, regime_atr=100.0)
        assert len(exits) == 1
        assert exits[0][1] == "SL_HIT"
        assert exits[0][4] == "EMERGENCY_ADVERSE_MOVE"


def test_emergency_bypass_not_for_tp():
    """Emergency bypass sadece SL içindir; TP arming olmadan çalışmamalı."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        from services.realtime.paper_lifecycle import PaperLifecycle
        pl = PaperLifecycle.__new__(PaperLifecycle)

        now_ms = int(time.time() * 1000)
        trade = {
            "id": "emgtp01",
            "pattern": "STOP_HUNT_RECLAIM_LONG",
            "direction": "LONG",
            "entry": 50000.0,
            "sl": 49850.0,
            "tp1": 50300.0,
            "tp2": 50600.0,
            "opened_at_epoch": now_ms - 1000,
            "exit_armed_at": now_ms + 29000,  # not yet armed
            "min_exit_delay_seconds": 30,
            "context_json": "{}",
            "mae": 0,
            "mfe": 0,
        }
        # price above TP1, not armed — TP should NOT fire
        exits = pl._check_exits([trade], 50400.0)
        assert len(exits) == 0, "TP must not fire during arming phase"


# ── GÖREV 2: THESIS_BROKEN Tests ─────────────────────────────────────────────

def test_thesis_broken_blocked_during_arming():
    """THESIS_BROKEN arming dolmadan çalışmamalı."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        from services.realtime.paper_lifecycle import PaperLifecycle
        pl = PaperLifecycle.__new__(PaperLifecycle)

        now_ms = int(time.time() * 1000)
        trade = {
            "id": "tb01",
            "pattern": "STOP_HUNT_RECLAIM_LONG",
            "direction": "LONG",
            "entry": 50000.0,
            "sl": 49850.0,
            "tp1": 50300.0,
            "tp2": 50600.0,
            "opened_at_epoch": now_ms - 5000,
            "exit_armed_at": now_ms + 25000,  # not yet armed
            "min_exit_delay_seconds": 30,
            "context_json": "{}",
            "mae": 0,
            "mfe": 0,
        }
        # THESIS_BROKEN conditions: cvd<-0.5, body_ratio>0.6, price<entry
        candle_dna = {"cvd": -0.7, "body_ratio": 0.7}
        exits = pl._check_exits([trade], 49950.0, candle_dna=candle_dna)
        assert len(exits) == 0, "THESIS_BROKEN must not fire during arming"


def test_stop_hunt_reclaim_long_thesis_broken():
    """STOP_HUNT_RECLAIM_LONG reclaim kaybedilince thesis broken çalışmalı."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        from services.realtime.paper_lifecycle import PaperLifecycle
        pl = PaperLifecycle.__new__(PaperLifecycle)

        now_ms = int(time.time() * 1000)
        trade = {
            "id": "tb02",
            "pattern": "STOP_HUNT_RECLAIM_LONG",
            "direction": "LONG",
            "entry": 50000.0,
            "sl": 49800.0,
            "tp1": 50300.0,
            "tp2": 50600.0,
            "opened_at_epoch": now_ms - 35000,
            "exit_armed_at": now_ms - 5000,  # armed
            "min_exit_delay_seconds": 30,
            "context_json": "{}",
            "mae": 0,
            "mfe": 0,
        }
        # cvd < -0.5, body_ratio > 0.6, price < entry → THESIS_BROKEN
        candle_dna = {"cvd": -0.7, "body_ratio": 0.75}
        exits = pl._check_exits([trade], 49900.0, candle_dna=candle_dna)
        assert len(exits) == 1
        assert exits[0][1] == "THESIS_BROKEN"
        assert exits[0][4] == "thesis_broken"


def test_stop_hunt_reclaim_long_no_thesis_broken_without_conditions():
    """Conditions tam değilse THESIS_BROKEN çalışmamalı."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        from services.realtime.paper_lifecycle import PaperLifecycle
        pl = PaperLifecycle.__new__(PaperLifecycle)

        now_ms = int(time.time() * 1000)
        trade = {
            "id": "tb03",
            "pattern": "STOP_HUNT_RECLAIM_LONG",
            "direction": "LONG",
            "entry": 50000.0,
            "sl": 49800.0,
            "tp1": 50300.0,
            "tp2": 50600.0,
            "opened_at_epoch": now_ms - 35000,
            "exit_armed_at": now_ms - 5000,  # armed
            "min_exit_delay_seconds": 30,
            "context_json": "{}",
            "mae": 0,
            "mfe": 0,
        }
        # cvd is not negative enough → no THESIS_BROKEN
        candle_dna = {"cvd": -0.3, "body_ratio": 0.75}
        exits = pl._check_exits([trade], 49900.0, candle_dna=candle_dna)
        assert len(exits) == 0


# ── GÖREV 5: MAE/MFE Tests ───────────────────────────────────────────────────

def test_mae_mfe_columns_exist():
    """MAE/MFE kolonları hem open hem closed tabloda olmalı."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        open_cols = _get_columns(db_path, "open_trades")
        closed_cols = _get_columns(db_path, "closed_trades")
        assert "mae" in open_cols
        assert "mfe" in open_cols
        assert "mae" in closed_cols
        assert "mfe" in closed_cols


def test_mae_mfe_update_and_read():
    """MAE/MFE her saniye güncellenebilmeli."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        from services.realtime.paper_lifecycle import (
            _insert_open_trade, _update_mae_mfe, _get_open_trades
        )
        now_ms = int(time.time() * 1000)
        trade = {
            "id": "maetest",
            "pattern": "STOP_HUNT_RECLAIM_LONG",
            "direction": "LONG",
            "entry": 50000.0,
            "sl": 49850.0,
            "tp1": 50300.0,
            "tp2": 50600.0,
            "opened_at_epoch": now_ms,
            "context_json": "{}",
        }
        _insert_open_trade(db_path, trade)

        # Adverse move
        _update_mae_mfe(db_path, "maetest", 200.0, 0.0)
        trades = _get_open_trades(db_path)
        assert abs(trades[0]["mae"] - 200.0) < 0.01
        assert abs(trades[0]["mfe"] - 0.0) < 0.01

        # Favorable move added
        _update_mae_mfe(db_path, "maetest", 200.0, 100.0)
        trades = _get_open_trades(db_path)
        assert abs(trades[0]["mae"] - 200.0) < 0.01
        assert abs(trades[0]["mfe"] - 100.0) < 0.01


def test_mae_mfe_carried_to_closed_trade():
    """MAE/MFE kapanışta closed_trades'e taşınmalı."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _setup_temp_db(tmp)
        from services.realtime.paper_lifecycle import (
            _insert_open_trade, _update_mae_mfe, _close_trade_in_db
        )
        now_ms = int(time.time() * 1000)
        trade = {
            "id": "maecls",
            "pattern": "CONTINUATION_LONG",
            "direction": "LONG",
            "entry": 50000.0,
            "sl": 49850.0,
            "tp1": 50300.0,
            "tp2": 50600.0,
            "opened_at_epoch": now_ms - 70000,
            "context_json": "{}",
        }
        _insert_open_trade(db_path, trade)
        _update_mae_mfe(db_path, "maecls", 75.0, 180.0)

        closed = _close_trade_in_db(db_path, "maecls", "TP1_HIT", 2.0, 50300.0, now_ms, "tp1_target_hit")
        assert closed is not None
        assert abs(closed["mae"] - 75.0) < 0.01
        assert abs(closed["mfe"] - 180.0) < 0.01


def test_arming_sets_correct_delay_for_patterns():
    """_min_arming_seconds doğru değerleri döndürmeli."""
    from services.realtime.paper_lifecycle import _min_arming_seconds
    assert _min_arming_seconds("STOP_HUNT_RECLAIM_LONG") == 30
    assert _min_arming_seconds("STOP_HUNT_RECLAIM_SHORT") == 30
    assert _min_arming_seconds("CONTINUATION_LONG") == 60
    assert _min_arming_seconds("CONTINUATION_SHORT") == 60
    assert _min_arming_seconds("EXPANSION") == 15
    assert _min_arming_seconds("REVERSAL") == 60
    assert _min_arming_seconds("ABSORPTION") == 60
    assert _min_arming_seconds("TRAP") == 30  # default
