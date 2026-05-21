"""
Reads edge matrix, promotes/suppresses pattern combinations based on expectancy.
Writes state/suppressed_patterns.json.
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

MIN_SAMPLE = 20


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _evaluate_matrix(matrix: dict) -> tuple[list[str], list[str], list[str]]:
    """Returns (sample_building, active, suppressed) pattern keys."""
    sample_building = []
    active = []
    suppressed = []

    for key, stats in matrix.items():
        n = stats.get("sample_count", 0)
        expectancy = stats.get("expectancy", 0.0)

        if n < MIN_SAMPLE:
            sample_building.append(key)
        elif expectancy > 0:
            active.append(key)
        else:
            suppressed.append(key)

    return sample_building, active, suppressed


def _extract_pattern_names(suppressed_keys: list[str]) -> list[str]:
    names = set()
    for key in suppressed_keys:
        parts = key.split("|")
        for part in parts:
            if part.startswith("pattern:"):
                names.add(part.replace("pattern:", ""))
    return list(names)


async def run_model_promoter() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            edge_data = _load_json(config.EDGE_MATRIX_FILE)
            if edge_data:
                matrix = edge_data.get("matrix", {})
                sample_building, active, suppressed_keys = _evaluate_matrix(matrix)

                suppressed_patterns = _extract_pattern_names(suppressed_keys)

                output = {
                    "timestamp_ms": int(time.time() * 1000),
                    "sample_building_count": len(sample_building),
                    "active_count": len(active),
                    "suppressed_count": len(suppressed_keys),
                    "suppressed": suppressed_patterns,
                    "suppressed_keys": suppressed_keys,
                    "active_keys": active,
                    "sample_building_keys": sample_building,
                }

                tmp = config.SUPPRESSED_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(output, indent=2), encoding="utf-8")
                tmp.replace(config.SUPPRESSED_FILE)

                logger.info(
                    f"Model promoter: {len(active)} active, "
                    f"{len(sample_building)} building, "
                    f"{len(suppressed_keys)} suppressed"
                )

        except Exception as e:
            logger.warning(f"model_promoter error: {e}")
        await asyncio.sleep(3600)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_model_promoter())
