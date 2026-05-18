"""Mneme Worker – outbox poller and event dispatcher.

Public API
----------
* ``Consumer`` — abstract consumer base class
* ``Dispatcher`` — event dispatcher (routes events to registered consumers)
* ``main`` — worker process entry point
* Consumer implementations in ``mneme.worker.consumers``:
  * ``ReviewEventConsumer``, ``PipelineEventConsumer``, ``MemoryEventConsumer``
* Sweepers (run on independent intervals in the worker main loop):
  * ``RetrySweeper`` — retries failed deliveries with exponential backoff
  * ``RecoverySweeper`` — recovers events stuck in ``'dispatching'`` state
  * ``DecaySweeper`` — applies time-decay to active memories
  * ``EmotionSweeper`` — infers emotion_charge and uncertainty scores
  * ``SpontaneousRecallSweeper`` — 空闲扫描→发现矛盾→创建通知
  * ``SublimationSweeper`` — 5次相似事件→LLM抽象共识→写入用户画像
"""

from mneme.worker.dispatcher import Consumer, Dispatcher
from mneme.worker.consumers import (
    ReviewEventConsumer,
    PipelineEventConsumer,
    MemoryEventConsumer,
)
from mneme.worker.spontaneous_recall import SpontaneousRecallSweeper
from mneme.worker.sublimation_sweeper import SublimationSweeper

__all__ = [
    "Consumer",
    "Dispatcher",
    "ReviewEventConsumer",
    "PipelineEventConsumer",
    "MemoryEventConsumer",
    "SpontaneousRecallSweeper",
    "SublimationSweeper",
]
