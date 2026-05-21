"""
Aggregates 1s scores into 1m candles with candle DNA.
Writes data/candle_dna.jsonl and state/latest_candle_dna.json.
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

CANDLE_SECONDS = 60


def _read_scores_window(start_ms: int, end_ms: int) -> list[dict]:
    if not config.SCORES_FILE.exists():
        return []
    records = []
    try:
        with open(config.SCORES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = rec.get("timestamp_ms", 0)
                    if start_ms <= ts < end_ms:
                        records.append(rec)
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return records


def _build_candle_dna(records: list[dict], candle_start_ms: int) -> dict | None:
    if not records:
        return None

    scores = [r["score"] for r in records]
    deltas = [r.get("delta", 0.0) for r in records]
    absorptions = [r.get("absorption", 0.0) for r in records]
    large_lots = [r.get("large_lot", False) for r in records]

    open_s = scores[0]
    close_s = scores[-1]
    high_s = max(scores)
    low_s = min(scores)

    body = close_s - open_s
    total_range = high_s - low_s or 1e-9
    body_ratio = abs(body) / total_range

    upper_wick = (high_s - max(open_s, close_s)) / total_range
    lower_wick = (min(open_s, close_s) - low_s) / total_range

    cvd = sum(deltas)
    volume_proxy = sum(abs(d) for d in deltas)
    absorption = max(absorptions)
    large_lot_count = sum(1 for ll in large_lots if ll)

    # Displacement: net move in score direction
    displacement = abs(close_s - open_s)

    # Trapped: strong move then reversal
    mid_scores = scores[len(scores)//4: 3*len(scores)//4]
    mid_extreme = max(mid_scores) if body > 0 else min(mid_scores)
    trapped = abs(mid_extreme - close_s) > abs(body) * 0.5 if mid_scores else False

    n = len(records)
    return {
        "timestamp_ms": candle_start_ms,
        "open_score": round(open_s, 4),
        "close_score": round(close_s, 4),
        "high_score": round(high_s, 4),
        "low_score": round(low_s, 4),
        "body_ratio": round(body_ratio, 4),
        "upper_wick": round(upper_wick, 4),
        "lower_wick": round(lower_wick, 4),
        "cvd": round(cvd, 6),
        "volume": round(volume_proxy, 6),
        "absorption": round(absorption, 4),
        "trapped": trapped,
        "displacement": round(displacement, 4),
        "sample_count": n,
    }


async def run_candle_builder() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        now_ms = int(time.time() * 1000)
        candle_boundary_ms = (now_ms // (CANDLE_SECONDS * 1000)) * (CANDLE_SECONDS * 1000)
        prev_candle_start = candle_boundary_ms - CANDLE_SECONDS * 1000
        prev_candle_end = candle_boundary_ms

        sleep_to_next = (candle_boundary_ms + CANDLE_SECONDS * 1000 - now_ms) / 1000.0
        await asyncio.sleep(max(1, sleep_to_next))

        try:
            records = _read_scores_window(prev_candle_start, prev_candle_end)
            dna = _build_candle_dna(records, prev_candle_start)
            if dna is None:
                logger.warning("No records for candle window, skipping")
                continue

            line = json.dumps(dna) + "\n"
            with open(config.CANDLE_DNA_FILE, "a", encoding="utf-8") as f:
                f.write(line)

            tmp = config.CANDLE_DNA_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(dna, indent=2), encoding="utf-8")
            tmp.replace(config.CANDLE_DNA_STATE_FILE)

            logger.info(f"Candle built: cvd={dna['cvd']:.4f} body_ratio={dna['body_ratio']:.3f}")
        except Exception as e:
            logger.warning(f"candle_builder error: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_candle_builder())
