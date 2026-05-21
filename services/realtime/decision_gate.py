"""
Reads geometry + context. Blocks trades only when structure is fully opposite
or geometry is incomplete. Writes state/latest_decision.json.
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


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_suppressed() -> set[str]:
    data = _load_json(config.SUPPRESSED_FILE)
    if not data:
        return set()
    return set(data.get("suppressed", []))


def _make_decision(
    geometry: dict,
    trend: dict | None,
    regime: dict | None,
    suppressed: set[str],
) -> dict:
    now_ms = int(time.time() * 1000)

    pat = geometry.get("pattern", "NONE")
    direction = geometry.get("direction", "NEUTRAL")
    entry = geometry.get("entry")
    sl = geometry.get("sl")
    tp1 = geometry.get("tp1")

    if pat == "NONE" or direction == "NEUTRAL":
        return {
            "timestamp_ms": now_ms,
            "decision": "NO_SIGNAL",
            "reason": "no_pattern",
            "pattern": pat,
            "direction": direction,
            "tags": [],
        }

    if entry is None or sl is None or tp1 is None:
        return {
            "timestamp_ms": now_ms,
            "decision": "BLOCKED",
            "reason": "geometry_incomplete",
            "pattern": pat,
            "direction": direction,
            "tags": [],
        }

    # Check suppressed patterns
    suppressed_key = f"{pat}_{direction}"
    if suppressed_key in suppressed or pat in suppressed:
        return {
            "timestamp_ms": now_ms,
            "decision": "BLOCKED",
            "reason": "pattern_suppressed",
            "pattern": pat,
            "direction": direction,
            "tags": ["suppressed"],
        }

    # Check structure bias contradiction
    trend_dir = trend.get("trend", "NO_TREND") if trend else "NO_TREND"
    opposite = (
        (direction == "LONG" and trend_dir == "TREND_DOWN")
        or (direction == "SHORT" and trend_dir == "TREND_UP")
    )
    if opposite and pat in ("CONTINUATION",):
        return {
            "timestamp_ms": now_ms,
            "decision": "BLOCKED",
            "reason": "structure_bias_opposite",
            "pattern": pat,
            "direction": direction,
            "tags": ["counter_trend"],
        }

    # Build metadata tags
    tags = []
    if trend_dir != "NO_TREND":
        tags.append(f"trend_{trend_dir.lower()}")
    regime_type = regime.get("regime", "UNKNOWN") if regime else "UNKNOWN"
    if regime_type != "UNKNOWN":
        tags.append(f"regime_{regime_type.lower()}")

    return {
        "timestamp_ms": now_ms,
        "decision": "ALLOW_PAPER",
        "reason": "ok",
        "pattern": pat,
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": geometry.get("tp2"),
        "rr": geometry.get("rr"),
        "tags": tags,
        "safe_to_open_real_trade": config.safe_to_open_real_trade,
    }


async def run_decision_gate() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            geometry = _load_json(config.GEOMETRY_FILE) or {}
            trend = _load_json(config.TREND_FILE)
            regime = _load_json(config.REGIME_FILE)
            suppressed = _load_suppressed()

            decision = _make_decision(geometry, trend, regime, suppressed)

            tmp = config.DECISION_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(decision, indent=2), encoding="utf-8")
            tmp.replace(config.DECISION_FILE)
        except Exception as e:
            logger.warning(f"decision_gate error: {e}")
        await asyncio.sleep(2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_decision_gate())
