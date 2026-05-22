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


def apply_context_multiplier(confidence, direction, trend, regime):
    mult = 1.0

    if direction == "LONG" and trend == "TREND_UP":
        mult *= 1.3
    elif direction == "LONG" and trend == "TREND_DOWN":
        mult *= 0.5
    elif direction == "SHORT" and trend == "TREND_DOWN":
        mult *= 1.3
    elif direction == "SHORT" and trend == "TREND_UP":
        mult *= 0.5

    if regime == "COMPRESSION":
        mult *= 0.6
    elif regime == "EXPANSION":
        mult *= 1.2
    elif regime == "RANGE":
        mult *= 0.8

    return min(1.0, _coerce_float(confidence) * mult)


def _load_suppressed_state() -> dict:
    return _load_json(config.SUPPRESSED_FILE) or {}


def _find_suppressed_entry(suppressed_state: dict, combo_key: str) -> dict | None:
    combinations = suppressed_state.get("combinations", {})
    entry = combinations.get(combo_key)
    if entry and entry.get("status") == "SUPPRESSED":
        return entry
    for item in suppressed_state.get("suppressed", []):
        if item.get("combo_key") == combo_key:
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
) -> dict:
    now_ms = int(time.time() * 1000)
    pattern = geometry.get("pattern", "NONE")
    direction = geometry.get("direction", "NEUTRAL")
    confidence = _coerce_float(geometry.get("confidence", 0.0))
    trend_dir = trend.get("trend", "NO_TREND") if trend else "NO_TREND"
    regime_type = regime.get("regime", "UNKNOWN") if regime else "UNKNOWN"
    context_adjusted_confidence = apply_context_multiplier(confidence, direction, trend_dir, regime_type)
    decision = _base_decision(now_ms, pattern, direction, confidence, context_adjusted_confidence)

    timeframe = str(geometry.get("timeframe", "1m"))
    session = _utc_session(now_ms)
    # Canonical combo key: pattern|direction|timeframe|session|trend|regime
    combo_key = f"{pattern}|{direction}|{timeframe}|{session}|{trend_dir}|{regime_type}"
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

    suppressed_entry = _find_suppressed_entry(suppressed_state, combo_key)
    if suppressed_entry:
        decision.update({
            "decision": "BLOCKED",
            "reason": "SUPPRESSED_COMBINATION",
            "tags": ["suppressed"],
        })
        return decision

    if context_adjusted_confidence < config.MIN_CONTEXT_CONFIDENCE:
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
    return decision


async def run_decision_gate() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            geometry = _load_json(config.GEOMETRY_FILE) or {}
            trend = _load_json(config.TREND_FILE)
            regime = _load_json(config.REGIME_FILE)
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
