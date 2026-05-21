"""
Reads last 200 1m candle_dna records, detects and maintains price zones.
Writes state/latest_zones.json on every new candle.
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

EQUAL_TOLERANCE_PCT = 0.0005   # 0.05%
SWING_WINDOW = 3
ACTIVE_ZONE_PCT = 0.001        # 0.1%
BOOK_TICKER_URL = f"{config.BINANCE_REST}/api/v3/ticker/bookTicker?symbol={config.SYMBOL}"


def _read_last_candles(n: int = 200) -> list[dict]:
    if not config.CANDLE_DNA_FILE.exists():
        return []
    candles = []
    try:
        with open(config.CANDLE_DNA_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-n:]:
            line = line.strip()
            if line:
                try:
                    candles.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return candles


def _price_from_score(score: float, mid: float) -> float:
    return mid * (1 + score / 10000)


def _detect_equal_highs(candles: list[dict], mid_price: float) -> list[dict]:
    if not candles:
        return []
    highs = [_price_from_score(c.get("high_score", c.get("close_score", 0)), mid_price) for c in candles]
    zones = []
    n = len(highs)
    checked = [False] * n
    for i in range(n):
        if checked[i]:
            continue
        group = [highs[i]]
        indices = [i]
        for j in range(i + 1, n):
            if not checked[j] and abs(highs[j] - highs[i]) / (highs[i] + 1e-9) <= EQUAL_TOLERANCE_PCT:
                group.append(highs[j])
                indices.append(j)
                checked[j] = True
        if len(group) >= 2:
            zones.append({
                "type": "equal_highs",
                "price": round(sum(group) / len(group), 2),
                "touches": len(group),
            })
        checked[i] = True
    return zones


def _detect_equal_lows(candles: list[dict], mid_price: float) -> list[dict]:
    if not candles:
        return []
    lows = [_price_from_score(c.get("low_score", c.get("open_score", 0)), mid_price) for c in candles]
    zones = []
    n = len(lows)
    checked = [False] * n
    for i in range(n):
        if checked[i]:
            continue
        group = [lows[i]]
        indices = [i]
        for j in range(i + 1, n):
            if not checked[j] and abs(lows[j] - lows[i]) / (lows[i] + 1e-9) <= EQUAL_TOLERANCE_PCT:
                group.append(lows[j])
                indices.append(j)
                checked[j] = True
        if len(group) >= 2:
            zones.append({
                "type": "equal_lows",
                "price": round(sum(group) / len(group), 2),
                "touches": len(group),
            })
        checked[i] = True
    return zones


def _detect_swing_highs(candles: list[dict], mid_price: float) -> list[dict]:
    if len(candles) < SWING_WINDOW * 2 + 1:
        return []
    highs = [_price_from_score(c.get("high_score", c.get("close_score", 0)), mid_price) for c in candles]
    zones = []
    w = SWING_WINDOW
    for i in range(w, len(highs) - w):
        if all(highs[i] > highs[i - j] for j in range(1, w + 1)) and \
           all(highs[i] > highs[i + j] for j in range(1, w + 1)):
            zones.append({
                "type": "swing_high",
                "price": round(highs[i], 2),
                "index": i,
            })
    return zones


def _detect_swing_lows(candles: list[dict], mid_price: float) -> list[dict]:
    if len(candles) < SWING_WINDOW * 2 + 1:
        return []
    lows = [_price_from_score(c.get("low_score", c.get("open_score", 0)), mid_price) for c in candles]
    zones = []
    w = SWING_WINDOW
    for i in range(w, len(lows) - w):
        if all(lows[i] < lows[i - j] for j in range(1, w + 1)) and \
           all(lows[i] < lows[i + j] for j in range(1, w + 1)):
            zones.append({
                "type": "swing_low",
                "price": round(lows[i], 2),
                "index": i,
            })
    return zones


def _mark_active(zones: list[dict], current_price: float) -> list[dict]:
    for z in zones:
        z["active"] = abs(z["price"] - current_price) / (current_price + 1e-9) <= ACTIVE_ZONE_PCT
    return zones


async def _fetch_mid_price(session: aiohttp.ClientSession) -> float | None:
    try:
        async with session.get(BOOK_TICKER_URL, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data = await resp.json()
            return (float(data["bidPrice"]) + float(data["askPrice"])) / 2
    except Exception as e:
        logger.warning(f"Zone engine price fetch error: {e}")
        return None


async def run_zone_engine() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    last_candle_ts: int = 0

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                candles = _read_last_candles(200)
                if not candles:
                    await asyncio.sleep(5)
                    continue

                latest_ts = candles[-1].get("timestamp_ms", 0)
                if latest_ts == last_candle_ts:
                    await asyncio.sleep(5)
                    continue
                last_candle_ts = latest_ts

                mid_price = await _fetch_mid_price(session) or 50000.0

                all_zones = (
                    _detect_equal_highs(candles, mid_price)
                    + _detect_equal_lows(candles, mid_price)
                    + _detect_swing_highs(candles, mid_price)
                    + _detect_swing_lows(candles, mid_price)
                )
                all_zones = _mark_active(all_zones, mid_price)

                output = {
                    "timestamp_ms": int(time.time() * 1000),
                    "current_price": round(mid_price, 2),
                    "zone_count": len(all_zones),
                    "zones": all_zones,
                }

                tmp = config.ZONES_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(output, indent=2), encoding="utf-8")
                tmp.replace(config.ZONES_FILE)
                logger.info(f"Zones updated: {len(all_zones)} zones at price {mid_price:.2f}")

            except Exception as e:
                logger.warning(f"zone_engine error: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_zone_engine())
