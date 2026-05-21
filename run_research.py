"""Entry point for Service 3: Research Engine only."""
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
logger = logging.getLogger("run_research")


async def run() -> None:
    from services.research.edge_matrix import run_edge_matrix
    from services.research.model_promoter import run_model_promoter
    from services.research.reporter import run_reporter

    await asyncio.gather(
        run_edge_matrix(),
        run_model_promoter(),
        run_reporter(),
        return_exceptions=True,
    )


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
