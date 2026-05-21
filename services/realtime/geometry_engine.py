"""
Reads pattern + context zones to compute entry/SL/TP geometry.
Writes state/latest_geometry.json. Skips if RR < MIN_RR.
"""
import asyncio
import json
import logging
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

logger = logging.getLogger(__name__)

SL_BUFFER_PCT = 0.0003  # 0.03%


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _get_current_price(zones: dict | None) -> float | None:
    if zones and "current_price" in zones:
        return float(zones["current_price"])
    return None


def _compute_geometry(
    pattern: dict,
    zones: dict | None,
    current_price: float | None,
) -> dict | None:
    pat = pattern.get("pattern", "NONE")
    direction = pattern.get("direction", "NEUTRAL")

    if pat == "NONE" or direction == "NEUTRAL":
        return None
    if current_price is None:
        return None

    zone_list = zones.get("zones", []) if zones else []

    equal_highs = [z for z in zone_list if z.get("type") == "equal_highs"]
    equal_lows = [z for z in zone_list if z.get("type") == "equal_lows"]
    swing_highs = [z for z in zone_list if z.get("type") == "swing_high"]
    swing_lows = [z for z in zone_list if z.get("type") == "swing_low"]

    if direction == "LONG":
        sweep_low = min(
            (z["price"] for z in zone_list if z.get("type") in ("equal_lows", "swing_low")),
            default=current_price * 0.998,
        )
        entry = sweep_low * (1 + SL_BUFFER_PCT * 2)
        sl = sweep_low * (1 - SL_BUFFER_PCT)

        candidates_tp1 = sorted(
            [z["price"] for z in (equal_highs + swing_highs) if z["price"] > entry],
        )
        tp1 = candidates_tp1[0] if candidates_tp1 else current_price * 1.005

        candidates_tp2 = [p for p in candidates_tp1 if p > tp1]
        tp2 = candidates_tp2[0] if candidates_tp2 else tp1 * 1.005

    else:  # SHORT
        sweep_high = max(
            (z["price"] for z in zone_list if z.get("type") in ("equal_highs", "swing_high")),
            default=current_price * 1.002,
        )
        entry = sweep_high * (1 - SL_BUFFER_PCT * 2)
        sl = sweep_high * (1 + SL_BUFFER_PCT)

        candidates_tp1 = sorted(
            [z["price"] for z in (equal_lows + swing_lows) if z["price"] < entry],
            reverse=True,
        )
        tp1 = candidates_tp1[0] if candidates_tp1 else current_price * 0.995

        candidates_tp2 = [p for p in candidates_tp1 if p < tp1]
        tp2 = candidates_tp2[0] if candidates_tp2 else tp1 * 0.995

    risk = abs(entry - sl)
    reward = abs(tp1 - entry)

    if risk <= 0:
        return None

    rr = reward / risk
    if rr < config.MIN_RR:
        logger.info(f"Geometry skipped: RR={rr:.2f} < {config.MIN_RR}")
        return None

    return {
        "timestamp_ms": int(time.time() * 1000),
        "pattern": pat,
        "direction": direction,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "rr": round(rr, 3),
        "current_price": round(current_price, 2),
    }


async def run_geometry_engine() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            pattern = _load_json(config.PATTERN_FILE) or {}
            zones = _load_json(config.ZONES_FILE)
            current_price = _get_current_price(zones)

            geometry = _compute_geometry(pattern, zones, current_price)

            now_ms = int(time.time() * 1000)
            output = geometry if geometry else {
                "timestamp_ms": now_ms,
                "pattern": "NONE",
                "direction": "NEUTRAL",
                "entry": None,
                "sl": None,
                "tp1": None,
                "tp2": None,
                "rr": None,
                "current_price": current_price,
            }

            tmp = config.GEOMETRY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(output, indent=2), encoding="utf-8")
            tmp.replace(config.GEOMETRY_FILE)
        except Exception as e:
            logger.warning(f"geometry_engine error: {e}")
        await asyncio.sleep(2)


def compute_geometry_from_data(pattern, zones, current_price):
    """Public API for testing."""
    return _compute_geometry(pattern, zones, current_price)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_geometry_engine())
