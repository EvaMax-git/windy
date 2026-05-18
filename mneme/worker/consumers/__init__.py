"""Mneme Worker Consumers — outbox event handlers registered with the Dispatcher.

Phase 2 (P2-07) adds:
- ReviewEventConsumer — handles review lifecycle events.

Phase 3 (P3-04) adds:
- PipelineEventConsumer — handles pipeline.run.requested outbox events,
  creating root jobs and executing pipeline steps.

Phase 4 (P4-09) adds:
- MemoryEventConsumer — handles message.created / raw_event.created events,
  triggering the Memory Extract Pipeline to extract candidate memories.
"""

from __future__ import annotations

from mneme.worker.consumers.review_consumer import ReviewEventConsumer
from mneme.worker.consumers.pipeline_consumer import PipelineEventConsumer
from mneme.worker.consumers.memory_consumer import MemoryEventConsumer

__all__ = ["ReviewEventConsumer", "PipelineEventConsumer", "MemoryEventConsumer"]
