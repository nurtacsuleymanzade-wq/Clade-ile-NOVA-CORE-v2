"""
Reads edge matrix, promotes/suppresses combinations based on expectancy,
and writes state/suppressed_patterns.json with reasons.
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
from services.research.edge_matrix import evaluate_combo_status

logger = logging.getLogger(__name__)


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _current_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_reason(entry: dict) -> str:
    return (
        f"{entry.get('sample_count', 0)} sample, "
        f"winrate %{entry.get('winrate', 0.0) * 100:.1f}, "
        f"expectancy {entry.get('expectancy', 0.0):.2f}R"
    )


def _summary_key(entry: dict) -> str:
    return f"{entry.get('pattern', '')}|{entry.get('session', '')}|{entry.get('regime', '')}"


def _evaluate_matrix(matrix: dict) -> tuple[list[str], list[str], list[str]]:
    """Compatibility wrapper for legacy tests expecting key lists."""
    sample_building: list[str] = []
    active: list[str] = []
    suppressed: list[str] = []

    for key, stats in matrix.items():
        sample_count = stats.get("sample_count", 0)
        expectancy = stats.get("expectancy", 0.0)
        status, _, _ = evaluate_combo_status(sample_count, expectancy)
        if status == "ACTIVE":
            active.append(key)
        elif status == "SUPPRESSED":
            suppressed.append(key)
        else:
            sample_building.append(key)

    return sample_building, active, suppressed


def _extract_pattern_names(suppressed_keys: list[str]) -> list[str]:
    """Compatibility helper preserved for legacy callers."""
    names = set()
    for key in suppressed_keys:
        parts = key.split("|")
        for part in parts:
            if part.startswith("pattern:"):
                names.add(part.replace("pattern:", ""))
                continue
        if "|" not in key and ":" not in key:
            names.add(key)
    return sorted(names)


def _status_lists(canonical_combos: dict[str, dict]) -> tuple[list[dict], list[dict], list[dict], dict[str, dict]]:
    building: list[dict] = []
    active: list[dict] = []
    suppressed: list[dict] = []
    combinations: dict[str, dict] = {}

    for combo_key, combo in canonical_combos.items():
        sample_count = combo.get("sample_count", 0)
        expectancy = combo.get("expectancy", 0.0)
        status, suppress_reason, promote_reason = evaluate_combo_status(sample_count, expectancy)
        enriched = {
            **combo,
            "status": status,
            "suppress_reason": suppress_reason,
            "promote_reason": promote_reason,
            "summary_key": _summary_key(combo),
            "reason_text": _format_reason(combo),
            "evaluated_at": _current_iso(),
        }
        combinations[combo_key] = enriched
        if status == "ACTIVE":
            active.append(enriched)
        elif status == "SUPPRESSED":
            suppressed.append(enriched)
        else:
            building.append(enriched)

    return building, active, suppressed, combinations


def _log_status_transitions(previous: dict | None, current: dict[str, dict]) -> None:
    previous_map = (previous or {}).get("combinations", {})
    for combo_key, entry in current.items():
        old_status = (previous_map.get(combo_key) or {}).get("status")
        if old_status == entry["status"]:
            continue
        descriptor = f"{entry.get('pattern')} + {entry.get('session')} + {entry.get('regime')}"
        if entry["status"] == "SUPPRESSED":
            logger.warning(
                "SUPPRESSED: %s | Sebep: %s | Tarih: %s | Bu kombinasyon artik trade acmaz.",
                descriptor,
                entry["reason_text"],
                entry["evaluated_at"],
            )
        elif entry["status"] == "ACTIVE":
            logger.info(
                "ACTIVE: %s | Sebep: %s | Tarih: %s",
                descriptor,
                entry["reason_text"],
                entry["evaluated_at"],
            )


async def run_model_promoter() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            edge_data = _load_json(config.EDGE_MATRIX_FILE)
            if edge_data:
                canonical_combos = edge_data.get("canonical_combos", {})
                building, active, suppressed, combinations = _status_lists(canonical_combos)
                previous_state = _load_json(config.SUPPRESSED_FILE)

                output = {
                    "timestamp_ms": int(time.time() * 1000),
                    "evaluated_at": _current_iso(),
                    "sample_building_count": len(building),
                    "active_count": len(active),
                    "suppressed_count": len(suppressed),
                    "sample_building": building,
                    "active": active,
                    "suppressed": suppressed,
                    "active_combo_keys": [item["combo_key"] for item in active],
                    "suppressed_combo_keys": [item["combo_key"] for item in suppressed],
                    "suppressed_patterns": sorted({item["pattern"] for item in suppressed}),
                    "combinations": combinations,
                }

                _log_status_transitions(previous_state, combinations)

                tmp = config.SUPPRESSED_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp.replace(config.SUPPRESSED_FILE)

                logger.info(
                    "Model promoter: %s active, %s building, %s suppressed",
                    len(active),
                    len(building),
                    len(suppressed),
                )

        except Exception as e:
            logger.warning(f"model_promoter error: {e}")
        await asyncio.sleep(3600)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_model_promoter())
