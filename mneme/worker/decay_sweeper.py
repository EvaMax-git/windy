"""P4-11 Memory Time Decay Sweeper — periodic background task that applies
time-decay to all active memories.

Runs inside the worker main loop (similar to RetrySweeper / MemoryAutoExtractSweeper).
Each sweep:
1. Fetches a batch of active memories ordered by least-recently-decayed.
2. Computes the elapsed decay based on ``last_decayed_at``.
3. Updates ``decay_score`` and transitions ``decay_state`` as needed.
4. Logs state transitions (active→decaying→silent→archived).

Configuration
-------------
All tunables from ``mneme.config.Settings``:

* ``worker_memory_decay_enabled`` (default ``True``)
* ``worker_memory_decay_interval_seconds`` (default 300 = 5 min)
* ``decay_rate_per_day`` (default 0.05)
* ``decay_max_batch_size`` (default 500)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from mneme.config import get_settings
from mneme.db.base import SessionLocal
from mneme.memory.decay import apply_decay_batch

logger = logging.getLogger(__name__)


@dataclass
class DecaySweepResult:
    """Result of a single decay sweeper cycle."""

    memories_processed: int = 0
    scores_updated: int = 0
    state_transitions: int = 0
    errors: int = 0


class DecaySweeper:
    """Periodic sweeper that applies time-decay to memories."""

    def __init__(self) -> None:
        self._settings = get_settings()

    def sweep(self) -> DecaySweepResult:
        """Execute one decay sweep cycle.

        Opens its own DB session, applies decay, and returns the result.
        """
        db = SessionLocal()
        try:
            result = apply_decay_batch(
                db,
                limit=self._settings.decay_max_batch_size,
            )

            return DecaySweepResult(
                memories_processed=result.total_processed,
                scores_updated=result.scores_updated,
                state_transitions=len(result.transitions),
                errors=len(result.errors),
            )
        except Exception as exc:
            logger.error("decay sweeper error: %s", exc, exc_info=True)
            db.rollback()
            return DecaySweepResult(errors=1)
        finally:
            db.close()


def create_decay_sweeper() -> DecaySweeper:
    """Factory function for creating a DecaySweeper instance."""
    return DecaySweeper()
