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
STRUCTURAL_BUFFER_PCT = 0.0005
MIN_SWING_DISTANCE_PCT = 0.001
MAX_SWING_DISTANCE_PCT = 0.008
MIN_RISK_PCT = 0.002
FALLBACK_SL_PCT = 0.003
MIN_ACCEPTABLE_RR = 1.0
NO_STRUCTURAL_GEOMETRY = "NO_STRUCTURAL_GEOMETRY"

_last_geometry_skip_reason = ""

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


def _combine_zone_prices(zones: dict | None, zone_list: list[dict], array_key: str, zone_types: tuple[str, ...]) -> list[float]:
    prices: list[float] = []
    if zones:
        values = zones.get(array_key, [])
        if isinstance(values, list):
            for value in values:
                if isinstance(value, dict):
                    candidate = value.get("price")
                else:
                    candidate = value
                if candidate not in (None, ""):
                    prices.append(float(candidate))
    prices.extend(_zone_prices(zone_list, zone_types))
    return sorted(set(prices))


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


def _fallback_sl(entry: float, direction: str) -> float:
    distance = entry * FALLBACK_SL_PCT
    if direction == "LONG":
        return entry - distance
    return entry + distance


def _normalize_sl(entry: float, sl: float | None, direction: str) -> tuple[float, float]:
    minimum_risk = entry * MIN_RISK_PCT
    if sl is None:
        sl = _fallback_sl(entry, direction)
    if direction == "LONG":
        raw_risk = entry - sl
        risk = max(raw_risk, minimum_risk)
        return entry - risk, risk
    raw_risk = sl - entry
    risk = max(raw_risk, minimum_risk)
    return entry + risk, risk


def _candle_extreme(candle_dna: dict | None, key: str) -> float | None:
    if not candle_dna:
        return None
    value = candle_dna.get(key)
    if value in (None, ""):
        return None
    return float(value)


def _candle_buffer_sl(entry: float, direction: str, candle_dna: dict | None) -> tuple[float | None, str]:
    candle_low = _candle_extreme(candle_dna, "low")
    candle_high = _candle_extreme(candle_dna, "high")
    candle_range = None
    if candle_low is not None and candle_high is not None:
        candle_range = candle_high - candle_low
    buffer = (candle_range * 0.1) if candle_range and candle_range > 0 else (entry * MIN_RISK_PCT)

    if direction == "LONG":
        if candle_low is None:
            return None, "candle_low_buffer"
        return candle_low - buffer, "candle_low_buffer"

    if candle_high is None:
        return None, "candle_high_buffer"
    return candle_high + buffer, "candle_high_buffer"


def _resolve_structural_sl(
    entry: float,
    direction: str,
    swing_highs: list[float],
    swing_lows: list[float],
    candle_dna: dict | None,
) -> tuple[float, float, str, dict]:
    analysis: dict = {}
    structural_buffer = entry * STRUCTURAL_BUFFER_PCT
    min_distance = entry * MIN_SWING_DISTANCE_PCT
    max_distance = entry * MAX_SWING_DISTANCE_PCT

    if direction == "LONG":
        swing_low = _nearest_below(swing_lows, entry)
        if swing_low is not None:
            distance = entry - swing_low
            analysis["swing_low_candidate"] = round(swing_low, 2)
            analysis["swing_low_distance"] = round(distance, 2)
            if min_distance < distance < max_distance:
                sl = swing_low - structural_buffer
                return sl, abs(entry - sl), "swing_low_structural", analysis
        sl, sl_reason = _candle_buffer_sl(entry, direction, candle_dna)
        if sl is not None and sl < entry:
            return sl, abs(entry - sl), sl_reason, analysis
        sl = _fallback_sl(entry, direction)
        return sl, abs(entry - sl), "fallback_percentage", analysis

    swing_high = _nearest_above(swing_highs, entry)
    if swing_high is not None:
        distance = swing_high - entry
        analysis["swing_high_candidate"] = round(swing_high, 2)
        analysis["swing_high_distance"] = round(distance, 2)
        if min_distance < distance < max_distance:
            sl = swing_high + structural_buffer
            return sl, abs(entry - sl), "swing_high_structural", analysis
    sl, sl_reason = _candle_buffer_sl(entry, direction, candle_dna)
    if sl is not None and sl > entry:
        return sl, abs(entry - sl), sl_reason, analysis
    sl = _fallback_sl(entry, direction)
    return sl, abs(entry - sl), "fallback_percentage", analysis


def _tp_candidates(direction: str, entry: float, equal_highs: list[float], equal_lows: list[float], swing_highs: list[float], swing_lows: list[float]) -> list[tuple[float, str]]:
    candidates: list[tuple[float, str]] = []
    if direction == "LONG":
        seen: set[tuple[float, str]] = set()
        for price in sorted(p for p in equal_highs if p > entry):
            item = (price, "equal_high_structural")
            if item not in seen:
                candidates.append(item)
                seen.add(item)
        for price in sorted(p for p in swing_highs if p > entry):
            item = (price, "swing_high_structural")
            if item not in seen:
                candidates.append(item)
                seen.add(item)
        return candidates

    seen = set()
    for price in sorted((p for p in equal_lows if p < entry), reverse=True):
        item = (price, "equal_low_structural")
        if item not in seen:
            candidates.append(item)
            seen.add(item)
    for price in sorted((p for p in swing_lows if p < entry), reverse=True):
        item = (price, "swing_low_structural")
        if item not in seen:
            candidates.append(item)
            seen.add(item)
    return candidates


def _fallback_tp_with_reason(entry: float, risk: float, direction: str) -> tuple[float, str]:
    risk = max(risk, entry * MIN_RISK_PCT)
    if direction == "LONG":
        return entry + (risk * 1.5), "fallback_rr_target"
    return entry - (risk * 1.5), "fallback_rr_target"


def _resolve_tp(
    entry: float,
    sl: float,
    direction: str,
    equal_highs: list[float],
    equal_lows: list[float],
    swing_highs: list[float],
    swing_lows: list[float],
) -> tuple[float, str, float, dict] | None:
    risk = max(abs(entry - sl), entry * MIN_RISK_PCT)
    candidates = _tp_candidates(direction, entry, equal_highs, equal_lows, swing_highs, swing_lows)
    analysis = {
        "tp_candidates": [round(price, 2) for price, _ in candidates],
        "minimum_risk_applied": round(entry * MIN_RISK_PCT, 2),
    }

    selected_tp = None
    selected_reason = ""
    rr = 0.0
    for index, (candidate_tp, candidate_reason) in enumerate(candidates):
        candidate_rr = abs(candidate_tp - entry) / risk if risk > 0 else 0.0
        if candidate_rr >= MIN_ACCEPTABLE_RR:
            selected_tp = candidate_tp
            selected_reason = candidate_reason
            rr = candidate_rr
            analysis["tp_candidate_index"] = index
            break

    if selected_tp is None:
        fallback_tp, fallback_reason = _fallback_tp_with_reason(entry, risk, direction)
        fallback_rr = abs(fallback_tp - entry) / risk if risk > 0 else 0.0
        analysis["fallback_tp"] = round(fallback_tp, 2)
        if fallback_rr < MIN_ACCEPTABLE_RR:
            return None
        selected_tp = fallback_tp
        selected_reason = fallback_reason
        rr = fallback_rr

    return selected_tp, selected_reason, rr, analysis


def _build_generic_geometry(
    pattern_name: str,
    direction: str,
    timeframe: str,
    current_price: float,
    zone_list: list[dict],
    pattern_reason: str,
    candle_dna: dict | None = None,
) -> dict | None:
    highs = _combine_zone_prices(None, zone_list, "equal_highs", ("equal_highs", "swing_high"))
    lows = _combine_zone_prices(None, zone_list, "equal_lows", ("equal_lows", "swing_low"))
    buffer = current_price * SL_BUFFER_PCT
    entry = current_price

    if direction == "LONG":
        sl_anchor = _nearest_below(lows, entry) or (min(lows) if lows else None)
        tp1 = _nearest_above(highs, entry)
        sl = (sl_anchor - buffer) if sl_anchor is not None else _candle_extreme(candle_dna, "low")
        sl, risk = _normalize_sl(entry, sl, direction)
        if tp1 is None:
            tp1, tp2 = _fallback_tp(entry, sl, direction)
        else:
            tp2 = _nearest_above(highs, tp1)
        if tp2 is None:
            tp2 = tp1 + max(buffer, abs(tp1 - entry) * 0.5)
    else:
        sl_anchor = _nearest_above(highs, entry) or (max(highs) if highs else None)
        tp1 = _nearest_below(lows, entry)
        sl = (sl_anchor + buffer) if sl_anchor is not None else _candle_extreme(candle_dna, "high")
        sl, risk = _normalize_sl(entry, sl, direction)
        if tp1 is None:
            tp1, tp2 = _fallback_tp(entry, sl, direction)
        else:
            tp2 = _nearest_below(lows, tp1)
        if tp2 is None:
            tp2 = tp1 - max(buffer, abs(entry - tp1) * 0.5)

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
            "sl_anchor": round(sl_anchor, 2) if sl_anchor is not None else None,
            "tp1_anchor": round(tp1, 2),
            "minimum_risk_applied": round(entry * 0.002, 2),
        },
    }


def _compute_geometry(
    pattern: dict,
    zones: dict | None,
    current_price: float | None,
    candle_dna: dict | None = None,
) -> dict | None:
    global _last_geometry_skip_reason
    _last_geometry_skip_reason = ""
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

    equal_highs = _combine_zone_prices(zones, zone_list, "equal_highs", ("equal_highs",))
    equal_lows = _combine_zone_prices(zones, zone_list, "equal_lows", ("equal_lows",))
    swing_highs = _combine_zone_prices(zones, zone_list, "swing_highs", ("swing_high",))
    swing_lows = _combine_zone_prices(zones, zone_list, "swing_lows", ("swing_low",))

    logic_key = _resolve_logic_key(pattern_name, direction)
    pattern_reason = _derive_pattern_reason(pattern, logic_key)
    confidence = float(pattern.get("confidence", 0.0))
    entry = None
    sl = None
    tp1 = None
    tp2 = None
    entry_reason = "close_entry_default"
    sl_reason = "fallback_percentage"
    tp_reason = "fallback_rr_target"
    analysis = {"logic_key": logic_key}

    if logic_key == "STOP_HUNT_RECLAIM_LONG":
        sweep_low = _nearest_below(equal_lows + swing_lows, current_price) or (min(equal_lows + swing_lows) if (equal_lows or swing_lows) else None)
        buffer = current_price * SL_BUFFER_PCT
        entry = sweep_low + (buffer * 2) if sweep_low is not None and current_price > sweep_low else current_price
        entry_reason = ENTRY_LOGIC[logic_key]["entry_reason"]
        analysis.update({"sweep_low": round(sweep_low, 2) if sweep_low is not None else None})
        pattern_name = logic_key
    elif logic_key == "STOP_HUNT_RECLAIM_SHORT":
        sweep_high = _nearest_above(equal_highs + swing_highs, current_price) or (max(equal_highs + swing_highs) if (equal_highs or swing_highs) else None)
        buffer = current_price * SL_BUFFER_PCT
        entry = sweep_high - (buffer * 2) if sweep_high is not None and current_price < sweep_high else current_price
        entry_reason = ENTRY_LOGIC[logic_key]["entry_reason"]
        analysis.update({"sweep_high": round(sweep_high, 2) if sweep_high is not None else None})
        pattern_name = logic_key
    elif logic_key == "CONTINUATION_LONG":
        pullback_low = _nearest_below(swing_lows + equal_lows, current_price)
        entry = current_price
        entry_reason = ENTRY_LOGIC[logic_key]["entry_reason"]
        analysis.update({"pullback_low": round(pullback_low, 2) if pullback_low is not None else None})
        pattern_name = logic_key
    elif logic_key == "CONTINUATION_SHORT":
        pullback_high = _nearest_above(swing_highs + equal_highs, current_price)
        entry = current_price
        entry_reason = ENTRY_LOGIC[logic_key]["entry_reason"]
        analysis.update({"pullback_high": round(pullback_high, 2) if pullback_high is not None else None})
        pattern_name = logic_key
    elif logic_key == "ABSORPTION_REVERSAL_LONG":
        absorption_low = _nearest_below(equal_lows + swing_lows, current_price)
        buffer = current_price * SL_BUFFER_PCT
        entry = absorption_low + (buffer * 2) if absorption_low is not None and current_price > absorption_low else current_price
        entry_reason = ENTRY_LOGIC[logic_key]["entry_reason"]
        analysis.update({"absorption_low": round(absorption_low, 2) if absorption_low is not None else None})
        pattern_name = logic_key
    elif logic_key == "ABSORPTION_REVERSAL_SHORT":
        absorption_high = _nearest_above(equal_highs + swing_highs, current_price)
        buffer = current_price * SL_BUFFER_PCT
        entry = absorption_high - (buffer * 2) if absorption_high is not None and current_price < absorption_high else current_price
        entry_reason = ENTRY_LOGIC[logic_key]["entry_reason"]
        analysis.update({"absorption_high": round(absorption_high, 2) if absorption_high is not None else None})
        pattern_name = logic_key
    elif pattern_name in {"TRAP", "REVERSAL"}:
        entry = current_price
        analysis.update({
            "candle_low": round(_candle_extreme(candle_dna, "low"), 2) if _candle_extreme(candle_dna, "low") is not None else None,
            "candle_high": round(_candle_extreme(candle_dna, "high"), 2) if _candle_extreme(candle_dna, "high") is not None else None,
        })
    else:
        geometry = _build_generic_geometry(pattern_name, direction, timeframe, current_price, zone_list, pattern_reason, candle_dna)
        if geometry is None:
            return None
        geometry["confidence"] = round(confidence, 4)
        return geometry

    if entry is None:
        return None

    sl, risk, sl_reason, sl_analysis = _resolve_structural_sl(entry, direction, swing_highs, swing_lows, candle_dna)
    analysis.update(sl_analysis)
    if sl_reason == "fallback_percentage":
        _last_geometry_skip_reason = NO_STRUCTURAL_GEOMETRY
        logger.info("Geometry skipped: %s %s %s (sl)", pattern_name, direction, NO_STRUCTURAL_GEOMETRY)
        return None
    tp_result = _resolve_tp(entry, sl, direction, equal_highs, equal_lows, swing_highs, swing_lows)
    if tp_result is None:
        logger.info("Geometry skipped: %s %s RR remained below %.2f", pattern_name, direction, MIN_ACCEPTABLE_RR)
        return None
    tp1, tp_reason, rr, tp_analysis = tp_result
    analysis.update(tp_analysis)
    if tp_reason == "fallback_rr_target":
        _last_geometry_skip_reason = NO_STRUCTURAL_GEOMETRY
        logger.info("Geometry skipped: %s %s %s (tp)", pattern_name, direction, NO_STRUCTURAL_GEOMETRY)
        return None
    tp2_fallback, _ = _fallback_tp_with_reason(entry, risk, direction)
    if direction == "LONG":
        tp2 = max(tp1, tp2_fallback)
    else:
        tp2 = min(tp1, tp2_fallback)

    if risk <= 0 or rr <= 0:
        return None
    if rr < MIN_ACCEPTABLE_RR:
        return None
    if rr < config.MIN_RR:
        logger.info(f"Geometry skipped: RR={rr:.2f} < {config.MIN_RR}")
        return None

    logger.info(
        "Geometry: %s %s | entry=%.2f | sl=%.2f (%s) | tp1=%.2f (%s) | RR=%.2f",
        pattern_name,
        direction,
        entry,
        sl,
        sl_reason,
        tp1,
        tp_reason,
        rr,
    )

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
            candle_dna = _load_json(config.CANDLE_DNA_STATE_FILE)
            current_price = _get_current_price(zones)

            geometry = _compute_geometry(pattern, zones, current_price, candle_dna)

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
                "reason": _last_geometry_skip_reason,
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


def compute_geometry_from_data(pattern, zones, current_price, candle_dna=None):
    """Public API for testing."""
    return _compute_geometry(pattern, zones, current_price, candle_dna)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_geometry_engine())
