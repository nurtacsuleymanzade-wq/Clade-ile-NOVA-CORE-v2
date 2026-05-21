"""
Fetches last 50 1h candles from Binance REST, detects swing structure,
classifies trend, and writes state/latest_trend.json.
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

KLINES_URL = f"{config.BINANCE_REST}/api/v3/klines"
INTERVAL = "1h"
LIMIT = 50


def _detect_swings(highs: list[float], lows: list[float], window: int = 3) -> tuple[list, list]:
    swing_highs = []
    swing_lows = []
    n = len(highs)
    for i in range(window, n - window):
        if all(highs[i] > highs[i - j] and highs[i] > highs[i + j] for j in range(1, window + 1)):
            swing_highs.append((i, highs[i]))
        if all(lows[i] < lows[i - j] and lows[i] < lows[i + j] for j in range(1, window + 1)):
            swing_lows.append((i, lows[i]))
    return swing_highs, swing_lows


def _classify_trend(swing_highs: list, swing_lows: list) -> str:
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "NO_TREND"

    last_hh = swing_highs[-1][1]
    prev_hh = swing_highs[-2][1]
    last_hl = swing_lows[-1][1]
    prev_hl = swing_lows[-2][1]

    hh = last_hh > prev_hh
    hl = last_hl > prev_hl
    lh = last_hh < prev_hh
    ll = last_hl < prev_hl

    if hh and hl:
        return "TREND_UP"
    if lh and ll:
        return "TREND_DOWN"
    return "NO_TREND"


async def _fetch_candles(session: aiohttp.ClientSession) -> list[dict]:
    params = {"symbol": config.SYMBOL, "interval": INTERVAL, "limit": LIMIT}
    try:
        async with session.get(
            KLINES_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            candles = []
            for k in data:
                candles.append({
                    "open_time": k[0],
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                })
            return candles
    except Exception as e:
        logger.warning(f"Failed to fetch 1h candles: {e}")
        return []


async def run_trend_engine() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                candles = await _fetch_candles(session)
                if candles:
                    highs = [c["high"] for c in candles]
                    lows = [c["low"] for c in candles]
                    swing_highs, swing_lows = _detect_swings(highs, lows, window=3)
                    trend = _classify_trend(swing_highs, swing_lows)

                    output = {
                        "timestamp_ms": int(time.time() * 1000),
                        "trend": trend,
                        "swing_highs_count": len(swing_highs),
                        "swing_lows_count": len(swing_lows),
                        "last_swing_high": swing_highs[-1][1] if swing_highs else None,
                        "last_swing_low": swing_lows[-1][1] if swing_lows else None,
                        "candle_count": len(candles),
                    }

                    tmp = config.TREND_FILE.with_suffix(".tmp")
                    tmp.write_text(json.dumps(output, indent=2), encoding="utf-8")
                    tmp.replace(config.TREND_FILE)
                    logger.info(f"Trend: {trend}")

            except Exception as e:
                logger.warning(f"trend_engine error: {e}")

            await asyncio.sleep(3600)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_trend_engine())
