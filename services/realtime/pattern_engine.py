"""
Reads micro_event + candle_dna + context to detect pattern categories.
Writes state/latest_pattern.json.
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

PATTERN_NONE = "NONE"
PATTERNS = ["TRAP", "ABSORPTION", "CONTINUATION", "REVERSAL", "STOP_HUNT", "COMPRESSION", "EXPANSION"]


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_recent_candles(n: int = 10) -> list[dict]:
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


def _detect_pattern(
    micro_event: dict,
    candles: list[dict],
    trend: dict | None,
    regime: dict | None,
    zones: dict | None,
) -> dict:
    now_ms = int(time.time() * 1000)
    base = {
        "timestamp_ms": now_ms,
        "pattern": PATTERN_NONE,
        "direction": "NEUTRAL",
        "confidence": 0.0,
        "details": {},
    }

    if not candles or len(candles) < 2:
        return base

    latest = candles[-1]
    prev = candles[-2] if len(candles) >= 2 else candles[-1]

    event_type = micro_event.get("event_type", "NEUTRAL_NOISE")
    event_dir = micro_event.get("direction", "NEUTRAL")
    continuity = micro_event.get("continuity", 0.0)
    momentum = micro_event.get("momentum", 0.0)

    cvd = latest.get("cvd", 0.0)
    prev_cvd = prev.get("cvd", 0.0)
    body_ratio = latest.get("body_ratio", 0.0)
    absorption = latest.get("absorption", 0.0)
    volume = latest.get("volume", 0.0)
    displacement = latest.get("displacement", 0.0)
    trapped = latest.get("trapped", False)
    upper_wick = latest.get("upper_wick", 0.0)
    lower_wick = latest.get("lower_wick", 0.0)
    open_s = latest.get("open_score", 0.0)
    close_s = latest.get("close_score", 0.0)

    regime_type = regime.get("regime", "UNKNOWN") if regime else "UNKNOWN"
    trend_dir = trend.get("trend", "NO_TREND") if trend else "NO_TREND"

    # ATR proxy from regime
    atr = regime.get("atr", 5.0) if regime else 5.0
    vol_avg = regime.get("volume_avg", 0.01) if regime else 0.01

    dominant_wick = max(upper_wick, lower_wick)
    wick_imbalance = dominant_wick >= max(0.35, body_ratio * 2)
    strong_cvd = abs(cvd) >= 0.1
    cvd_direction = "LONG" if cvd > 0 else "SHORT"

    # COMPRESSION
    if (
        regime_type == "COMPRESSION"
        and abs(cvd) < 0.03
        and (volume < vol_avg * 0.9 or body_ratio < 0.2)
    ):
        return {**base, "pattern": "COMPRESSION", "direction": "NEUTRAL",
                "confidence": 0.7, "details": {"regime": regime_type}}

    # EXPANSION
    if regime_type == "EXPANSION" and displacement > 3.0 and continuity > 0.65:
        direction = "LONG" if cvd > 0 else "SHORT"
        return {**base, "pattern": "EXPANSION", "direction": direction,
                "confidence": 0.75, "details": {"regime": regime_type, "cvd": cvd}}

    # ABSORPTION
    if absorption > 0.35 and volume > vol_avg * 1.1 and displacement < 1.8:
        direction = "LONG" if cvd > 0 else "SHORT"
        return {**base, "pattern": "ABSORPTION", "direction": direction,
                "confidence": 0.72, "details": {"absorption": absorption, "volume": volume}}

    # STOP_HUNT: spike + fast return + CVD reversal
    spike = upper_wick > 0.4 or lower_wick > 0.4 or wick_imbalance
    cvd_reversed = (cvd > 0 and prev_cvd < 0) or (cvd < 0 and prev_cvd > 0)
    if spike and body_ratio < 0.3 and (cvd_reversed or wick_imbalance):
        direction = "LONG" if lower_wick > upper_wick else "SHORT"
        confidence = 0.78 if cvd_reversed else 0.67
        return {**base, "pattern": "STOP_HUNT", "direction": direction,
                "confidence": confidence, "details": {"upper_wick": upper_wick, "lower_wick": lower_wick}}

    # TRAP: price crossed level + fast return + delta flipped
    trap_like = trapped or (body_ratio < 0.18 and dominant_wick > 0.55 and strong_cvd)
    if trap_like and (cvd_reversed or strong_cvd) and body_ratio < 0.25:
        direction = "SHORT" if open_s > close_s else "LONG"
        return {**base, "pattern": "TRAP", "direction": direction,
                "confidence": 0.74, "details": {"trapped": trap_like, "cvd_reversed": cvd_reversed}}

    # REVERSAL: CVD divergence + delta weakening
    cvd_divergence = (cvd < 0 and close_s > open_s) or (cvd > 0 and close_s < open_s)
    delta_weakening = abs(cvd) < abs(prev_cvd) * 0.6
    if cvd_divergence and delta_weakening and body_ratio < 0.4:
        direction = "LONG" if cvd < 0 and close_s > open_s else "SHORT"
        return {**base, "pattern": "REVERSAL", "direction": direction,
                "confidence": 0.68, "details": {"cvd_divergence": True, "delta_weakening": True}}

    # CONTINUATION: low volume pullback + weak delta on pullback
    trend_aligned = (trend_dir == "TREND_UP" and close_s > 0) or (trend_dir == "TREND_DOWN" and close_s < 0)
    low_vol_pullback = volume < vol_avg * 0.8 and abs(cvd) < abs(prev_cvd) * 0.7
    if trend_aligned and low_vol_pullback and event_dir != "NEUTRAL":
        direction = "LONG" if trend_dir == "TREND_UP" else "SHORT"
        return {**base, "pattern": "CONTINUATION", "direction": direction,
                "confidence": 0.65, "details": {"trend_aligned": True, "low_vol_pullback": True}}

    # Fallback: always emit the strongest visible candle signal instead of NONE.
    if body_ratio < 0.2:
        direction = cvd_direction if strong_cvd else "NEUTRAL"
        return {**base, "pattern": "COMPRESSION", "direction": direction,
                "confidence": 0.55, "details": {"body_ratio": body_ratio, "fallback": True}}

    if spike:
        direction = "LONG" if lower_wick > upper_wick else "SHORT"
        return {**base, "pattern": "STOP_HUNT", "direction": direction,
                "confidence": 0.52, "details": {"upper_wick": upper_wick, "lower_wick": lower_wick, "fallback": True}}

    if strong_cvd:
        return {**base, "pattern": "CONTINUATION", "direction": cvd_direction,
                "confidence": 0.51, "details": {"cvd": cvd, "fallback": True}}

    return {**base, "pattern": "REVERSAL", "direction": cvd_direction if strong_cvd else event_dir,
            "confidence": 0.5, "details": {"fallback": True}}


async def run_pattern_engine() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            micro_event = _load_json(config.MICRO_EVENT_FILE) or {}
            trend = _load_json(config.TREND_FILE)
            regime = _load_json(config.REGIME_FILE)
            zones = _load_json(config.ZONES_FILE)
            candles = _load_recent_candles(10)

            pattern = _detect_pattern(micro_event, candles, trend, regime, zones)

            tmp = config.PATTERN_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(pattern, indent=2), encoding="utf-8")
            tmp.replace(config.PATTERN_FILE)
        except Exception as e:
            logger.warning(f"pattern_engine error: {e}")
        await asyncio.sleep(2)


def detect_pattern_from_data(micro_event, candles, trend=None, regime=None, zones=None):
    """Public API for testing."""
    return _detect_pattern(micro_event, candles, trend, regime, zones)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_pattern_engine())
