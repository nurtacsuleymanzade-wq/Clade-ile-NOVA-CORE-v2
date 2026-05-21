"""
NOVA CORE v2 — Main entry point.
Starts all 3 services as asyncio tasks with graceful shutdown.
"""
import asyncio
import logging
import logging.handlers
import signal
import sys
from pathlib import Path

import config

# Logging setup with rotation
config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
log_file = config.LOGS_DIR / "nova.log"

handler = logging.handlers.RotatingFileHandler(
    log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
handler.setFormatter(formatter)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(handler)
root_logger.addHandler(stream_handler)

logger = logging.getLogger("main")


async def run_realtime_service() -> None:
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


async def run_context_service() -> None:
    from services.context.trend_engine import run_trend_engine
    from services.context.regime_engine import run_regime_engine
    from services.context.zone_engine import run_zone_engine

    await asyncio.gather(
        run_trend_engine(),
        run_regime_engine(),
        run_zone_engine(),
        return_exceptions=True,
    )


async def run_research_service() -> None:
    from services.research.edge_matrix import run_edge_matrix
    from services.research.model_promoter import run_model_promoter
    from services.research.reporter import run_reporter

    await asyncio.gather(
        run_edge_matrix(),
        run_model_promoter(),
        run_reporter(),
        return_exceptions=True,
    )


async def main() -> None:
    logger.info("NOVA CORE v2 starting...")

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown():
        logger.info("Shutdown signal received")
        stop_event.set()

    try:
        loop.add_signal_handler(signal.SIGINT, _shutdown)
        loop.add_signal_handler(signal.SIGTERM, _shutdown)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler for SIGINT on all versions
        pass

    tasks = [
        asyncio.create_task(run_realtime_service(), name="realtime"),
        asyncio.create_task(run_context_service(), name="context"),
        asyncio.create_task(run_research_service(), name="research"),
    ]

    try:
        done, pending = await asyncio.wait(
            tasks + [asyncio.create_task(stop_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("Cancelling all tasks...")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("NOVA CORE v2 stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
