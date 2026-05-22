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

ENTRY_LOGIC = {
    "STOP_HUNT_RECLAIM_LONG": {
        "entry_reason": "reclaim_after_sweep_low",
        "sl_reason": "below_sweep_low_invalidation",
        "tp_reason": "nearest_equal_high_liquidity",
    },
    "STOP_HUNT_RECLAIM_SHORT": {
        "entry_reason": "reclaim_after_sweep_high",
        "sl_reason": "above_sweep_high_invalidation",
        "tp_reason": "nearest_equal_low_liquidity",
    },
    "CONTINUATION_LONG": {
        "entry_reason": "cvd_back_with_trend",
        "sl_reason": "below_pullback_low",
        "tp_reason": "next_swing_high_target",
    },
    "CONTINUATION_SHORT": {
        "entry_reason": "cvd_back_with_trend",
        "sl_reason": "above_pullback_high",
        "tp_reason": "next_swing_low_target",
    },
    "ABSORPTION_REVERSAL_LONG": {
        "entry_reason": "post_absorption_breakout",
        "sl_reason": "below_absorption_zone",
        "tp_reason": "nearest_resistance_target",
    },
    "ABSORPTION_REVERSAL_SHORT": {
        "entry_reason": "post_absorption_breakdown",
        "sl_reason": "above_absorption_zone",
        "tp_reason": "nearest_support_target",
    },
}

PATTERN_REASON_DEFAULTS = {
    "STOP_HUNT_RECLAIM_LONG": "liquidity_sweep_low_then_reclaim",
    "STOP_HUNT_RECLAIM_SHORT": "liquidity_sweep_high_then_reclaim",
    "CONTINUATION_LONG": "trend_pullback_then_cvd_recovery",
    "CONTINUATION_SHORT": "trend_pullback_then_cvd_rejection",
    "ABSORPTION_REVERSAL_LONG": "sell_absorption_then_breakout",
    "ABSORPTION_REVERSAL_SHORT": "buy_absorption_then_breakdown",
}


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


def _zone_prices(zone_list: list[dict], zone_types: tuple[str, ...]) -> list[float]:
    return sorted(float(zone["price"]) for zone in zone_list if zone.get("type") in zone_types and zone.get("price") is not None)


def _nearest_above(prices: list[float], level: float) -> float | None:
    candidates = [price for price in prices if price > level]
    return min(candidates) if candidates else None


def _nearest_below(prices: list[float], level: float) -> float | None:
    candidates = [price for price in prices if price < level]
    return max(candidates) if candidates else None


def _resolve_logic_key(pattern_name: str, direction: str) -> str:
    directional_pattern = f"{pattern_name}_{direction}"
    if directional_pattern in ENTRY_LOGIC:
        return directional_pattern
    if pattern_name in ENTRY_LOGIC:
        return pattern_name
    mapping = {
        ("STOP_HUNT", "LONG"): "STOP_HUNT_RECLAIM_LONG",
        ("STOP_HUNT", "SHORT"): "STOP_HUNT_RECLAIM_SHORT",
        ("CONTINUATION", "LONG"): "CONTINUATION_LONG",
        ("CONTINUATION", "SHORT"): "CONTINUATION_SHORT",
        ("ABSORPTION", "LONG"): "ABSORPTION_REVERSAL_LONG",
        ("ABSORPTION", "SHORT"): "ABSORPTION_REVERSAL_SHORT",
    }
    return mapping.get((pattern_name, direction), pattern_name)


def _derive_pattern_reason(pattern: dict, logic_key: str) -> str:
    if pattern.get("pattern_reason"):
        return str(pattern["pattern_reason"])
    details = pattern.get("details", {})
    if isinstance(details, dict):
        active_details = [key for key, value in details.items() if value not in (False, None, 0, "", [])]
        if active_details:
            return ", ".join(active_details)
    return PATTERN_REASON_DEFAULTS.get(logic_key, f"{logic_key.lower()}_detected")


def _fallback_tp(entry: float, sl: float, direction: str) -> tuple[float, float]:
    risk = abs(entry - sl)
    if direction == "LONG":
        tp1 = entry + (risk * 1.5)
        tp2 = entry + (risk * 2.0)
    else:
        tp1 = entry - (risk * 1.5)
        tp2 = entry - (risk * 2.0)
    return tp1, tp2


def _build_generic_geometry(
    pattern_name: str,
    direction: str,
    timeframe: str,
    current_price: float,
    zone_list: list[dict],
    pattern_reason: str,
) -> dict | None:
    highs = _zone_prices(zone_list, ("equal_highs", "swing_high"))
    lows = _zone_prices(zone_list, ("equal_lows", "swing_low"))
    buffer = current_price * SL_BUFFER_PCT
    entry = current_price

    if direction == "LONG":
        sl_anchor = _nearest_below(lows, entry) or (min(lows) if lows else None)
        tp1 = _nearest_above(highs, entry)
        if sl_anchor is None:
            return None
        sl = sl_anchor - buffer
        if tp1 is None:
            tp1, tp2 = _fallback_tp(entry, sl, direction)
        else:
            tp2 = _nearest_above(highs, tp1)
        if tp2 is None:
            tp2 = tp1 + max(buffer, abs(tp1 - entry) * 0.5)
    else:
        sl_anchor = _nearest_above(highs, entry) or (max(highs) if highs else None)
        tp1 = _nearest_below(lows, entry)
        if sl_anchor is None:
            return None
        sl = sl_anchor + buffer
        if tp1 is None:
            tp1, tp2 = _fallback_tp(entry, sl, direction)
        else:
            tp2 = _nearest_below(lows, tp1)
        if tp2 is None:
            tp2 = tp1 - max(buffer, abs(entry - tp1) * 0.5)

    risk = abs(entry - sl)
    reward = abs(tp1 - entry)
    if risk <= 0 or reward <= 0:
        return None

    rr = reward / risk
    if rr < config.MIN_RR:
        return None

    return {
        "timestamp_ms": int(time.time() * 1000),
        "pattern": pattern_name,
        "base_pattern": pattern_name,
        "timeframe": timeframe,
        "direction": direction,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "rr": round(rr, 3),
        "current_price": round(current_price, 2),
        "confidence": float(pattern_reason is not None),  # overwritten by caller
        "pattern_reason": pattern_reason,
        "entry_reason": "close_entry_default",
        "sl_reason": "candle_extreme_default",
        "tp_reason": "nearest_opposing_liquidity_default",
        "analysis": {
            "logic_key": "DEFAULT",
            "entry_source": "current_price",
            "sl_anchor": round(sl_anchor, 2),
            "tp1_anchor": round(tp1, 2),
        },
    }


def _compute_geometry(
    pattern: dict,
    zones: dict | None,
    current_price: float | None,
) -> dict | None:
    pattern_name = pattern.get("pattern", "NONE")
    direction = pattern.get("direction", "NEUTRAL")
    timeframe = str(pattern.get("timeframe", "1m"))

    if pattern_name == "NONE" or direction == "NEUTRAL":
        return None
    if current_price is None:
        return None

    zone_list = zones.get("zones", []) if zones else []
    if not isinstance(zone_list, list):
        zone_list = []

    equal_highs = _zone_prices(zone_list, ("equal_highs",))
    equal_lows = _zone_prices(zone_list, ("equal_lows",))
    swing_highs = _zone_prices(zone_list, ("swing_high",))
    swing_lows = _zone_prices(zone_list, ("swing_low",))

    logic_key = _resolve_logic_key(pattern_name, direction)
    pattern_reason = _derive_pattern_reason(pattern, logic_key)
    confidence = float(pattern.get("confidence", 0.0))
    buffer = current_price * SL_BUFFER_PCT

    entry = None
    sl = None
    tp1 = None
    tp2 = None
    entry_reason = "close_entry_default"
    sl_reason = "candle_extreme_default"
    tp_reason = "nearest_opposing_liquidity_default"
    analysis = {"logic_key": logic_key}

    if logic_key == "STOP_HUNT_RECLAIM_LONG":
        sweep_low = _nearest_below(equal_lows + swing_lows, current_price) or (min(equal_lows + swing_lows) if (equal_lows or swing_lows) else None)
        if sweep_low is None or current_price <= sweep_low:
            return None
        entry = sweep_low + (buffer * 2)
        tp1 = _nearest_above(equal_highs, entry) or _nearest_above(swing_highs, entry)
        sl = sweep_low - buffer
        if tp1 is None:
            tp1, tp2 = _fallback_tp(entry, sl, direction)
            tp_reason = "fallback_rr_target"
            analysis.update({"fallback_tp": True})
        else:
            tp2 = _nearest_above(equal_highs + swing_highs, tp1) or (tp1 + abs(tp1 - entry))
        entry_reason = ENTRY_LOGIC[logic_key]["entry_reason"]
        sl_reason = ENTRY_LOGIC[logic_key]["sl_reason"]
        tp_reason = tp_reason if tp_reason == "fallback_rr_target" else ENTRY_LOGIC[logic_key]["tp_reason"]
        analysis.update({"sweep_low": round(sweep_low, 2), "target_liquidity": round(tp1, 2)})
        pattern_name = logic_key
    elif logic_key == "STOP_HUNT_RECLAIM_SHORT":
        sweep_high = _nearest_above(equal_highs + swing_highs, current_price) or (max(equal_highs + swing_highs) if (equal_highs or swing_highs) else None)
        if sweep_high is None or current_price >= sweep_high:
            return None
        entry = sweep_high - (buffer * 2)
        tp1 = _nearest_below(equal_lows, entry) or _nearest_below(swing_lows, entry)
        sl = sweep_high + buffer
        if tp1 is None:
            tp1, tp2 = _fallback_tp(entry, sl, direction)
            tp_reason = "fallback_rr_target"
            analysis.update({"fallback_tp": True})
        else:
            tp2 = _nearest_below(equal_lows + swing_lows, tp1) or (tp1 - abs(entry - tp1))
        entry_reason = ENTRY_LOGIC[logic_key]["entry_reason"]
        sl_reason = ENTRY_LOGIC[logic_key]["sl_reason"]
        tp_reason = tp_reason if tp_reason == "fallback_rr_target" else ENTRY_LOGIC[logic_key]["tp_reason"]
        analysis.update({"sweep_high": round(sweep_high, 2), "target_liquidity": round(tp1, 2)})
        pattern_name = logic_key
    elif logic_key == "CONTINUATION_LONG":
        pullback_low = _nearest_below(swing_lows + equal_lows, current_price)
        tp1 = _nearest_above(swing_highs, current_price) or _nearest_above(equal_highs, current_price)
        if pullback_low is None:
            return None
        entry = current_price
        sl = pullback_low - buffer
        if tp1 is None:
            tp1, tp2 = _fallback_tp(entry, sl, direction)
            tp_reason = "fallback_rr_target"
            analysis.update({"fallback_tp": True})
        else:
            tp2 = _nearest_above(swing_highs + equal_highs, tp1) or (tp1 + abs(tp1 - entry))
        entry_reason = ENTRY_LOGIC[logic_key]["entry_reason"]
        sl_reason = ENTRY_LOGIC[logic_key]["sl_reason"]
        tp_reason = tp_reason if tp_reason == "fallback_rr_target" else ENTRY_LOGIC[logic_key]["tp_reason"]
        analysis.update({"pullback_low": round(pullback_low, 2), "trend_target": round(tp1, 2)})
        pattern_name = logic_key
    elif logic_key == "CONTINUATION_SHORT":
        pullback_high = _nearest_above(swing_highs + equal_highs, current_price)
        tp1 = _nearest_below(swing_lows, current_price) or _nearest_below(equal_lows, current_price)
        if pullback_high is None:
            return None
        entry = current_price
        sl = pullback_high + buffer
        if tp1 is None:
            tp1, tp2 = _fallback_tp(entry, sl, direction)
            tp_reason = "fallback_rr_target"
            analysis.update({"fallback_tp": True})
        else:
            tp2 = _nearest_below(swing_lows + equal_lows, tp1) or (tp1 - abs(entry - tp1))
        entry_reason = ENTRY_LOGIC[logic_key]["entry_reason"]
        sl_reason = ENTRY_LOGIC[logic_key]["sl_reason"]
        tp_reason = tp_reason if tp_reason == "fallback_rr_target" else ENTRY_LOGIC[logic_key]["tp_reason"]
        analysis.update({"pullback_high": round(pullback_high, 2), "trend_target": round(tp1, 2)})
        pattern_name = logic_key
    elif logic_key == "ABSORPTION_REVERSAL_LONG":
        absorption_low = _nearest_below(equal_lows + swing_lows, current_price)
        if absorption_low is None or current_price <= absorption_low:
            return None
        entry = absorption_low + (buffer * 2)
        tp1 = _nearest_above(equal_highs + swing_highs, entry)
        sl = absorption_low - buffer
        if tp1 is None:
            tp1, tp2 = _fallback_tp(entry, sl, direction)
            tp_reason = "fallback_rr_target"
            analysis.update({"fallback_tp": True})
        else:
            tp2 = _nearest_above(equal_highs + swing_highs, tp1) or (tp1 + abs(tp1 - entry))
        entry_reason = ENTRY_LOGIC[logic_key]["entry_reason"]
        sl_reason = ENTRY_LOGIC[logic_key]["sl_reason"]
        tp_reason = tp_reason if tp_reason == "fallback_rr_target" else ENTRY_LOGIC[logic_key]["tp_reason"]
        analysis.update({"absorption_low": round(absorption_low, 2), "resistance_target": round(tp1, 2)})
        pattern_name = logic_key
    elif logic_key == "ABSORPTION_REVERSAL_SHORT":
        absorption_high = _nearest_above(equal_highs + swing_highs, current_price)
        if absorption_high is None or current_price >= absorption_high:
            return None
        entry = absorption_high - (buffer * 2)
        tp1 = _nearest_below(equal_lows + swing_lows, entry)
        sl = absorption_high + buffer
        if tp1 is None:
            tp1, tp2 = _fallback_tp(entry, sl, direction)
            tp_reason = "fallback_rr_target"
            analysis.update({"fallback_tp": True})
        else:
            tp2 = _nearest_below(equal_lows + swing_lows, tp1) or (tp1 - abs(entry - tp1))
        entry_reason = ENTRY_LOGIC[logic_key]["entry_reason"]
        sl_reason = ENTRY_LOGIC[logic_key]["sl_reason"]
        tp_reason = tp_reason if tp_reason == "fallback_rr_target" else ENTRY_LOGIC[logic_key]["tp_reason"]
        analysis.update({"absorption_high": round(absorption_high, 2), "support_target": round(tp1, 2)})
        pattern_name = logic_key
    else:
        geometry = _build_generic_geometry(pattern_name, direction, timeframe, current_price, zone_list, pattern_reason)
        if geometry is None:
            return None
        geometry["confidence"] = round(confidence, 4)
        return geometry

    if entry is None or sl is None or tp1 is None or tp2 is None:
        return None

    risk = abs(entry - sl)
    reward = abs(tp1 - entry)
    if risk <= 0 or reward <= 0:
        return None

    rr = reward / risk
    if rr < config.MIN_RR:
        logger.info(f"Geometry skipped: RR={rr:.2f} < {config.MIN_RR}")
        return None

    return {
        "timestamp_ms": int(time.time() * 1000),
        "pattern": pattern_name,
        "base_pattern": pattern.get("pattern", pattern_name),
        "timeframe": timeframe,
        "direction": direction,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "rr": round(rr, 3),
        "current_price": round(current_price, 2),
        "confidence": round(confidence, 4),
        "pattern_reason": pattern_reason,
        "entry_reason": entry_reason,
        "sl_reason": sl_reason,
        "tp_reason": tp_reason,
        "analysis": analysis,
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
                "base_pattern": "NONE",
                "timeframe": str(pattern.get("timeframe", "1m")),
                "direction": "NEUTRAL",
                "entry": None,
                "sl": None,
                "tp1": None,
                "tp2": None,
                "rr": None,
                "current_price": current_price,
                "confidence": float(pattern.get("confidence", 0.0)),
                "pattern_reason": "",
                "entry_reason": "",
                "sl_reason": "",
                "tp_reason": "",
                "analysis": {"logic_key": "NONE"},
            }

            tmp = config.GEOMETRY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
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
