"""
Reads last 30s of 1s scores, computes continuity/momentum/consistency,
detects micro event type, and writes state/latest_micro_event.json.
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


def _read_recent_scores(window_seconds: int = 30) -> list[dict]:
    if not config.SCORES_FILE.exists():
        return []
    cutoff_ms = int(time.time() * 1000) - window_seconds * 1000
    records = []
    try:
        with open(config.SCORES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("timestamp_ms", 0) >= cutoff_ms:
                        records.append(rec)
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return records


def _compute_micro_event(records: list[dict]) -> dict:
    now_ms = int(time.time() * 1000)

    if len(records) < 5:
        return {
            "timestamp_ms": now_ms,
            "event_type": "NEUTRAL_NOISE",
            "direction": "NEUTRAL",
            "continuity": 0.0,
            "momentum": 0.0,
            "consistency": 0.0,
            "sample_count": len(records),
        }

    scores = [r["score"] for r in records]
    n = len(scores)

    # Continuity: fraction of same-sign scores as final score
    final_sign = 1 if scores[-1] >= 0 else -1
    continuity = sum(1 for s in scores if (1 if s >= 0 else -1) == final_sign) / n

    # Momentum: slope of linear trend
    x_mean = (n - 1) / 2.0
    numerator = sum((i - x_mean) * s for i, s in enumerate(scores))
    denominator = sum((i - x_mean) ** 2 for i in range(n)) or 1e-9
    momentum = numerator / denominator
    momentum_norm = max(-1.0, min(1.0, momentum / 2.0))

    # Consistency: how uniform the magnitude is
    avg_score = sum(scores) / n
    variance = sum((s - avg_score) ** 2 for s in scores) / n
    consistency = max(0.0, 1.0 - variance / 25.0)

    avg_abs = sum(abs(s) for s in scores) / n
    is_long = avg_score > 0

    # Absorptions from records
    absorption_count = sum(1 for r in records if r.get("absorption", 0) > 0.3)

    event_type = "NEUTRAL_NOISE"
    direction = "NEUTRAL"

    if continuity > 0.7 and avg_abs > 3.0 and momentum_norm > 0.2:
        event_type = "PRESSURE_BUILDING_LONG" if is_long else "PRESSURE_BUILDING_SHORT"
        direction = "LONG" if is_long else "SHORT"
    elif continuity > 0.6 and avg_abs > 2.0 and momentum_norm < -0.1:
        event_type = "EXHAUSTION_LONG" if is_long else "EXHAUSTION_SHORT"
        direction = "LONG" if is_long else "SHORT"
    elif absorption_count > n * 0.4 and avg_abs < 3.0:
        event_type = "ABSORPTION_EVENT"
        direction = "LONG" if is_long else "SHORT"
    elif avg_abs < 2.0:
        event_type = "NEUTRAL_NOISE"
        direction = "NEUTRAL"
    elif avg_abs >= 2.0:
        direction = "LONG" if is_long else "SHORT"

    return {
        "timestamp_ms": now_ms,
        "event_type": event_type,
        "direction": direction,
        "continuity": round(continuity, 4),
        "momentum": round(momentum_norm, 4),
        "consistency": round(consistency, 4),
        "avg_score": round(avg_score, 4),
        "sample_count": n,
    }


async def run_micro_event_cloud() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            records = _read_recent_scores(30)
            event = _compute_micro_event(records)
            tmp = config.MICRO_EVENT_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(event, indent=2), encoding="utf-8")
            tmp.replace(config.MICRO_EVENT_FILE)
        except Exception as e:
            logger.warning(f"micro_event_cloud error: {e}")
        await asyncio.sleep(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_micro_event_cloud())
