"""
Builds performance summary every 15 minutes.
Writes reports/latest_report.json and optionally sends Telegram message.
"""
import asyncio
import json
import logging
import time
from pathlib import Path

import aiohttp

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

logger = logging.getLogger(__name__)


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


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


def _build_report(
    closed_trades: list[dict],
    lifecycle: dict | None,
    edge_data: dict | None,
    suppressed_data: dict | None,
) -> dict:
    total_closed = len(closed_trades)
    total_open = lifecycle.get("open_count", 0) if lifecycle else 0

    wins = [t for t in closed_trades if t.get("result") in ("TP1_HIT", "TP2_HIT")]
    winrate = len(wins) / max(total_closed, 1)
    avg_R = sum(t.get("R", 0.0) for t in closed_trades) / max(total_closed, 1)

    win_R = sum(t.get("R", 0.0) for t in wins) / max(len(wins), 1)
    loss_R = abs(sum(t.get("R", 0.0) for t in closed_trades if t.get("result") not in ("TP1_HIT", "TP2_HIT"))
                 / max(total_closed - len(wins), 1))
    expectancy = winrate * win_R - (1 - winrate) * loss_R

    best_pattern = None
    worst_pattern = None
    top_5 = []

    if edge_data:
        matrix = edge_data.get("matrix", {})
        qualified = {
            k: v for k, v in matrix.items()
            if v.get("sample_count", 0) >= 20
        }
        if qualified:
            sorted_by_exp = sorted(qualified.items(), key=lambda x: x[1].get("expectancy", 0), reverse=True)
            best_pattern = sorted_by_exp[0][0] if sorted_by_exp else None
            worst_pattern = sorted_by_exp[-1][0] if sorted_by_exp else None
            top_5 = [
                {"key": k, **v}
                for k, v in sorted_by_exp[:5]
            ]

    return {
        "timestamp_ms": int(time.time() * 1000),
        "total_closed": total_closed,
        "total_open": total_open,
        "winrate": round(winrate, 4),
        "expectancy": round(expectancy, 4),
        "avg_R": round(avg_R, 4),
        "best_pattern": best_pattern,
        "worst_pattern": worst_pattern,
        "top_5_combinations": top_5,
        "sample_building_count": suppressed_data.get("sample_building_count", 0) if suppressed_data else 0,
        "suppressed_count": suppressed_data.get("suppressed_count", 0) if suppressed_data else 0,
    }


def _format_telegram(report: dict) -> str:
    lines = [
        "📊 *NOVA CORE v2 Report*",
        f"Closed: {report['total_closed']} | Open: {report['total_open']}",
        f"Winrate: {report['winrate']*100:.1f}%",
        f"Expectancy: {report['expectancy']:.3f}R",
        f"Avg R: {report['avg_R']:.3f}R",
        f"Best: {report['best_pattern'] or 'N/A'}",
        f"Building: {report['sample_building_count']} | Suppressed: {report['suppressed_count']}",
    ]
    return "\n".join(lines)


async def _send_telegram(session: aiohttp.ClientSession, text: str) -> None:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
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
                closed_trades = _load_closed_trades()
                lifecycle = _load_json(config.LIFECYCLE_FILE)
                edge_data = _load_json(config.EDGE_MATRIX_FILE)
                suppressed_data = _load_json(config.SUPPRESSED_FILE)

                report = _build_report(closed_trades, lifecycle, edge_data, suppressed_data)

                tmp = config.REPORT_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(report, indent=2), encoding="utf-8")
                tmp.replace(config.REPORT_FILE)

                logger.info(
                    f"Report: closed={report['total_closed']} "
                    f"winrate={report['winrate']*100:.1f}% "
                    f"expectancy={report['expectancy']:.3f}R"
                )

                tg_text = _format_telegram(report)
                await _send_telegram(session, tg_text)

            except Exception as e:
                logger.warning(f"reporter error: {e}")
            await asyncio.sleep(900)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_reporter())
