"""Mneme Restore v1 — restore preview and convenience wrappers.

This module provides restore preview functionality (P2-16) and re-exports
from the primary restore_engine implemented in ``mneme.backup.restore_engine``.

Package contents
----------------
* ``preview`` – ``preview_restore()``, ``RestorePreview`` for comparing
  backup manifest against live DB before restore.
* Re-exports from ``mneme.backup.restore_engine``:
  ``run_restore_drill()``, ``run_restore_live()``, ``RestoreReport``,
  ``RestoreResult``, ``list_restores()``
"""

from mneme.backup.restore_engine import (
    RestoreReport,
    RestoreResult,
    list_restores,
    run_restore_drill,
    run_restore_live,
)
from mneme.restore.preview import (
    RestorePreview,
    preview_restore,
)

__all__ = [
    "RestorePreview",
    "RestoreReport",
    "RestoreResult",
    "list_restores",
    "preview_restore",
    "run_restore_drill",
    "run_restore_live",
]
