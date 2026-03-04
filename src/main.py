"""Main entry point for Sidekick application."""

import asyncio
import logging
import sys
from pathlib import Path

import uvicorn

from config.settings import get_settings


def main() -> None:
    """Run the Sidekick application."""
    settings = get_settings()

    # Ensure data directory exists
    Path("data").mkdir(exist_ok=True)

    # Configure application logging so logger.info() calls are captured in sidekick.log.
    # Must run before uvicorn.run() — uvicorn's dictConfig uses disable_existing_loggers=False
    # so this root handler is preserved after uvicorn configures its own loggers.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
        stream=sys.stderr,
    )

    # Run uvicorn server
    uvicorn.run(
        "src.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info",
    )


if __name__ == "__main__":
    main()
