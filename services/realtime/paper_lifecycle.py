"""
Opens/closes paper trades based on decision_gate output.
Monitors price via bookTicker, closes on SL/TP/timeout.
"""
import asyncio
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path

import aiohttp

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

logger = logging.getLogger(__name__)

BOOK_TICKER_URL = f"{config.BINANCE_REST}/api/v3/ticker/bookTicker?symbol={config.SYMBOL}"
TIMEOUT_SECONDS = config.TRADE_TIMEOUT_HOURS * 3600


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS open_trades (
            id TEXT PRIMARY KEY,
            pattern TEXT,
            direction TEXT,
            entry REAL,
            sl REAL,
            tp1 REAL,
            tp2 REAL,
            rr REAL,
            opened_at INTEGER,
            context_json TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS closed_trades (
            id TEXT PRIMARY KEY,
            pattern TEXT,
            direction TEXT,
            entry REAL,
            sl REAL,
            tp1 REAL,
            tp2 REAL,
            rr REAL,
            opened_at INTEGER,
            context_json TEXT,
            closed_at INTEGER,
            result TEXT,
            R REAL,
            duration_seconds INTEGER
        )
    """)
    conn.commit()
    conn.close()


def _get_open_trades(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM open_trades")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def _insert_open_trade(db_path: Path, trade: dict) -> None:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO open_trades (id, pattern, direction, entry, sl, tp1, tp2, rr, opened_at, context_json)
        VALUES (:id, :pattern, :direction, :entry, :sl, :tp1, :tp2, :rr, :opened_at, :context_json)
    """, trade)
    conn.commit()
    conn.close()


def _close_trade_in_db(db_path: Path, trade_id: str, result: str, R: float, closed_at: int) -> None:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT * FROM open_trades WHERE id=?", (trade_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        return
    cols = [desc[0] for desc in cur.description]
    trade = dict(zip(cols, row))
    duration = (closed_at - trade["opened_at"]) // 1000

    cur.execute("""
        INSERT OR REPLACE INTO closed_trades
        (id, pattern, direction, entry, sl, tp1, tp2, rr, opened_at, context_json,
         closed_at, result, R, duration_seconds)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        trade["id"], trade["pattern"], trade["direction"],
        trade["entry"], trade["sl"], trade["tp1"], trade["tp2"], trade["rr"],
        trade["opened_at"], trade["context_json"],
        closed_at, result, R, duration,
    ))
    cur.execute("DELETE FROM open_trades WHERE id=?", (trade_id,))
    conn.commit()
    conn.close()


def _append_closed_trade(trade: dict, result: str, R: float, closed_at: int) -> None:
    duration = (closed_at - trade["opened_at"]) // 1000
    try:
        ctx = json.loads(trade.get("context_json", "{}"))
    except Exception:
        ctx = {}
    record = {
        "id": trade["id"],
        "pattern": trade["pattern"],
        "direction": trade["direction"],
        "entry": trade["entry"],
        "sl": trade["sl"],
        "tp1": trade["tp1"],
        "tp2": trade["tp2"],
        "rr": trade["rr"],
        "opened_at": trade["opened_at"],
        "closed_at": closed_at,
        "result": result,
        "R": R,
        "duration_seconds": duration,
        "context": ctx,
    }
    line = json.dumps(record) + "\n"
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

        trade_id = str(uuid.uuid4())[:8]
        now_ms = int(time.time() * 1000)
        context = {
            "tags": decision.get("tags", []),
            "rr": decision.get("rr"),
        }
        trade = {
            "id": trade_id,
            "pattern": pattern,
            "direction": direction,
            "entry": decision["entry"],
            "sl": decision["sl"],
            "tp1": decision["tp1"],
            "tp2": decision.get("tp2", decision["tp1"]),
            "rr": decision.get("rr", 0.0),
            "opened_at": now_ms,
            "context_json": json.dumps(context),
        }
        _insert_open_trade(config.PAPER_TRADES_DB, trade)
        logger.info(f"Opened paper trade {trade_id}: {pattern} {direction} entry={trade['entry']}")
        return trade

    def _check_exits(self, trades: list[dict], price: float) -> list[tuple[str, str, float]]:
        exits = []
        now_ms = int(time.time() * 1000)
        for t in trades:
            direction = t["direction"]
            entry = t["entry"]
            sl = t["sl"]
            tp1 = t["tp1"]
            tp2 = t.get("tp2", tp1)
            risk = abs(entry - sl) or 1e-9

            if (now_ms - t["opened_at"]) >= TIMEOUT_SECONDS * 1000:
                R = (price - entry) / risk if direction == "LONG" else (entry - price) / risk
                exits.append((t["id"], "TIMEOUT", round(R, 4)))
            elif direction == "LONG":
                if price <= sl:
                    exits.append((t["id"], "SL_HIT", round(-1.0, 4)))
                elif price >= tp2:
                    R = (tp2 - entry) / risk
                    exits.append((t["id"], "TP2_HIT", round(R, 4)))
                elif price >= tp1:
                    R = (tp1 - entry) / risk
                    exits.append((t["id"], "TP1_HIT", round(R, 4)))
            else:  # SHORT
                if price >= sl:
                    exits.append((t["id"], "SL_HIT", round(-1.0, 4)))
                elif price <= tp2:
                    R = (entry - tp2) / risk
                    exits.append((t["id"], "TP2_HIT", round(R, 4)))
                elif price <= tp1:
                    R = (entry - tp1) / risk
                    exits.append((t["id"], "TP1_HIT", round(R, 4)))
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

                    # Open new trade if allowed
                    decision_ts = decision.get("timestamp_ms", 0)
                    if (
                        decision.get("decision") == "ALLOW_PAPER"
                        and decision_ts != self._last_decision_ts
                    ):
                        self._open_trade(decision, open_trades)
                        self._last_decision_ts = decision_ts
                        open_trades = _get_open_trades(config.PAPER_TRADES_DB)

                    # Check exits
                    if price:
                        exits = self._check_exits(open_trades, price)
                        for trade_id, result, R in exits:
                            now_ms = int(time.time() * 1000)
                            trade = next((t for t in open_trades if t["id"] == trade_id), None)
                            if trade:
                                _close_trade_in_db(config.PAPER_TRADES_DB, trade_id, result, R, now_ms)
                                _append_closed_trade(trade, result, R, now_ms)
                                logger.info(f"Closed trade {trade_id}: {result} R={R}")
                        open_trades = _get_open_trades(config.PAPER_TRADES_DB)

                    # Write lifecycle state
                    state = {
                        "timestamp_ms": int(time.time() * 1000),
                        "current_price": price,
                        "open_count": len(open_trades),
                        "open_trades": open_trades,
                    }
                    tmp = config.LIFECYCLE_FILE.with_suffix(".tmp")
                    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
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
