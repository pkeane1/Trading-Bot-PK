"""Entry point: python -m kalshi_bot"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

from kalshi_bot.config import load_config
from kalshi_bot.logging_config import setup_logging
from kalshi_bot.scheduler.orchestrator import Orchestrator


async def main() -> None:
    load_dotenv()
    config = load_config()
    setup_logging(log_level=config.log_level, json_output=config.json_logs)

    orchestrator = Orchestrator(config)
    try:
        await orchestrator.start()
        # Keep running until interrupted (Windows-compatible)
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await orchestrator.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
