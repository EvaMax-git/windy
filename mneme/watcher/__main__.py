"""Entry point for running the watcher as a module: python -m mneme.watcher"""

from __future__ import annotations

import logging
import sys

from mneme.config import get_settings
from mneme.watcher.watcher import Watcher


def main() -> None:
    """Run the file watcher."""
    settings = get_settings()

    # Configure logging (JSON structured, same as API and worker)
    from mneme.observability.logging import configure_logging
    configure_logging(settings.log_level)

    watch_dir = settings.storage_root + "/watch"
    interval = int(__import__("os").environ.get("MNEME_WATCH_INTERVAL", "5"))

    watcher = Watcher(watch_dir=watch_dir, interval=interval)

    logger = logging.getLogger("mneme.watcher")
    logger.info("Starting file watcher...")
    logger.info("  Watch directory: %s", watch_dir)
    logger.info("  Poll interval: %ds", interval)
    logger.info("  Drop files into public/ or private/ subdirectories")

    try:
        watcher.start()
    except KeyboardInterrupt:
        logger.info("Interrupted, shutting down...")
        watcher.stop()
    except Exception as e:
        logger.exception("Watcher failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
