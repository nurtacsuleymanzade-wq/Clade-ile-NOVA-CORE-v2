"""
Fetches 15m candles, computes ATR/volume/delta metrics, detects market regime.
Writes state/latest_regime.json.
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
INTERVAL = "15m"
LIMIT = 20
ATR_PERIOD = 14


def _compute_atr(candles: list[dict]) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    period = min(ATR_PERIOD, len(trs))
    return sum(trs[-period:]) / period


def _compute_delta_consistency(candles: list[dict]) -> float:
    if not candles:
        return 0.0
    ups = sum(1 for c in candles if c["close"] > c["open"])
    return ups / len(candles)


def _detect_bos_choch(candles: list[dict]) -> bool:
    if len(candles) < 4:
        return False
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    recent_high = max(highs[-4:-1])
    recent_low = min(lows[-4:-1])
    last_close = candles[-1]["close"]
    return last_close > recent_high or last_close < recent_low


def _classify_regime(candles: list[dict], atr: float, vol_avg: float, delta_consistency: float) -> str:
    if not candles:
        return "UNKNOWN"

    recent_candles = candles[-4:]
    recent_atr = _compute_atr(recent_candles) if len(recent_candles) >= 2 else atr
    overall_atr = _compute_atr(candles[:-4]) if len(candles) > 4 else atr

    atr_expanding = recent_atr > overall_atr * 1.1
    atr_contracting = recent_atr < overall_atr * 0.9

    avg_vol = vol_avg
    recent_vol = sum(c["volume"] for c in recent_candles) / max(len(recent_candles), 1)
    vol_above = recent_vol > avg_vol
    vol_below = recent_vol < avg_vol

    if _detect_bos_choch(candles):
        return "REVERSAL_RISK"
    if atr_expanding and vol_above:
        return "EXPANSION"
    if atr_contracting and vol_below:
        return "COMPRESSION"
    if delta_consistency > 0.70:
        return "TREND"
    return "RANGE"


async def _fetch_candles(session: aiohttp.ClientSession) -> list[dict]:
    params = {"symbol": config.SYMBOL, "interval": INTERVAL, "limit": LIMIT}
    try:
        async with session.get(
            KLINES_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            return [{
                "open_time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            } for k in data]
    except Exception as e:
        logger.warning(f"Failed to fetch 15m candles: {e}")
        return []


async def run_regime_engine() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                candles = await _fetch_candles(session)
                if candles:
                    atr = _compute_atr(candles)
                    vol_avg = sum(c["volume"] for c in candles) / len(candles)
                    delta_consistency = _compute_delta_consistency(candles)
                    regime = _classify_regime(candles, atr, vol_avg, delta_consistency)

                    output = {
                        "timestamp_ms": int(time.time() * 1000),
                        "regime": regime,
                        "atr": round(atr, 2),
                        "volume_avg": round(vol_avg, 4),
                        "delta_consistency": round(delta_consistency, 4),
                        "candle_count": len(candles),
                    }

                    tmp = config.REGIME_FILE.with_suffix(".tmp")
                    tmp.write_text(json.dumps(output, indent=2), encoding="utf-8")
                    tmp.replace(config.REGIME_FILE)
                    logger.info(f"Regime: {regime} ATR={atr:.2f}")

            except Exception as e:
                logger.warning(f"regime_engine error: {e}")

            await asyncio.sleep(900)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_regime_engine())
