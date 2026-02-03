"""Main entry point for Sidekick application."""

import asyncio
import sys
from pathlib import Path

import uvicorn

from config.settings import get_settings


def main() -> None:
    """Run the Sidekick application."""
    settings = get_settings()

    # Ensure data directory exists
    Path("data").mkdir(exist_ok=True)

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
