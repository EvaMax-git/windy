"""Pipeline Orchestrator — runs the context assembly pipeline.

The orchestrator is the single entry-point that wires together all
``PipelineStep`` implementations and executes them in order.  It is
independent of any specific strategy or card type — new steps can
be inserted without modifying the orchestrator.
"""

from __future__ import annotations

import logging
from typing import Any

from mneme.context.pipeline.base import PipelineContext, PipelineStep
from mneme.context.pipeline.steps import (
    ResolveStoresStep,
    ResolveStrategiesStep,
    AllocateBudgetStep,
    LoadContentStep,
    AssembleTextStep,
    WriteAuditStep,
)

logger = logging.getLogger(__name__)


# ── Default pipeline steps (in execution order) ────────────────────────────

_DEFAULT_PIPELINE: list[type[PipelineStep]] = [
    ResolveStoresStep,
    ResolveStrategiesStep,
    AllocateBudgetStep,
    LoadContentStep,
    AssembleTextStep,
    WriteAuditStep,
]


class PipelineOrchestrator:
    """Orchestrate the context assembly pipeline.

    Usage::

        orch = PipelineOrchestrator()
        ctx = PipelineContext(db=db, ...)
        ctx = orch.run(ctx)

    To add or reorder steps without touching engine core::

        orch = PipelineOrchestrator(steps=[
            ResolveStoresStep,
            ResolveStrategiesStep,
            MyCustomStep,        # ← your step
            AllocateBudgetStep,
            LoadContentStep,
            AssembleTextStep,
            WriteAuditStep,
        ])
    """

    def __init__(
        self,
        steps: list[type[PipelineStep]] | None = None,
    ) -> None:
        """Create an orchestrator with the given steps.

        Parameters
        ----------
        steps : list[type[PipelineStep]] | None
            Pipeline step classes in execution order.
            Defaults to the standard 6-step pipeline.
        """
        self._step_classes = steps or list(_DEFAULT_PIPELINE)

    def run(self, ctx: PipelineContext) -> PipelineContext:
        """Execute all steps in order against *ctx*.

        If any step raises an exception, steps already executed are
        rolled back in reverse order.

        Parameters
        ----------
        ctx : PipelineContext
            Initial context with input fields populated.

        Returns
        -------
        PipelineContext
            The context after all steps have executed (or been
            partially rolled back).
        """
        executed: list[PipelineStep] = []

        try:
            for step_cls in self._step_classes:
                step = step_cls()
                logger.debug("Pipeline ▶ %s", step.name)
                ctx = step.execute(ctx)
                executed.append(step)
        except Exception as exc:
            logger.error(
                "Pipeline step failed: %s. Rolling back %d executed steps.",
                exc,
                len(executed),
            )
            # Rollback in reverse order
            for step in reversed(executed):
                try:
                    logger.debug("Rollback ◀ %s", step.name)
                    ctx = step.rollback(ctx)
                except Exception as rollback_exc:
                    logger.error(
                        "Rollback of %s also failed: %s", step.name, rollback_exc
                    )
            raise

        return ctx
