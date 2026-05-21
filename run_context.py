"""Entry point for Service 2: Context Engine only."""
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
logger = logging.getLogger("run_context")

TEST_MODE = "--test" in sys.argv


async def test_check() -> None:
    logger.info("=== CONTEXT ENGINE TEST MODE ===")
    import aiohttp

    from services.context.trend_engine import _fetch_candles as fetch_trend
    from services.context.regime_engine import _fetch_candles as fetch_regime
    from services.context.zone_engine import run_zone_engine

    async with aiohttp.ClientSession() as session:
        trend_candles = await fetch_trend(session)
        logger.info(f"Trend candles fetched: {len(trend_candles)}")
        if trend_candles:
            logger.info(f"  First: {trend_candles[0]}")
            logger.info(f"  Last:  {trend_candles[-1]}")

        regime_candles = await fetch_regime(session)
        logger.info(f"Regime candles fetched: {len(regime_candles)}")

    # Ensure state dirs exist
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("State directory OK")
    logger.info("=== CONTEXT TEST COMPLETE ===")


async def run() -> None:
    if TEST_MODE:
        await test_check()
        return

    from services.context.trend_engine import run_trend_engine
    from services.context.regime_engine import run_regime_engine
    from services.context.zone_engine import run_zone_engine

    await asyncio.gather(
        run_trend_engine(),
        run_regime_engine(),
        run_zone_engine(),
        return_exceptions=True,
    )


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
