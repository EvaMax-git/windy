"""P4-12 Emotion Inference Sweeper — periodic background task that infers
emotion_charge and uncertainty_score for active memories.

Runs inside the worker main loop (similar to DecaySweeper).
Each sweep:
1. Fetches a batch of active memories that need emotion inference
   (never inferred, not inferred recently, or high uncertainty).
2. Runs the behavior-based inference engine on memory_text.
3. Updates emotion_charge and uncertainty_score.

Configuration
-------------
All tunables from ``mneme.config.Settings``:

* ``worker_emotion_infer_enabled`` (default ``True``)
* ``worker_emotion_infer_interval_seconds`` (default 600 = 10 min)
* ``emotion_infer_batch_size`` (default 200)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from mneme.config import get_settings
from mneme.db.base import SessionLocal
from mneme.memory.emotion import apply_emotion_inference_batch

logger = logging.getLogger(__name__)


@dataclass
class EmotionSweepResult:
    """Result of a single emotion inference sweeper cycle."""

    memories_processed: int = 0
    emotions_updated: int = 0
    emotion_counts: dict[str, int] = field(default_factory=dict)
    avg_uncertainty: float = 0.0
    errors: int = 0


class EmotionSweeper:
    """Periodic sweeper that infers emotion_charge for memories."""

    def __init__(self) -> None:
        self._settings = get_settings()

    def sweep(self) -> EmotionSweepResult:
        """Execute one emotion inference sweep cycle.

        Opens its own DB session, runs inference, and returns the result.
        """
        db = SessionLocal()
        try:
            result = apply_emotion_inference_batch(
                db,
                limit=self._settings.emotion_infer_batch_size,
            )

            return EmotionSweepResult(
                memories_processed=result.total_processed,
                emotions_updated=result.emotions_updated,
                emotion_counts=result.emotion_counts,
                avg_uncertainty=result.avg_uncertainty,
                errors=len(result.errors),
            )
        except Exception as exc:
            logger.error("emotion inference sweeper error: %s", exc, exc_info=True)
            db.rollback()
            return EmotionSweepResult(errors=1)
        finally:
            db.close()


def create_emotion_sweeper() -> EmotionSweeper:
    """Factory function for creating an EmotionSweeper instance."""
    return EmotionSweeper()
