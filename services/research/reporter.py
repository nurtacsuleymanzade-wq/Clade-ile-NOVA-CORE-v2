"""
Builds a transparent performance summary every 15 minutes.
Writes reports/latest_report.json and optionally sends a Telegram message.
"""
import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

logger = logging.getLogger(__name__)

WIN_RESULTS = {"TP1_HIT", "TP2_HIT"}
LOSS_RESULTS = {"SL_HIT"}
TIMEOUT_RESULTS = {"TIMEOUT"}


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_trade_rows(table_name: str) -> list[dict]:
    if not config.PAPER_TRADES_DB.exists():
        return []
    conn = sqlite3.connect(str(config.PAPER_TRADES_DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT * FROM {table_name}")
        rows = [dict(row) for row in cur.fetchall()]
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


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


def _utc_session(timestamp_ms: int) -> str:
    hour = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).hour
    if config.LONDON_START_UTC <= hour < config.LONDON_END_UTC:
        return "LONDON"
    if config.NEW_YORK_START_UTC <= hour < config.NEW_YORK_END_UTC:
        return "NEW_YORK"
    return "OFF_SESSION"


def _trend_reason(trend: dict | None) -> str:
    if not trend:
        return "trend_state_missing"
    if trend.get("reason"):
        return str(trend["reason"])
    return (
        f"swings high={trend.get('swing_highs_count', 0)}, "
        f"low={trend.get('swing_lows_count', 0)}"
    )


def _regime_reason(regime: dict | None) -> str:
    if not regime:
        return "regime_state_missing"
    if regime.get("reason"):
        return str(regime["reason"])
    return (
        f"atr={regime.get('atr', 0.0)}, "
        f"delta_consistency={regime.get('delta_consistency', 0.0)}"
    )


def _normalize_trade(trade: dict, table_name: str) -> dict:
    opened_at_epoch = _coerce_int(trade.get("opened_at_epoch"))
    if opened_at_epoch <= 0 and trade.get("opened_at"):
        try:
            opened_at_epoch = int(
                datetime.fromisoformat(str(trade["opened_at"]).replace("Z", "+00:00")).timestamp() * 1000
            )
        except ValueError:
            opened_at_epoch = 0

    closed_at_epoch = _coerce_int(trade.get("closed_at_epoch"))
    if closed_at_epoch <= 0 and trade.get("closed_at"):
        try:
            closed_at_epoch = int(
                datetime.fromisoformat(str(trade["closed_at"]).replace("Z", "+00:00")).timestamp() * 1000
            )
        except ValueError:
            closed_at_epoch = 0

    result = str(trade.get("result", ""))
    return {
        **trade,
        "table_name": table_name,
        "pattern": str(trade.get("pattern", "")),
        "timeframe": str(trade.get("timeframe", "1m")),
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
        "session": str(trade.get("session") or _utc_session(opened_at_epoch or int(time.time() * 1000))),
        "trend_at_entry": str(trade.get("trend_at_entry", "NO_TREND")),
        "regime_at_entry": str(trade.get("regime_at_entry", "UNKNOWN")),
        "opened_at_epoch": opened_at_epoch,
        "closed_at_epoch": closed_at_epoch,
        "exit_price": _coerce_float(trade.get("exit_price")),
        "r_multiple": _coerce_float(trade.get("r_multiple", trade.get("R"))),
        "result": result,
        "duration_seconds": _coerce_int(trade.get("duration_seconds")),
    }


def _filter_recent(trades: list[dict], field_name: str, cutoff_ms: int) -> list[dict]:
    return [trade for trade in trades if _coerce_int(trade.get(field_name)) >= cutoff_ms]


def _tp_count(trades: list[dict]) -> int:
    return sum(1 for trade in trades if trade.get("result") in WIN_RESULTS)


def _sl_count(trades: list[dict]) -> int:
    return sum(1 for trade in trades if trade.get("result") in LOSS_RESULTS)


def _build_long_short_breakdown(closed_non_timeout: list[dict]) -> tuple[dict, str]:
    long_trades = [trade for trade in closed_non_timeout if trade.get("direction") == "LONG"]
    short_trades = [trade for trade in closed_non_timeout if trade.get("direction") == "SHORT"]

    long_tp = _tp_count(long_trades)
    long_sl = _sl_count(long_trades)
    short_tp = _tp_count(short_trades)
    short_sl = _sl_count(short_trades)

    long_wr = long_tp / len(long_trades) if long_trades else 0.0
    short_wr = short_tp / len(short_trades) if short_trades else 0.0

    insight = ""
    if long_wr < 0.3 and short_wr > 0.5:
        insight = "⚠️ LONG'lar underperform ediyor. Context çarpanı kontrol et."
    elif short_wr < 0.3 and long_wr > 0.5:
        insight = "⚠️ SHORT'lar underperform ediyor. Context çarpanı kontrol et."

    return {
        "long_tp": long_tp,
        "long_sl": long_sl,
        "long_wr": round(long_wr * 100, 1),
        "short_tp": short_tp,
        "short_sl": short_sl,
        "short_wr": round(short_wr * 100, 1),
    }, insight


def _best_and_worst_combo(edge_data: dict | None, suppressed_data: dict | None) -> tuple[dict | None, dict | None]:
    if not edge_data:
        return None, None

    canonical_combos = edge_data.get("canonical_combos", {})
    evaluated = (suppressed_data or {}).get("combinations", {})
    best = edge_data.get("best_combo")
    worst = edge_data.get("worst_combo")

    if best and best.get("combo_key") in evaluated:
        best = {**best, **evaluated[best["combo_key"]]}
    if worst and worst.get("combo_key") in evaluated:
        worst = {**worst, **evaluated[worst["combo_key"]]}

    if best and worst:
        return best, worst

    combos = list(canonical_combos.values())
    if not combos:
        return None, None
    combos.sort(key=lambda item: item.get("expectancy", 0.0), reverse=True)
    return combos[0], combos[-1]


def _format_combo_label(combo: dict | None) -> str:
    if not combo:
        return "N/A"
    return f"{combo.get('pattern', 'N/A')} + {combo.get('session', 'N/A')} + {combo.get('trend', 'N/A')}"


def _build_report(
    open_trades: list[dict],
    closed_trades: list[dict],
    lifecycle: dict | None,
    edge_data: dict | None,
    suppressed_data: dict | None,
    trend: dict | None,
    regime: dict | None,
    decision: dict | None,
    zones: dict | None,
) -> dict:
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - (15 * 60 * 1000)

    recent_opened = _filter_recent(open_trades, "opened_at_epoch", cutoff_ms)
    recent_closed = _filter_recent(closed_trades, "closed_at_epoch", cutoff_ms)

    total_closed = len(closed_trades)
    total_open = len(open_trades)
    timeout_count = sum(1 for trade in closed_trades if trade.get("result") in TIMEOUT_RESULTS)
    closed_non_timeout = [trade for trade in closed_trades if trade.get("result") not in TIMEOUT_RESULTS]
    tp_count = _tp_count(closed_non_timeout)
    sl_count = _sl_count(closed_non_timeout)
    effective_closed = len(closed_non_timeout)
    winrate = tp_count / effective_closed if effective_closed else 0.0
    expectancy = (
        sum(trade.get("r_multiple", 0.0) for trade in closed_non_timeout) / effective_closed
        if effective_closed else 0.0
    )
    breakdown, insight = _build_long_short_breakdown(closed_non_timeout)
    best_combo, worst_combo = _best_and_worst_combo(edge_data, suppressed_data)

    btc_price = (
        _coerce_float((lifecycle or {}).get("current_price"))
        or _coerce_float((zones or {}).get("current_price"))
        or _coerce_float((decision or {}).get("current_price"))
    )

    sample_building = (suppressed_data or {}).get("sample_building", [])
    active = (suppressed_data or {}).get("active", [])
    suppressed = (suppressed_data or {}).get("suppressed", [])

    return {
        "timestamp_ms": now_ms,
        "generated_at_utc": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "context": {
            "trend": (trend or {}).get("trend", "NO_TREND"),
            "trend_reason": _trend_reason(trend),
            "regime": (regime or {}).get("regime", "UNKNOWN"),
            "regime_reason": _regime_reason(regime),
            "session": _utc_session(now_ms),
            "btc_price": round(btc_price, 2) if btc_price else 0.0,
        },
        "recent_opened": recent_opened,
        "recent_closed": recent_closed,
        "general": {
            "closed": total_closed,
            "open": total_open,
            "tp_count": tp_count,
            "sl_count": sl_count,
            "timeout_count": timeout_count,
            "winrate_pct": round(winrate * 100, 1),
            "expectancy_r": round(expectancy, 3),
            **breakdown,
            "insight": insight,
        },
        "best_combo": best_combo,
        "worst_combo": worst_combo,
        "sample_status": {
            "building_count": len(sample_building),
            "active_count": len(active),
            "suppressed_count": len(suppressed),
            "suppressed": suppressed,
        },
    }


def _emoji_for_result(result: str) -> str:
    if result in WIN_RESULTS:
        return "✅"
    if result in LOSS_RESULTS:
        return "❌"
    if result in TIMEOUT_RESULTS:
        return "⏱️"
    return "•"


def _format_trade_number(value: float) -> str:
    return f"{_coerce_float(value):.2f}"


def _format_telegram(report: dict) -> str:
    lines = [
        f"NOVA CORE — {report['generated_at_utc']}",
        "",
        "BAĞLAM",
        f"Trend: {report['context']['trend']} ({report['context']['trend_reason']})",
        f"Regime: {report['context']['regime']} ({report['context']['regime_reason']})",
        f"Session: {report['context']['session']}",
        f"BTC: {_format_trade_number(report['context']['btc_price'])}",
        "",
        "⚡ SON 15 DAKİKA",
        f"Açılan: {len(report['recent_opened'])} trade",
    ]

    for trade in report["recent_opened"]:
        lines.extend([
            f"  → {trade.get('pattern', '')} | {trade.get('timeframe', '1m')} | conf: {trade.get('context_adjusted_confidence', 0.0):.2f}",
            f"    Sebep: {trade.get('pattern_reason', '')}",
            f"    Entry: {_format_trade_number(trade.get('entry'))} | SL: {_format_trade_number(trade.get('sl'))} | TP1: {_format_trade_number(trade.get('tp1'))}",
            f"    RR: {trade.get('rr', 0.0):.2f}",
        ])

    lines.append(f"Kapanan: {len(report['recent_closed'])} trade")
    for trade in report["recent_closed"]:
        lines.extend([
            f"  {_emoji_for_result(trade.get('result', ''))} {trade.get('pattern', '')} | {trade.get('timeframe', '1m')} | {trade.get('result', '')}",
            f"    Açılış: {trade.get('pattern_reason', '')}",
            (
                "    Zincir: "
                f"{trade.get('observer_score', 0.0):.2f} → "
                f"{trade.get('micro_event', '')} → "
                f"{trade.get('pattern', '')} → "
                f"{trade.get('entry_reason', '')} → "
                f"{trade.get('result', '')}"
            ),
            f"    Entry: {_format_trade_number(trade.get('entry'))} → Çıkış: {_format_trade_number(trade.get('exit_price'))}",
            f"    Süre: {max(0, trade.get('duration_seconds', 0) // 60)}dk | R: {trade.get('r_multiple', 0.0):.2f}",
        ])

    general = report["general"]
    lines.extend([
        "",
        "GENEL (timeout hariç)",
        f"Closed: {general['closed']} | Open: {general['open']}",
        f"TP: {general['tp_count']} | SL: {general['sl_count']} | Timeout: {general['timeout_count']} (edge dışı)",
        f"Winrate: {general['winrate_pct']:.1f}% ({general['tp_count']} TP / {general['sl_count']} SL)",
        f"Expectancy: {general['expectancy_r']:.3f}R",
        "",
        f"LONG: {general['long_tp']} TP / {general['long_sl']} SL → %{general['long_wr']:.1f}",
        f"SHORT: {general['short_tp']} TP / {general['short_sl']} SL → %{general['short_wr']:.1f}",
    ])
    if general["insight"]:
        lines.append(general["insight"])

    best_combo = report["best_combo"]
    worst_combo = report["worst_combo"]
    lines.extend([
        "",
        "EN İYİ KOMBİNASYON",
        (
            f"{_format_combo_label(best_combo)}: "
            f"{(best_combo or {}).get('sample_count', 0)} sample, "
            f"%{(best_combo or {}).get('winrate', 0.0) * 100:.1f} WR, "
            f"{(best_combo or {}).get('expectancy', 0.0):.2f}R exp"
        ),
        "",
        "⚠️ EN KÖTÜ KOMBİNASYON",
        (
            f"{_format_combo_label(worst_combo)}: "
            f"{(worst_combo or {}).get('sample_count', 0)} sample, "
            f"%{(worst_combo or {}).get('winrate', 0.0) * 100:.1f} WR → "
            f"{(worst_combo or {}).get('status', 'BUILD')}"
        ),
        "",
        "SAMPLE DURUMU",
        f"Building: {report['sample_status']['building_count']} kombinasyon",
        f"Active: {report['sample_status']['active_count']} kombinasyon",
    ])

    suppressed = report["sample_status"]["suppressed"]
    suppressed_lines = [
        f"{item.get('pattern')} + {item.get('session')} + {item.get('regime')} "
        f"({item.get('sample_count', 0)}s, {item.get('expectancy', 0.0):.2f}R)"
        for item in suppressed
    ]
    lines.append(
        f"Suppressed: {report['sample_status']['suppressed_count']} kombinasyon "
        f"({'; '.join(suppressed_lines) if suppressed_lines else 'yok'})"
    )

    if suppressed_lines:
        lines.extend([
            "",
            "⚠️ SUPPRESSED KOMBİNASYONLAR:",
            *[f"- {line}" for line in suppressed_lines],
        ])

    return "\n".join(lines)


async def _send_telegram(session: aiohttp.ClientSession, text: str) -> None:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
    }
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(f"Telegram send failed: {resp.status} {body}")
    except Exception as e:
        logger.warning(f"Telegram error: {e}")


async def run_reporter() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                open_trades = [_normalize_trade(row, "open_trades") for row in _load_trade_rows("open_trades")]
                closed_trades = [_normalize_trade(row, "closed_trades") for row in _load_trade_rows("closed_trades")]
                lifecycle = _load_json(config.LIFECYCLE_FILE)
                edge_data = _load_json(config.EDGE_MATRIX_FILE)
                suppressed_data = _load_json(config.SUPPRESSED_FILE)
                trend = _load_json(config.TREND_FILE)
                regime = _load_json(config.REGIME_FILE)
                decision = _load_json(config.DECISION_FILE)
                zones = _load_json(config.ZONES_FILE)

                report = _build_report(
                    open_trades,
                    closed_trades,
                    lifecycle,
                    edge_data,
                    suppressed_data,
                    trend,
                    regime,
                    decision,
                    zones,
                )
                report["telegram_text"] = _format_telegram(report)

                tmp = config.REPORT_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp.replace(config.REPORT_FILE)

                logger.info(
                    "Report: closed=%s open=%s winrate=%.1f%% expectancy=%.3fR",
                    report["general"]["closed"],
                    report["general"]["open"],
                    report["general"]["winrate_pct"],
                    report["general"]["expectancy_r"],
                )

                await _send_telegram(session, report["telegram_text"])

            except Exception as e:
                logger.warning(f"reporter error: {e}")
            await asyncio.sleep(900)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_reporter())
