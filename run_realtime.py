"""Entry point for Service 1: Realtime Core only."""
import asyncio
import logging
import sys
from pathlib import Path

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("run_realtime")

DRY_RUN = "--dry-run" in sys.argv


async def dry_run_check() -> None:
    logger.info("=== DRY RUN MODE ===")
    logger.info("Checking imports and config...")

    from services.realtime.observer import Observer
    from services.realtime.micro_event_cloud import run_micro_event_cloud
    from services.realtime.candle_builder import run_candle_builder
    from services.realtime.pattern_engine import run_pattern_engine
    from services.realtime.geometry_engine import run_geometry_engine
    from services.realtime.decision_gate import run_decision_gate
    from services.realtime.paper_lifecycle import run_paper_lifecycle

    obs = Observer()
    record = obs._compute_score()
    logger.info(f"Score computed: {record}")

    # Ensure directories exist
    for d in [config.DATA_DIR, config.STATE_DIR, config.LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    logger.info("All directories OK")
    logger.info("=== DRY RUN COMPLETE ===")


async def run() -> None:
    if DRY_RUN:
        await dry_run_check()
        return

    from services.realtime.observer import Observer
    from services.realtime.micro_event_cloud import run_micro_event_cloud
    from services.realtime.candle_builder import run_candle_builder
    from services.realtime.pattern_engine import run_pattern_engine
    from services.realtime.geometry_engine import run_geometry_engine
    from services.realtime.decision_gate import run_decision_gate
    from services.realtime.paper_lifecycle import run_paper_lifecycle

    obs = Observer()
    await asyncio.gather(
        obs.run_with_reconnect(),
        run_micro_event_cloud(),
        run_candle_builder(),
        run_pattern_engine(),
        run_geometry_engine(),
        run_decision_gate(),
        run_paper_lifecycle(),
        return_exceptions=True,
    )


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
