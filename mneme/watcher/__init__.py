"""File watcher — auto-import new files from a monitored directory.

Monitors ``MNEME_WATCH_DIR`` for new files, deduplicates by content hash,
and processes them through the file processing pipeline.
"""

from mneme.watcher.watcher import Watcher, FileEvent

__all__ = ["Watcher", "FileEvent"]
