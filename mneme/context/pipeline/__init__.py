"""Context Assembly Pipeline — orchestrator + step primitives.

Provides the composable pipeline that drives context assembly:

* ``PipelineContext`` — shared state dataclass
* ``PipelineStep`` — ABC for discrete assembly phases
* ``PipelineOrchestrator`` — runs steps in order with rollback
"""

from mneme.context.pipeline.base import PipelineContext, PipelineStep
from mneme.context.pipeline.orchestrator import PipelineOrchestrator

__all__ = [
    "PipelineContext",
    "PipelineStep",
    "PipelineOrchestrator",
]
