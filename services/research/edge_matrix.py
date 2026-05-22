"""
Reads closed trades, groups by combinations, computes winrate/expectancy,
and writes state/latest_edge_matrix.json.
"""
import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

logger = logging.getLogger(__name__)

MIN_PROMOTION_SAMPLE = 30
ACTIVE_EXPECTANCY_THRESHOLD = 0.1
SUPPRESS_EXPECTANCY_THRESHOLD = -0.3
WIN_RESULTS = {"TP1_HIT", "TP2_HIT"}
LOSS_RESULTS = {"SL_HIT"}
THESIS_RESULTS = {"THESIS_BROKEN"}
TIMEOUT_RESULTS = {"TIMEOUT"}


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_closed_trades() -> list[dict]:
    if not config.PAPER_TRADES_DB.exists():
        return []
    conn = sqlite3.connect(str(config.PAPER_TRADES_DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM closed_trades ORDER BY closed_at_epoch ASC, id ASC")
        rows = [dict(row) for row in cur.fetchall()]
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


def _utc_session_from_epoch(opened_at_epoch: int) -> str:
    dt = datetime.fromtimestamp(opened_at_epoch / 1000, tz=timezone.utc)
    hour = dt.hour
    if config.LONDON_START_UTC <= hour < config.LONDON_END_UTC:
        return "LONDON"
    if config.NEW_YORK_START_UTC <= hour < config.NEW_YORK_END_UTC:
        return "NEW_YORK"
    return "OFF_SESSION"


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


def evaluate_combo_status(sample_count: int, expectancy: float) -> tuple[str, str | None, str | None]:
    if sample_count < MIN_PROMOTION_SAMPLE:
        return "BUILD", None, f"sample<{MIN_PROMOTION_SAMPLE}"
    if expectancy < SUPPRESS_EXPECTANCY_THRESHOLD:
        return "SUPPRESSED", f"expectancy<{SUPPRESS_EXPECTANCY_THRESHOLD}", None
    if expectancy > ACTIVE_EXPECTANCY_THRESHOLD:
        return "ACTIVE", None, f"sample>={MIN_PROMOTION_SAMPLE}, expectancy>{ACTIVE_EXPECTANCY_THRESHOLD}"
    return "BUILD", None, f"sample>={MIN_PROMOTION_SAMPLE}, expectancy_between_thresholds"


def _enrich_trade(trade: dict) -> dict:
    opened_at_epoch = _coerce_int(trade.get("opened_at_epoch"))
    if opened_at_epoch <= 0 and trade.get("opened_at"):
        try:
            opened_at_epoch = int(
                datetime.fromisoformat(str(trade["opened_at"]).replace("Z", "+00:00")).timestamp() * 1000
            )
        except ValueError:
            opened_at_epoch = 0

    context = trade.get("context")
    if context is None and trade.get("context_json"):
        try:
            context = json.loads(trade["context_json"])
        except Exception:
            context = {}
    if not isinstance(context, dict):
        context = {}

    tags = context.get("tags", [])
    trend = trade.get("trend_at_entry") or next(
        (tag.replace("trend_", "").upper() for tag in tags if str(tag).startswith("trend_")),
        "NO_TREND",
    )
    regime = trade.get("regime_at_entry") or next(
        (tag.replace("regime_", "").upper() for tag in tags if str(tag).startswith("regime_")),
        "UNKNOWN",
    )
    session = trade.get("session") or _utc_session_from_epoch(opened_at_epoch or int(time.time() * 1000))
    timeframe = trade.get("timeframe") or "1m"
    r_multiple = _coerce_float(trade.get("r_multiple", trade.get("R")))

    return {
        **trade,
        "pattern": str(trade.get("pattern", "UNKNOWN")),
        "timeframe": str(timeframe),
        "session": str(session),
        "trend_at_entry": str(trend),
        "regime_at_entry": str(regime),
        "opened_at_epoch": opened_at_epoch,
        "r_multiple": r_multiple,
    }


def _build_stats(key: str, group: list[dict], canonical: bool) -> dict:
    edge_trades = [trade for trade in group if trade.get("result") not in TIMEOUT_RESULTS]
    timeout_count = sum(1 for trade in group if trade.get("result") in TIMEOUT_RESULTS)
    tp_count = sum(1 for trade in edge_trades if trade.get("result") in WIN_RESULTS)
    sl_count = sum(1 for trade in edge_trades if trade.get("result") in LOSS_RESULTS)
    thesis_broken_count = sum(1 for trade in edge_trades if trade.get("result") in THESIS_RESULTS)
    sample_count = len(edge_trades)
    loss_like_trades = [trade for trade in edge_trades if trade.get("result") in (LOSS_RESULTS | THESIS_RESULTS)]
    loss_like_count = len(loss_like_trades)

    avg_r = sum(trade.get("r_multiple", 0.0) for trade in edge_trades) / sample_count if sample_count else 0.0
    winrate = tp_count / sample_count if sample_count else 0.0
    avg_win_r = (
        sum(trade.get("r_multiple", 0.0) for trade in edge_trades if trade.get("result") in WIN_RESULTS) / tp_count
        if tp_count else 0.0
    )
    avg_loss_r = (
        abs(sum(trade.get("r_multiple", 0.0) for trade in loss_like_trades) / loss_like_count)
        if loss_like_count else 0.0
    )
    expectancy = (winrate * avg_win_r) - ((loss_like_count / sample_count) * avg_loss_r) if sample_count else 0.0
    status, suppress_reason, promote_reason = evaluate_combo_status(sample_count, expectancy)

    exemplar = group[0] if group else {}
    return {
        "combo_key": key,
        "pattern": exemplar.get("pattern", "UNKNOWN"),
        "timeframe": exemplar.get("timeframe", "1m") if canonical else exemplar.get("timeframe", "*"),
        "session": exemplar.get("session", "OFF_SESSION") if canonical else exemplar.get("session", "*"),
        "trend": exemplar.get("trend_at_entry", "NO_TREND") if canonical else exemplar.get("trend_at_entry", "*"),
        "regime": exemplar.get("regime_at_entry", "UNKNOWN"),
        "sample_count": sample_count,
        "tp_count": tp_count,
        "sl_count": sl_count,
        "thesis_broken_count": thesis_broken_count,
        "timeout_count": timeout_count,
        "winrate": round(winrate, 4),
        "avg_r": round(avg_r, 4),
        "avg_R": round(avg_r, 4),
        "expectancy": round(expectancy, 4),
        "status": status,
        "suppress_reason": suppress_reason,
        "promote_reason": promote_reason,
        "grouping": "canonical" if canonical else "aggregate",
    }


def _compute_edge_matrix(trades: list[dict]) -> tuple[dict, dict]:
    if not trades:
        return {}, {}

    enriched = [_enrich_trade(trade) for trade in trades]
    canonical_groups: dict[str, list[dict]] = {}
    aggregate_groups: dict[str, list[dict]] = {}

    def add_group(target: dict[str, list[dict]], key: str, trade: dict) -> None:
        target.setdefault(key, []).append(trade)

    for trade in enriched:
        canonical_key = (
            f"{trade['pattern']}|{trade['timeframe']}|{trade['session']}|{trade['trend_at_entry']}"
        )
        add_group(canonical_groups, canonical_key, trade)
        add_group(aggregate_groups, f"pattern:{trade['pattern']}", trade)
        add_group(aggregate_groups, f"pattern:{trade['pattern']}|session:{trade['session']}", trade)
        add_group(aggregate_groups, f"pattern:{trade['pattern']}|trend:{trade['trend_at_entry']}", trade)
        add_group(aggregate_groups, f"pattern:{trade['pattern']}|regime:{trade['regime_at_entry']}", trade)

    canonical_matrix = {
        key: _build_stats(key, group, canonical=True)
        for key, group in canonical_groups.items()
    }
    aggregate_matrix = {
        key: _build_stats(key, group, canonical=False)
        for key, group in aggregate_groups.items()
    }
    return canonical_matrix, {**aggregate_matrix, **canonical_matrix}


def _pick_best_and_worst(canonical_matrix: dict[str, dict]) -> tuple[dict | None, dict | None]:
    qualified = [entry for entry in canonical_matrix.values() if entry.get("sample_count", 0) > 0]
    if not qualified:
        return None, None
    best_combo = max(qualified, key=lambda entry: (entry.get("expectancy", 0.0), entry.get("sample_count", 0)))
    worst_combo = min(qualified, key=lambda entry: (entry.get("expectancy", 0.0), entry.get("sample_count", 0)))
    return best_combo, worst_combo


def _run_edge_matrix() -> dict:
    trades = _load_closed_trades()
    canonical_matrix, full_matrix = _compute_edge_matrix(trades)
    best_combo, worst_combo = _pick_best_and_worst(canonical_matrix)
    status_counts = {
        "building": sum(1 for entry in canonical_matrix.values() if entry.get("status") == "BUILD"),
        "active": sum(1 for entry in canonical_matrix.values() if entry.get("status") == "ACTIVE"),
        "suppressed": sum(1 for entry in canonical_matrix.values() if entry.get("status") == "SUPPRESSED"),
    }
    return {
        "timestamp_ms": int(time.time() * 1000),
        "total_trades": len(trades),
        "combinations": len(full_matrix),
        "canonical_combo_count": len(canonical_matrix),
        "matrix": full_matrix,
        "canonical_combos": canonical_matrix,
        "best_combo": best_combo,
        "worst_combo": worst_combo,
        "status_counts": status_counts,
    }


async def run_edge_matrix() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            result = _run_edge_matrix()

            tmp = config.EDGE_MATRIX_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(config.EDGE_MATRIX_FILE)

            history_line = json.dumps(
                {
                    "timestamp_ms": result["timestamp_ms"],
                    "total_trades": result["total_trades"],
                    "canonical_combo_count": result["canonical_combo_count"],
                },
                ensure_ascii=False,
            ) + "\n"
            with open(config.EDGE_MATRIX_HISTORY_FILE, "a", encoding="utf-8") as handle:
                handle.write(history_line)

            logger.info(
                "Edge matrix: %s trades, %s canonical combos",
                result["total_trades"],
                result["canonical_combo_count"],
            )
        except Exception as e:
            logger.warning(f"edge_matrix error: {e}")
        await asyncio.sleep(900)


def compute_edge_matrix_from_trades(trades: list[dict]) -> dict:
    """Public API for testing."""
    _, matrix = _compute_edge_matrix(trades)
    return matrix


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_edge_matrix())
