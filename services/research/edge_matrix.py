"""
Reads all closed trades, groups by combinations, computes winrate/expectancy.
Writes state/latest_edge_matrix.json and appends to data/edge_matrix_history.jsonl.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

logger = logging.getLogger(__name__)


def _load_closed_trades() -> list[dict]:
    if not config.CLOSED_TRADES_FILE.exists():
        return []
    trades = []
    try:
        with open(config.CLOSED_TRADES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError:
        pass
    return trades


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _get_session(opened_at: int) -> str:
    dt = datetime.fromtimestamp(opened_at / 1000, tz=timezone.utc)
    hour = dt.hour
    if config.LONDON_START_UTC <= hour < config.LONDON_END_UTC:
        return "LONDON"
    if config.NEW_YORK_START_UTC <= hour < config.NEW_YORK_END_UTC:
        return "NEW_YORK"
    return "OTHER"


def _stats(group: list[dict]) -> dict:
    n = len(group)
    if n == 0:
        return {"sample_count": 0, "winrate": 0.0, "expectancy": 0.0, "avg_R": 0.0, "avg_duration_minutes": 0.0}

    wins = [t for t in group if t.get("result") in ("TP1_HIT", "TP2_HIT")]
    losses = [t for t in group if t.get("result") not in ("TP1_HIT", "TP2_HIT")]

    winrate = len(wins) / n
    avg_win_R = sum(t.get("R", 0.0) for t in wins) / max(len(wins), 1)
    avg_loss_R = abs(sum(t.get("R", 0.0) for t in losses) / max(len(losses), 1))
    expectancy = (winrate * avg_win_R) - ((1 - winrate) * avg_loss_R)
    avg_R = sum(t.get("R", 0.0) for t in group) / n
    avg_dur = sum(t.get("duration_seconds", 0) for t in group) / n / 60

    return {
        "sample_count": n,
        "winrate": round(winrate, 4),
        "expectancy": round(expectancy, 4),
        "avg_R": round(avg_R, 4),
        "avg_duration_minutes": round(avg_dur, 2),
    }


def _enrich_trade(trade: dict) -> dict:
    ctx = trade.get("context", {})
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except Exception:
            ctx = {}

    tags = ctx.get("tags", [])
    trend = next((t.replace("trend_", "").upper() for t in tags if t.startswith("trend_")), "NO_TREND")
    regime = next((t.replace("regime_", "").upper() for t in tags if t.startswith("regime_")), "UNKNOWN")
    zone_type = next((t for t in tags if t in ("equal_highs", "equal_lows", "swing_high", "swing_low")), "NONE")
    session = _get_session(trade.get("opened_at", 0))

    return {**trade, "_trend": trend, "_regime": regime, "_zone_type": zone_type, "_session": session}


def _compute_edge_matrix(trades: list[dict]) -> dict:
    enriched = [_enrich_trade(t) for t in trades]

    groups: dict[str, list[dict]] = {}

    def add(key: str, trade: dict) -> None:
        groups.setdefault(key, []).append(trade)

    for t in enriched:
        pat = t.get("pattern", "UNKNOWN")
        trend = t["_trend"]
        regime = t["_regime"]
        zone = t["_zone_type"]
        session = t["_session"]

        add(f"pattern:{pat}", t)
        add(f"pattern:{pat}|trend:{trend}", t)
        add(f"pattern:{pat}|regime:{regime}", t)
        add(f"pattern:{pat}|zone:{zone}", t)
        add(f"pattern:{pat}|session:{session}", t)
        add(f"pattern:{pat}|trend:{trend}|regime:{regime}", t)
        add(f"pattern:{pat}|trend:{trend}|regime:{regime}|zone:{zone}", t)

    matrix = {}
    for key, group in groups.items():
        matrix[key] = _stats(group)

    return matrix


def _run_edge_matrix() -> dict:
    trades = _load_closed_trades()
    matrix = _compute_edge_matrix(trades)
    return {
        "timestamp_ms": int(time.time() * 1000),
        "total_trades": len(trades),
        "combinations": len(matrix),
        "matrix": matrix,
    }


async def run_edge_matrix() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            result = _run_edge_matrix()

            tmp = config.EDGE_MATRIX_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
            tmp.replace(config.EDGE_MATRIX_FILE)

            line = json.dumps({"timestamp_ms": result["timestamp_ms"], "total_trades": result["total_trades"]}) + "\n"
            with open(config.EDGE_MATRIX_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(line)

            logger.info(f"Edge matrix: {result['total_trades']} trades, {result['combinations']} combinations")
        except Exception as e:
            logger.warning(f"edge_matrix error: {e}")
        await asyncio.sleep(900)


def compute_edge_matrix_from_trades(trades: list[dict]) -> dict:
    """Public API for testing."""
    return _compute_edge_matrix(trades)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_edge_matrix())
