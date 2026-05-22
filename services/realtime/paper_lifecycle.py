"""
Opens/closes paper trades based on decision_gate output.
Monitors price via bookTicker, closes on SL/TP/THESIS_BROKEN/TIMEOUT.
Includes arming phase, emergency bypass, and MAE/MFE tracking.
"""
import asyncio
import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

logger = logging.getLogger(__name__)

BOOK_TICKER_URL = f"{config.BINANCE_REST}/api/v3/ticker/bookTicker?symbol={config.SYMBOL}"
TIMEOUT_SECONDS = config.TRADE_TIMEOUT_HOURS * 3600
WIN_RESULTS = {"TP1_HIT", "TP2_HIT"}

OPEN_TRADE_SCHEMA = [
    ("id", "TEXT PRIMARY KEY", "''"),
    ("pattern", "TEXT", "''"),
    ("timeframe", "TEXT", "''"),
    ("direction", "TEXT", "''"),
    ("entry", "REAL", "0"),
    ("sl", "REAL", "0"),
    ("tp1", "REAL", "0"),
    ("tp2", "REAL", "0"),
    ("rr", "REAL", "0"),
    ("confidence", "REAL", "0"),
    ("context_adjusted_confidence", "REAL", "0"),
    ("observer_score", "REAL", "0"),
    ("delta_at_entry", "REAL", "0"),
    ("imbalance_at_entry", "REAL", "0"),
    ("cvd_at_entry", "REAL", "0"),
    ("body_ratio", "REAL", "0"),
    ("micro_event", "TEXT", "''"),
    ("pattern_reason", "TEXT", "''"),
    ("entry_reason", "TEXT", "''"),
    ("sl_reason", "TEXT", "''"),
    ("tp_reason", "TEXT", "''"),
    ("session", "TEXT", "''"),
    ("trend_at_entry", "TEXT", "''"),
    ("regime_at_entry", "TEXT", "''"),
    ("opened_at", "TEXT", "''"),
    ("opened_at_epoch", "INTEGER", "0"),
    ("context_json", "TEXT", "'{}'"),
    ("min_exit_delay_seconds", "INTEGER", "0"),
    ("exit_armed_at", "INTEGER", "0"),
    ("mae", "REAL", "0"),
    ("mfe", "REAL", "0"),
]

CLOSED_EXTRA_SCHEMA = [
    ("closed_at", "TEXT", "''"),
    ("closed_at_epoch", "INTEGER", "0"),
    ("result", "TEXT", "''"),
    ("exit_price", "REAL", "0"),
    ("r_multiple", "REAL", "0"),
    ("R", "REAL", "0"),
    ("duration_seconds", "INTEGER", "0"),
    ("close_reason", "TEXT", "''"),
]

OPEN_TRADE_COLUMNS = [name for name, _, _ in OPEN_TRADE_SCHEMA]
CLOSED_TRADE_COLUMNS = OPEN_TRADE_COLUMNS + [name for name, _, _ in CLOSED_EXTRA_SCHEMA]
# Backward-compatible alias for callers still referencing the pre-split name.
TRADE_COLUMNS = OPEN_TRADE_COLUMNS


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _utc_iso_from_ms(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_from_utc_iso(timestamp_text: str | int | float | None) -> int:
    if not timestamp_text:
        return 0
    if isinstance(timestamp_text, (int, float)):
        return int(timestamp_text)
    try:
        parsed = datetime.fromisoformat(str(timestamp_text).replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return 0


def _coerce_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _coerce_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _default_close_reason(result: str) -> str:
    mapping = {
        "SL_HIT": "stop_loss_hit",
        "TP1_HIT": "tp1_target_hit",
        "TP2_HIT": "tp2_target_hit",
        "TIMEOUT": "timeout_no_target_hit",
        "THESIS_BROKEN": "thesis_broken",
    }
    return mapping.get(result, result.lower() if result else "")


def _ensure_columns(cursor: sqlite3.Cursor, table_name: str, schema: list[tuple[str, str, str]]) -> None:
    cursor.execute(f"PRAGMA table_info({table_name})")
    existing = {row[1] for row in cursor.fetchall()}
    for column_name, column_type, default_sql in schema:
        if column_name in existing:
            continue
        cursor.execute(
            f"ALTER TABLE {table_name} "
            f"ADD COLUMN {column_name} {column_type} DEFAULT {default_sql}"
        )


def _init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    open_defs = ",\n            ".join(f"{name} {column_type}" for name, column_type, _ in OPEN_TRADE_SCHEMA)
    closed_defs = ",\n            ".join(
        f"{name} {column_type}" for name, column_type, _ in OPEN_TRADE_SCHEMA + CLOSED_EXTRA_SCHEMA
    )

    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS open_trades (
            {open_defs}
        )
        """
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS closed_trades (
            {closed_defs}
        )
        """
    )

    _ensure_columns(cur, "open_trades", OPEN_TRADE_SCHEMA)
    _ensure_columns(cur, "closed_trades", OPEN_TRADE_SCHEMA + CLOSED_EXTRA_SCHEMA)

    conn.commit()
    conn.close()


def _normalize_open_trade(trade: dict) -> dict:
    opened_at_epoch = _coerce_int(trade.get("opened_at_epoch"))
    if opened_at_epoch <= 0:
        opened_at_epoch = _ms_from_utc_iso(trade.get("opened_at"))
    if opened_at_epoch <= 0:
        opened_at_epoch = int(time.time() * 1000)

    opened_at_text = trade.get("opened_at") or _utc_iso_from_ms(opened_at_epoch)
    context_json = trade.get("context_json")
    if not context_json:
        context_json = "{}"
    elif not isinstance(context_json, str):
        context_json = json.dumps(context_json, ensure_ascii=False)

    return {
        "id": str(trade.get("id", "")),
        "pattern": str(trade.get("pattern", "")),
        "timeframe": str(trade.get("timeframe", "")),
        "direction": str(trade.get("direction", "")),
        "entry": _coerce_float(trade.get("entry")),
        "sl": _coerce_float(trade.get("sl")),
        "tp1": _coerce_float(trade.get("tp1")),
        "tp2": _coerce_float(trade.get("tp2")),
        "rr": _coerce_float(trade.get("rr")),
        "confidence": _coerce_float(trade.get("confidence")),
        "context_adjusted_confidence": _coerce_float(trade.get("context_adjusted_confidence")),
        "observer_score": _coerce_float(trade.get("observer_score")),
        "delta_at_entry": _coerce_float(trade.get("delta_at_entry")),
        "imbalance_at_entry": _coerce_float(trade.get("imbalance_at_entry")),
        "cvd_at_entry": _coerce_float(trade.get("cvd_at_entry")),
        "body_ratio": _coerce_float(trade.get("body_ratio")),
        "micro_event": str(trade.get("micro_event", "")),
        "pattern_reason": str(trade.get("pattern_reason", "")),
        "entry_reason": str(trade.get("entry_reason", "")),
        "sl_reason": str(trade.get("sl_reason", "")),
        "tp_reason": str(trade.get("tp_reason", "")),
        "session": str(trade.get("session", "")),
        "trend_at_entry": str(trade.get("trend_at_entry", "")),
        "regime_at_entry": str(trade.get("regime_at_entry", "")),
        "opened_at": opened_at_text,
        "opened_at_epoch": opened_at_epoch,
        "context_json": context_json,
        "min_exit_delay_seconds": _coerce_int(trade.get("min_exit_delay_seconds")),
        "exit_armed_at": _coerce_int(trade.get("exit_armed_at")),
        "mae": _coerce_float(trade.get("mae")),
        "mfe": _coerce_float(trade.get("mfe")),
    }


def _normalize_closed_trade(trade: dict) -> dict:
    closed_at_epoch = _coerce_int(trade.get("closed_at_epoch"))
    if closed_at_epoch <= 0:
        closed_at_epoch = _ms_from_utc_iso(trade.get("closed_at"))
    closed_at_text = trade.get("closed_at") or _utc_iso_from_ms(closed_at_epoch or int(time.time() * 1000))

    opened_at_epoch = _coerce_int(trade.get("opened_at_epoch"))
    if opened_at_epoch <= 0:
        opened_at_epoch = _ms_from_utc_iso(trade.get("opened_at"))
    duration_seconds = _coerce_int(trade.get("duration_seconds"))
    if duration_seconds <= 0 and opened_at_epoch and closed_at_epoch:
        duration_seconds = max(0, (closed_at_epoch - opened_at_epoch) // 1000)

    base = _normalize_open_trade(trade)
    r_multiple = _coerce_float(trade.get("r_multiple", trade.get("R")))
    return {
        **base,
        "closed_at": closed_at_text,
        "closed_at_epoch": closed_at_epoch,
        "result": str(trade.get("result", "")),
        "exit_price": _coerce_float(trade.get("exit_price")),
        "r_multiple": r_multiple,
        "R": r_multiple,
        "duration_seconds": duration_seconds,
        "close_reason": str(trade.get("close_reason", "")),
    }


def _get_open_trades(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM open_trades ORDER BY opened_at_epoch ASC, id ASC")
    rows = [_normalize_open_trade(dict(r)) for r in cur.fetchall()]
    conn.close()
    return rows


def _insert_open_trade(db_path: Path, trade: dict) -> None:
    normalized = _normalize_open_trade(trade)
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    columns = ", ".join(OPEN_TRADE_COLUMNS)
    placeholders = ", ".join(f":{column}" for column in OPEN_TRADE_COLUMNS)
    cur.execute(
        f"INSERT INTO open_trades ({columns}) VALUES ({placeholders})",
        normalized,
    )
    conn.commit()
    conn.close()


def _update_mae_mfe(db_path: Path, trade_id: str, mae: float, mfe: float) -> None:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        "UPDATE open_trades SET mae=?, mfe=? WHERE id=?",
        (round(mae, 6), round(mfe, 6), trade_id),
    )
    conn.commit()
    conn.close()


def _close_trade_in_db(
    db_path: Path,
    trade_id: str,
    result: str,
    r_multiple: float,
    exit_price: float,
    closed_at_epoch: int,
    close_reason: str,
) -> dict | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM open_trades WHERE id=?", (trade_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        return None

    trade = _normalize_open_trade(dict(row))
    duration_seconds = max(0, (closed_at_epoch - trade["opened_at_epoch"]) // 1000)
    closed_record = _normalize_closed_trade({
        **trade,
        "closed_at": _utc_iso_from_ms(closed_at_epoch),
        "closed_at_epoch": closed_at_epoch,
        "result": result,
        "exit_price": exit_price,
        "r_multiple": round(r_multiple, 4),
        "duration_seconds": duration_seconds,
        "close_reason": close_reason or _default_close_reason(result),
    })

    columns = ", ".join(CLOSED_TRADE_COLUMNS)
    placeholders = ", ".join(f":{column}" for column in CLOSED_TRADE_COLUMNS)
    cur.execute(
        f"INSERT OR REPLACE INTO closed_trades ({columns}) VALUES ({placeholders})",
        closed_record,
    )
    cur.execute("DELETE FROM open_trades WHERE id=?", (trade_id,))
    conn.commit()
    conn.close()
    return closed_record


def _append_closed_trade(closed_trade: dict) -> None:
    line = json.dumps(closed_trade, ensure_ascii=False) + "\n"
    with open(config.CLOSED_TRADES_FILE, "a", encoding="utf-8") as f:
        f.write(line)


async def _fetch_price(session: aiohttp.ClientSession) -> float | None:
    try:
        async with session.get(BOOK_TICKER_URL, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data = await resp.json()
            return (float(data["bidPrice"]) + float(data["askPrice"])) / 2
    except Exception as e:
        logger.warning(f"Price fetch error: {e}")
        return None


def _min_arming_seconds(pattern: str) -> int:
    if pattern.startswith("STOP_HUNT_RECLAIM_"):
        return 30
    if pattern.startswith("CONTINUATION_"):
        return 60
    if pattern == "EXPANSION":
        return 15
    if pattern in ("REVERSAL", "ABSORPTION"):
        return 60
    return 30


def _check_thesis_broken(trade: dict, price: float, candle_dna: dict | None) -> bool:
    if not candle_dna:
        return False
    pattern = trade.get("pattern", "")
    direction = trade.get("direction", "")
    entry = _coerce_float(trade.get("entry"))
    cvd = _coerce_float(candle_dna.get("cvd"))
    body_ratio = _coerce_float(candle_dna.get("body_ratio"))

    is_sh_long = pattern.startswith("STOP_HUNT_RECLAIM_LONG") or (
        pattern.startswith("STOP_HUNT_RECLAIM") and direction == "LONG"
    )
    is_sh_short = pattern.startswith("STOP_HUNT_RECLAIM_SHORT") or (
        pattern.startswith("STOP_HUNT_RECLAIM") and direction == "SHORT"
    )
    is_cont_long = pattern == "CONTINUATION_LONG" or (
        pattern.startswith("CONTINUATION") and direction == "LONG"
    )
    is_cont_short = pattern == "CONTINUATION_SHORT" or (
        pattern.startswith("CONTINUATION") and direction == "SHORT"
    )

    if is_sh_long:
        return cvd < -0.5 and body_ratio > 0.6 and price < entry
    if is_sh_short:
        return cvd > 0.5 and body_ratio > 0.6 and price > entry
    if is_cont_long:
        return cvd < -0.3 and price < entry * 0.999
    if is_cont_short:
        return cvd > 0.3 and price > entry * 1.001
    return False


class PaperLifecycle:
    def __init__(self):
        _init_db(config.PAPER_TRADES_DB)
        self._last_decision_ts: int = 0

    def _can_open(self, direction: str, pattern: str, open_trades: list[dict]) -> bool:
        if len(open_trades) >= config.MAX_OPEN_TRADES:
            return False
        same_dir = sum(1 for t in open_trades if t["direction"] == direction)
        if same_dir >= config.MAX_SAME_DIRECTION:
            return False
        same_pat = sum(1 for t in open_trades if t["pattern"] == pattern)
        if same_pat >= config.MAX_SAME_PATTERN:
            return False
        return True

    def _open_trade(self, decision: dict, open_trades: list[dict]) -> dict | None:
        direction = decision.get("direction", "NEUTRAL")
        pattern = decision.get("pattern", "NONE")
        if not self._can_open(direction, pattern, open_trades):
            logger.info(f"Trade limit reached for {pattern} {direction}")
            return None

        now_ms = int(time.time() * 1000)
        trade_id = str(uuid.uuid4())[:8]
        min_exit_delay_seconds = _min_arming_seconds(pattern)
        exit_armed_at = now_ms + min_exit_delay_seconds * 1000

        context = {
            "tags": decision.get("tags", []),
            "combo_key": decision.get("combo_key", ""),
            "trend_reason": decision.get("trend_reason", ""),
            "regime_reason": decision.get("regime_reason", ""),
            "lineage_chain": decision.get("lineage_chain", ""),
        }
        trade = _normalize_open_trade({
            "id": trade_id,
            "pattern": pattern,
            "timeframe": decision.get("timeframe", "1m"),
            "direction": direction,
            "entry": decision.get("entry"),
            "sl": decision.get("sl"),
            "tp1": decision.get("tp1"),
            "tp2": decision.get("tp2", decision.get("tp1", 0)),
            "rr": decision.get("rr", 0.0),
            "confidence": decision.get("confidence", 0.0),
            "context_adjusted_confidence": decision.get("context_adjusted_confidence", 0.0),
            "observer_score": decision.get("observer_score", 0.0),
            "delta_at_entry": decision.get("delta_at_entry", 0.0),
            "imbalance_at_entry": decision.get("imbalance_at_entry", 0.0),
            "cvd_at_entry": decision.get("cvd_at_entry", 0.0),
            "body_ratio": decision.get("body_ratio", 0.0),
            "micro_event": decision.get("micro_event", ""),
            "pattern_reason": decision.get("pattern_reason", ""),
            "entry_reason": decision.get("entry_reason", ""),
            "sl_reason": decision.get("sl_reason", ""),
            "tp_reason": decision.get("tp_reason", ""),
            "session": decision.get("session", ""),
            "trend_at_entry": decision.get("trend_at_entry", ""),
            "regime_at_entry": decision.get("regime_at_entry", ""),
            "opened_at": _utc_iso_from_ms(now_ms),
            "opened_at_epoch": now_ms,
            "context_json": context,
            "min_exit_delay_seconds": min_exit_delay_seconds,
            "exit_armed_at": exit_armed_at,
            "mae": 0.0,
            "mfe": 0.0,
        })
        _insert_open_trade(config.PAPER_TRADES_DB, trade)
        logger.info(
            "Opened paper trade %s: %s %s entry=%s conf=%.3f ctx_conf=%.3f armed_in=%ds",
            trade_id,
            pattern,
            direction,
            trade["entry"],
            trade["confidence"],
            trade["context_adjusted_confidence"],
            min_exit_delay_seconds,
        )
        return trade

    def _check_exits(
        self,
        trades: list[dict],
        price: float,
        candle_dna: dict | None = None,
        regime_atr: float | None = None,
    ) -> list[tuple[str, str, float, float, str]]:
        exits: list[tuple[str, str, float, float, str]] = []
        now_ms = int(time.time() * 1000)

        for trade in trades:
            direction = trade["direction"]
            entry = _coerce_float(trade["entry"])
            sl = _coerce_float(trade["sl"])
            tp1 = _coerce_float(trade["tp1"])
            tp2 = _coerce_float(trade.get("tp2", tp1))
            opened_at_epoch = _coerce_int(trade.get("opened_at_epoch"))
            if opened_at_epoch <= 0:
                opened_at_epoch = _ms_from_utc_iso(trade.get("opened_at"))
            exit_armed_at = _coerce_int(trade.get("exit_armed_at"))
            risk = abs(entry - sl) or 1e-9

            # TIMEOUT always fires regardless of arming
            if (now_ms - opened_at_epoch) >= TIMEOUT_SECONDS * 1000:
                r_multiple = (price - entry) / risk if direction == "LONG" else (entry - price) / risk
                exits.append((trade["id"], "TIMEOUT", round(r_multiple, 4), round(price, 2), "timeout_no_target_hit"))
                continue

            # Arming gate: exit_armed_at <= 0 means old trade without arming → treat as armed
            is_armed = (exit_armed_at <= 0) or (now_ms >= exit_armed_at)

            # Emergency bypass threshold (SL only)
            if regime_atr and regime_atr > 0:
                emergency_threshold = regime_atr * 1.2
            else:
                emergency_threshold = entry * 0.015

            if direction == "LONG":
                adverse_move = entry - price
                sl_triggered = price <= sl
            else:
                adverse_move = price - entry
                sl_triggered = price >= sl

            is_emergency = adverse_move > emergency_threshold

            # Emergency bypass: adverse move exceeds threshold during arming → close now
            if not is_armed and is_emergency:
                r_multiple = (price - entry) / risk if direction == "LONG" else (entry - price) / risk
                exits.append((trade["id"], "SL_HIT", round(r_multiple, 4), round(price, 2), "EMERGENCY_ADVERSE_MOVE"))
                continue

            # SL check: fires when armed and price reaches SL level
            if sl_triggered:
                if is_armed:
                    exits.append((trade["id"], "SL_HIT", -1.0, round(sl, 2), "stop_loss_hit"))
                # Price is at/past SL — skip TP and THESIS_BROKEN regardless
                continue

            # Not yet armed: skip TP and THESIS_BROKEN
            if not is_armed:
                continue

            # TP and THESIS_BROKEN only when armed and SL not triggered
            if direction == "LONG":
                if price >= tp2:
                    r_multiple = (tp2 - entry) / risk
                    exits.append((trade["id"], "TP2_HIT", round(r_multiple, 4), round(tp2, 2), "tp2_target_hit"))
                elif price >= tp1:
                    r_multiple = (tp1 - entry) / risk
                    exits.append((trade["id"], "TP1_HIT", round(r_multiple, 4), round(tp1, 2), "tp1_target_hit"))
                elif _check_thesis_broken(trade, price, candle_dna):
                    r_multiple = (price - entry) / risk
                    exits.append((trade["id"], "THESIS_BROKEN", round(r_multiple, 4), round(price, 2), "thesis_broken"))
            else:
                if price <= tp2:
                    r_multiple = (entry - tp2) / risk
                    exits.append((trade["id"], "TP2_HIT", round(r_multiple, 4), round(tp2, 2), "tp2_target_hit"))
                elif price <= tp1:
                    r_multiple = (entry - tp1) / risk
                    exits.append((trade["id"], "TP1_HIT", round(r_multiple, 4), round(tp1, 2), "tp1_target_hit"))
                elif _check_thesis_broken(trade, price, candle_dna):
                    r_multiple = (entry - price) / risk
                    exits.append((trade["id"], "THESIS_BROKEN", round(r_multiple, 4), round(price, 2), "thesis_broken"))

        return exits

    async def run(self) -> None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)

        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    decision = _load_json(config.DECISION_FILE) or {}
                    price = await _fetch_price(session)
                    open_trades = _get_open_trades(config.PAPER_TRADES_DB)

                    decision_ts = decision.get("timestamp_ms", 0)
                    if (
                        decision.get("decision") == "ALLOW_PAPER"
                        and decision_ts != self._last_decision_ts
                    ):
                        self._open_trade(decision, open_trades)
                        self._last_decision_ts = decision_ts
                        open_trades = _get_open_trades(config.PAPER_TRADES_DB)

                    if price is not None:
                        # Update MAE/MFE for every open trade each tick
                        for trade in open_trades:
                            t_entry = _coerce_float(trade["entry"])
                            t_direction = trade["direction"]
                            t_mae = _coerce_float(trade.get("mae", 0))
                            t_mfe = _coerce_float(trade.get("mfe", 0))
                            if t_direction == "LONG":
                                new_mae = max(t_mae, t_entry - price)
                                new_mfe = max(t_mfe, price - t_entry)
                            else:
                                new_mae = max(t_mae, price - t_entry)
                                new_mfe = max(t_mfe, t_entry - price)
                            _update_mae_mfe(config.PAPER_TRADES_DB, trade["id"], new_mae, new_mfe)

                        # Read regime ATR for emergency bypass
                        regime_data = _load_json(config.REGIME_FILE) or {}
                        regime_atr = _coerce_float(regime_data.get("atr")) or None
                        candle_dna = _load_json(config.CANDLE_DNA_STATE_FILE)

                        # Re-read trades to include updated MAE/MFE
                        open_trades = _get_open_trades(config.PAPER_TRADES_DB)

                        exits = self._check_exits(open_trades, price, candle_dna, regime_atr)
                        for trade_id, result, r_multiple, exit_price, close_reason in exits:
                            now_ms = int(time.time() * 1000)
                            closed_trade = _close_trade_in_db(
                                config.PAPER_TRADES_DB,
                                trade_id,
                                result,
                                r_multiple,
                                exit_price,
                                now_ms,
                                close_reason,
                            )
                            if closed_trade:
                                _append_closed_trade(closed_trade)
                                logger.info(
                                    "Closed trade %s: %s exit=%s R=%.4f mae=%.4f mfe=%.4f",
                                    trade_id,
                                    result,
                                    exit_price,
                                    r_multiple,
                                    closed_trade.get("mae", 0),
                                    closed_trade.get("mfe", 0),
                                )
                        open_trades = _get_open_trades(config.PAPER_TRADES_DB)

                    state = {
                        "timestamp_ms": int(time.time() * 1000),
                        "current_price": price,
                        "open_count": len(open_trades),
                        "open_trades": open_trades,
                    }
                    tmp = config.LIFECYCLE_FILE.with_suffix(".tmp")
                    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(config.LIFECYCLE_FILE)

                except Exception as e:
                    logger.warning(f"paper_lifecycle error: {e}")
                await asyncio.sleep(1)


async def run_paper_lifecycle() -> None:
    pl = PaperLifecycle()
    await pl.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_paper_lifecycle())
