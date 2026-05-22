"""
Reads geometry + context. Applies context-aware confidence gating and
writes state/latest_decision.json.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

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


def _read_last_jsonl_record(path: Path) -> dict | None:
    if not path.exists():
        return None
    last_record = None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    last_record = json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None
    return last_record


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
    swing_highs = trend.get("swing_highs_count", 0)
    swing_lows = trend.get("swing_lows_count", 0)
    return f"swings high={swing_highs}, low={swing_lows}"


def _regime_reason(regime: dict | None) -> str:
    if not regime:
        return "regime_state_missing"
    if regime.get("reason"):
        return str(regime["reason"])
    atr = regime.get("atr", 0.0)
    delta_consistency = regime.get("delta_consistency", 0.0)
    return f"atr={atr}, delta_consistency={delta_consistency}"


def _coerce_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _is_continuation_pattern(pattern: str) -> bool:
    return pattern == "CONTINUATION" or pattern.startswith("CONTINUATION_")


def _base_pattern_name(pattern: str) -> str:
    if not pattern:
        return "NONE"
    if pattern in {"STOP_HUNT_RECLAIM_LONG", "STOP_HUNT_RECLAIM_SHORT"}:
        return "STOP_HUNT"
    if pattern in {"ABSORPTION_REVERSAL_LONG", "ABSORPTION_REVERSAL_SHORT"}:
        return "ABSORPTION"
    if pattern.endswith("_LONG") or pattern.endswith("_SHORT"):
        return pattern.rsplit("_", 1)[0]
    return pattern


def _is_reversal_pattern(pattern: str) -> bool:
    return _base_pattern_name(pattern) in {"STOP_HUNT", "ABSORPTION", "REVERSAL", "TRAP"}


def _context_block_reason(pattern: str, session: str, trend: str, regime: str) -> str | None:
    if _is_continuation_pattern(pattern) and (regime == "COMPRESSION" or trend == "NO_TREND"):
        return "CONTINUATION_REQUIRES_TREND"
    if session == "OFF_SESSION" and regime == "COMPRESSION":
        if _is_reversal_pattern(pattern):
            return None
        return "LOW_QUALITY_SESSION_CONTEXT"
    if regime == "COMPRESSION" and trend == "NO_TREND":
        return "INVALID_MARKET_CONTEXT"
    if regime == "COMPRESSION" and trend != "NO_TREND" and pattern != "COMPRESSION_BREAKOUT":
        if _is_reversal_pattern(pattern):
            return None
        return "INVALID_MARKET_CONTEXT"
    return None


def apply_context_multiplier(confidence, direction, trend, regime, pattern: str = "NONE"):
    mult = 1.0

    if _is_reversal_pattern(pattern):
        if direction == "LONG" and trend == "TREND_DOWN":
            mult *= 1.15
        elif direction == "LONG" and trend == "TREND_UP":
            mult *= 0.95
        elif direction == "SHORT" and trend == "TREND_UP":
            mult *= 1.15
        elif direction == "SHORT" and trend == "TREND_DOWN":
            mult *= 0.95
    else:
        if direction == "LONG" and trend == "TREND_UP":
            mult *= 1.3
        elif direction == "LONG" and trend == "TREND_DOWN":
            mult *= 0.5
        elif direction == "SHORT" and trend == "TREND_DOWN":
            mult *= 1.3
        elif direction == "SHORT" and trend == "TREND_UP":
            mult *= 0.5

    if regime == "COMPRESSION":
        if _is_reversal_pattern(pattern):
            mult *= 1.0
        else:
            mult *= 0.6
    elif regime == "EXPANSION":
        mult *= 1.2
    elif regime == "RANGE":
        mult *= 0.8

    return min(1.0, _coerce_float(confidence) * mult)


def _nearest_above(prices: list[float], level: float) -> float | None:
    candidates = [price for price in prices if price > level]
    return min(candidates) if candidates else None


def _nearest_below(prices: list[float], level: float) -> float | None:
    candidates = [price for price in prices if price < level]
    return max(candidates) if candidates else None


def _fallback_geometry_from_pattern(pattern_state: dict | None, zones: dict | None, candle_dna: dict | None) -> dict | None:
    if not pattern_state:
        return None
    pattern = str(pattern_state.get("pattern", "NONE"))
    direction = str(pattern_state.get("direction", "NEUTRAL"))
    if pattern == "NONE" or direction == "NEUTRAL":
        return None

    current_price = _coerce_float((zones or {}).get("current_price"), 0.0)
    if current_price <= 0:
        return None

    equal_highs = [_coerce_float(v) for v in (zones or {}).get("equal_highs", []) if _coerce_float(v) > 0]
    swing_highs = [_coerce_float(v) for v in (zones or {}).get("swing_highs", []) if _coerce_float(v) > 0]
    equal_lows = [_coerce_float(v) for v in (zones or {}).get("equal_lows", []) if _coerce_float(v) > 0]
    swing_lows = [_coerce_float(v) for v in (zones or {}).get("swing_lows", []) if _coerce_float(v) > 0]

    highs = sorted(set(equal_highs + swing_highs))
    lows = sorted(set(equal_lows + swing_lows))
    entry = current_price
    candle_low = _coerce_float((candle_dna or {}).get("low"), 0.0)
    candle_high = _coerce_float((candle_dna or {}).get("high"), 0.0)
    min_risk = max(entry * 0.002, 1.0)

    if direction == "LONG":
        sl_anchor = _nearest_below(lows, entry)
        sl = (sl_anchor - (entry * 0.0003)) if sl_anchor is not None else (candle_low - min_risk if candle_low > 0 else entry - min_risk)
        if sl >= entry:
            sl = entry - min_risk
        risk = entry - sl
        reward = max(risk * 1.5, entry * 0.001)
        tp_candidates = [price for price in highs if price >= entry + reward]
        tp1 = min(tp_candidates) if tp_candidates else entry + reward
        tp2 = max(tp1, entry + (risk * 2.0))
    else:
        sl_anchor = _nearest_above(highs, entry)
        sl = (sl_anchor + (entry * 0.0003)) if sl_anchor is not None else (candle_high + min_risk if candle_high > 0 else entry + min_risk)
        if sl <= entry:
            sl = entry + min_risk
        risk = sl - entry
        reward = max(risk * 1.5, entry * 0.001)
        tp_candidates = [price for price in lows if price <= entry - reward]
        tp1 = max(tp_candidates) if tp_candidates else entry - reward
        tp2 = min(tp1, entry - (risk * 2.0))

    if risk <= 0:
        return None

    return {
        "timestamp_ms": int(time.time() * 1000),
        "pattern": pattern,
        "base_pattern": pattern,
        "timeframe": str(pattern_state.get("timeframe", "1m")),
        "direction": direction,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "rr": round(abs(tp1 - entry) / risk, 3),
        "current_price": round(current_price, 2),
        "confidence": round(_coerce_float(pattern_state.get("confidence", 0.0)), 4),
        "pattern_reason": str(pattern_state.get("pattern_reason", "")),
        "entry_reason": "decision_gate_pattern_fallback",
        "sl_reason": "decision_gate_liquidity_fallback",
        "tp_reason": "decision_gate_rr_fallback",
        "analysis": {
            "fallback_source": "latest_pattern",
        },
    }


def _load_suppressed_state() -> dict:
    return _load_json(config.SUPPRESSED_FILE) or {}


def _suppressed_combo_keys(suppressed_state: dict) -> set[str]:
    suppressed_keys: set[str] = set()
    for combo_key in suppressed_state.get("suppressed_combo_keys", []):
        if combo_key:
            suppressed_keys.add(str(combo_key))
    for item in suppressed_state.get("suppressed", []):
        combo_key = item.get("combo_key")
        if combo_key:
            suppressed_keys.add(str(combo_key))
    for combo_key, item in (suppressed_state.get("combinations") or {}).items():
        if item.get("status") == "SUPPRESSED" and combo_key:
            suppressed_keys.add(str(combo_key))
    return suppressed_keys


def _suppressed_summary_keys(suppressed_state: dict) -> set[str]:
    summary_keys: set[str] = set()
    for item in suppressed_state.get("suppressed", []):
        if item.get("status") != "SUPPRESSED":
            continue
        summary_key = item.get("summary_key")
        if summary_key:
            summary_keys.add(str(summary_key))
    for item in (suppressed_state.get("combinations") or {}).values():
        if item.get("status") != "SUPPRESSED":
            continue
        summary_key = item.get("summary_key")
        if summary_key:
            summary_keys.add(str(summary_key))
    return summary_keys


def _find_suppressed_entry(
    suppressed_state: dict,
    combo_key: str,
    pattern: str,
    session: str,
    trend: str,
    regime: str,
) -> dict | None:
    combinations = suppressed_state.get("combinations", {})
    if combo_key in combinations and combinations[combo_key].get("status") == "SUPPRESSED":
        return combinations[combo_key]

    summary_key = f"{pattern}|{session}|{regime}"
    for item in suppressed_state.get("suppressed", []):
        if item.get("combo_key") == combo_key:
            return item
        if item.get("summary_key") == summary_key and item.get("status") == "SUPPRESSED":
            return item
        if item.get("pattern") == pattern and item.get("trend") == trend and item.get("status") == "SUPPRESSED":
            return item
    return None


def _base_decision(now_ms: int, pattern: str, direction: str, confidence: float, context_adjusted_confidence: float) -> dict:
    return {
        "timestamp_ms": now_ms,
        "pattern": pattern,
        "direction": direction,
        "confidence": round(confidence, 4),
        "context_adjusted_confidence": round(context_adjusted_confidence, 4),
        "timeframe": "1m",
        "entry": None,
        "sl": None,
        "tp1": None,
        "tp2": None,
        "rr": 0.0,
        "observer_score": 0.0,
        "delta_at_entry": 0.0,
        "imbalance_at_entry": 0.0,
        "cvd_at_entry": 0.0,
        "body_ratio": 0.0,
        "micro_event": "",
        "pattern_reason": "",
        "entry_reason": "",
        "sl_reason": "",
        "tp_reason": "",
        "session": _utc_session(now_ms),
        "trend_at_entry": "NO_TREND",
        "regime_at_entry": "UNKNOWN",
        "trend_reason": "",
        "regime_reason": "",
        "combo_key": "",
        "tags": [],
        "lineage_chain": "",
        "safe_to_open_real_trade": config.safe_to_open_real_trade,
    }


def _make_decision(
    geometry: dict,
    trend: dict | None,
    regime: dict | None,
    suppressed_state: dict,
    latest_score: dict | None,
    micro_event: dict | None,
    candle_dna: dict | None,
    pattern_state: dict | None = None,
    zones: dict | None = None,
) -> dict:
    now_ms = int(time.time() * 1000)
    used_pattern_fallback = False
    if (
        geometry.get("pattern", "NONE") == "NONE"
        and (geometry.get("reason") == "NO_STRUCTURAL_GEOMETRY" or geometry.get("entry") is None)
    ):
        fallback_geometry = _fallback_geometry_from_pattern(pattern_state, zones, candle_dna)
        if fallback_geometry:
            geometry = fallback_geometry
            used_pattern_fallback = True
    pattern = geometry.get("pattern", "NONE")
    direction = geometry.get("direction", "NEUTRAL")
    confidence = _coerce_float(geometry.get("confidence", 0.0))
    trend_dir = trend.get("trend", "NO_TREND") if trend else "NO_TREND"
    regime_type = regime.get("regime", "UNKNOWN") if regime else "UNKNOWN"
    context_adjusted_confidence = apply_context_multiplier(confidence, direction, trend_dir, regime_type, pattern)
    decision = _base_decision(now_ms, pattern, direction, confidence, context_adjusted_confidence)

    timeframe = str(geometry.get("timeframe", "1m"))
    session = str(geometry.get("session") or _utc_session(now_ms))
    combo_key = f"{pattern}|{timeframe}|{session}|{trend_dir}"
    summary_key = f"{pattern}|{session}|{regime_type}"
    decision.update({
        "timeframe": timeframe,
        "session": session,
        "trend_at_entry": trend_dir,
        "regime_at_entry": regime_type,
        "trend_reason": _trend_reason(trend),
        "regime_reason": _regime_reason(regime),
        "combo_key": combo_key,
    })

    if pattern == "NONE" or direction == "NEUTRAL":
        decision.update({
            "decision": "NO_SIGNAL",
            "reason": "NO_PATTERN",
        })
        return decision

    entry = geometry.get("entry")
    sl = geometry.get("sl")
    tp1 = geometry.get("tp1")
    if entry is None or sl is None or tp1 is None:
        decision.update({
            "decision": "BLOCKED",
            "reason": "GEOMETRY_INCOMPLETE",
        })
        return decision

    suppressed_keys = _suppressed_combo_keys(suppressed_state)
    suppressed_summaries = _suppressed_summary_keys(suppressed_state)
    if combo_key in suppressed_keys or summary_key in suppressed_summaries:
        logger.info("SUPPRESSED_COMBINATION combo_key=%s summary_key=%s", combo_key, summary_key)
        decision.update({
            "decision": "BLOCKED",
            "reason": "SUPPRESSED_COMBINATION",
            "tags": ["suppressed"],
        })
        return decision

    if not used_pattern_fallback:
        suppressed_entry = _find_suppressed_entry(
            suppressed_state,
            combo_key,
            pattern,
            session,
            trend_dir,
            regime_type,
        )
        if suppressed_entry:
            logger.info("SUPPRESSED_COMBINATION combo_key=%s summary_key=%s", combo_key, summary_key)
            decision.update({
                "decision": "BLOCKED",
                "reason": "SUPPRESSED_COMBINATION",
                "tags": ["suppressed"],
            })
            return decision

        context_block_reason = _context_block_reason(pattern, session, trend_dir, regime_type)
        if context_block_reason:
            decision.update({
                "decision": "BLOCKED",
                "reason": context_block_reason,
            })
            return decision

        if context_adjusted_confidence < 0.3:
            decision.update({
                "decision": "BLOCKED",
                "reason": "LOW_CONTEXT_CONFIDENCE",
            })
            return decision

    observer_score = _coerce_float((latest_score or {}).get("score"))
    delta_at_entry = _coerce_float((latest_score or {}).get("delta"))
    imbalance_at_entry = _coerce_float((latest_score or {}).get("imbalance"))
    cvd_at_entry = _coerce_float((candle_dna or {}).get("cvd"))
    body_ratio = _coerce_float((candle_dna or {}).get("body_ratio"))
    micro_event_name = str((micro_event or {}).get("event_type", ""))
    pattern_reason = str(geometry.get("pattern_reason", ""))
    entry_reason = str(geometry.get("entry_reason", ""))
    sl_reason = str(geometry.get("sl_reason", ""))
    tp_reason = str(geometry.get("tp_reason", ""))

    tags = [
        f"trend_{trend_dir.lower()}",
        f"regime_{regime_type.lower()}",
        f"session_{session.lower()}",
    ]
    lineage_chain = " -> ".join(
        [
            f"{observer_score:.2f}",
            micro_event_name or "NO_MICRO_EVENT",
            pattern,
            entry_reason or "NO_ENTRY_REASON",
        ]
    )

    decision.update({
        "decision": "ALLOW_PAPER",
        "reason": "OK",
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": geometry.get("tp2"),
        "rr": geometry.get("rr", 0.0),
        "observer_score": round(observer_score, 4),
        "delta_at_entry": round(delta_at_entry, 6),
        "imbalance_at_entry": round(imbalance_at_entry, 4),
        "cvd_at_entry": round(cvd_at_entry, 6),
        "body_ratio": round(body_ratio, 4),
        "micro_event": micro_event_name,
        "pattern_reason": pattern_reason,
        "entry_reason": entry_reason,
        "sl_reason": sl_reason,
        "tp_reason": tp_reason,
        "tags": tags,
        "lineage_chain": lineage_chain,
        "current_price": geometry.get("current_price"),
    })
    if used_pattern_fallback:
        decision["tags"] = decision.get("tags", []) + ["geometry_fallback"]
    return decision


async def run_decision_gate() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            geometry = _load_json(config.GEOMETRY_FILE) or {}
            pattern_state = _load_json(config.PATTERN_FILE)
            trend = _load_json(config.TREND_FILE)
            regime = _load_json(config.REGIME_FILE)
            zones = _load_json(config.ZONES_FILE)
            suppressed_state = _load_suppressed_state()
            latest_score = _read_last_jsonl_record(config.SCORES_FILE)
            micro_event = _load_json(config.MICRO_EVENT_FILE)
            candle_dna = _load_json(config.CANDLE_DNA_STATE_FILE)

            decision = _make_decision(
                geometry,
                trend,
                regime,
                suppressed_state,
                latest_score,
                micro_event,
                candle_dna,
                pattern_state,
                zones,
            )

            tmp = config.DECISION_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(decision, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(config.DECISION_FILE)
        except Exception as e:
            logger.warning(f"decision_gate error: {e}")
        await asyncio.sleep(2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_decision_gate())
