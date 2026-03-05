"""Pipeline step timing context manager for structured performance logging."""

import logging
import time
from contextlib import contextmanager
from typing import Generator


@contextmanager
def pipeline_step(
    logger: logging.Logger, name: str, **start_meta
) -> Generator[dict, None, None]:
    """Log start/done with elapsed time for a named pipeline step.

    Yields a mutable result dict. Populate it inside the block to include
    extra key=value pairs in the done log line.

    Usage::

        with pipeline_step(logger, "transcription", chars=12345) as step:
            result = await do_work()
            step["segments"] = len(result)

    Produces::

        INFO:module:[step] transcription | start | chars=12345
        INFO:module:[step] transcription | done | elapsed=23.4s | segments=42
    """
    parts = [f"{k}={v}" for k, v in start_meta.items()]
    logger.info(
        "[step] %s | start%s", name, (" | " + " | ".join(parts)) if parts else ""
    )
    t0 = time.monotonic()
    result: dict = {}
    try:
        yield result
    except Exception:
        elapsed = time.monotonic() - t0
        logger.warning("[step] %s | error | elapsed=%.1fs", name, elapsed)
        raise
    else:
        elapsed = time.monotonic() - t0
        done_parts = [f"elapsed={elapsed:.1f}s"] + [f"{k}={v}" for k, v in result.items()]
        logger.info("[step] %s | done | %s", name, " | ".join(done_parts))
