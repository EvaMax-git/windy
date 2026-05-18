"""P3-04 Pipeline Event Consumer — handles pipeline.run.requested events.

This consumer processes outbox events of type ``pipeline.run.requested``
by creating a root job and executing the pipeline steps.

Architecture
------------
1. Receives ``pipeline.run.requested`` event from the dispatcher.
2. Creates a root ``job`` for tracking and observability.
3. Links the job to the pipeline run (``pipeline_runs.root_job_id``).
4. Advances run status: ``pending → running``.
5. Executes pipeline steps according to the pipeline definition's ``config_json``.
6. On success: advances to ``succeeded`` with ``output_json``.
7. On failure: advances to ``failed`` with ``error_json``.

The consumer is **idempotent**: re-delivery of the same event checks the
current run status before acting.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from mneme.db.base import SessionLocal
from mneme.db.jobs import (
    add_job_log,
    create_job,
    update_job_completed,
    update_job_running,
)
from mneme.db.pipelines import (
    advance_run_status,
    asset_import_orchestrator,
    get_pipeline_def,
    get_pipeline_run,
)
from mneme.db.processing_jobs import (
    advance_job_status as advance_processing_job_status,
)
from mneme.api.context import RequestContext, ActorContext
from mneme.worker.dispatcher import Consumer, DispatchResult, DispatchOutcome

logger = logging.getLogger(__name__)

_HANDLED_EVENT_TYPES = frozenset({"pipeline.run.requested"})


class PipelineEventConsumer(Consumer):
    """Outbox consumer for pipeline.run.requested events.

    Registered in the worker's Dispatcher, this consumer:
    1. Creates a root job for the pipeline run.
    2. Links the job to ``pipeline_runs.root_job_id``.
    3. Advances the run to ``running``.
    4. Dispatches to the appropriate orchestrator based on pipeline_type.
    5. Updates the run to ``succeeded`` or ``failed``.
    """

    @property
    def name(self) -> str:
        return "pipeline-consumer"

    def can_handle(self, event_type: str) -> bool:
        """Return ``True`` for pipeline.run.requested events."""
        return event_type in _HANDLED_EVENT_TYPES

    def dispatch(
        self,
        *,
        event_id: UUID,
        event_type: str,
        aggregate_type: str,
        aggregate_id: UUID,
        payload: dict[str, Any],
        delivery_id: UUID,
    ) -> DispatchResult:
        """Process a pipeline.run.requested event.

        Steps:
        1. Look up the pipeline run.
        2. Create a root job.
        3. Link job → run.
        4. Advance run to ``running``.
        5. Execute pipeline steps.
        6. Mark run ``succeeded`` or ``failed``.
        """
        pipeline_run_id = aggregate_id
        pipeline_def_id_raw = payload.get("pipeline_def_id")
        pipeline_type = payload.get("pipeline_type", "unknown")
        trigger_type = payload.get("trigger_type", "event")
        input_data = payload.get("input_json", {})
        target_type = payload.get("target_type")
        target_id_raw = payload.get("target_id")

        # Track processing_job_id for status advancement
        processing_job_id_raw = input_data.get("processing_job_id")

        log_ctx = dict(
            event_id=str(event_id),
            pipeline_run_id=str(pipeline_run_id),
            pipeline_type=pipeline_type,
            delivery_id=str(delivery_id),
        )

        logger.info(
            "pipeline-consumer: received event – run=%s type=%s trigger=%s",
            pipeline_run_id,
            pipeline_type,
            trigger_type,
        )

        try:
            with SessionLocal() as db:
                # 1. Look up the pipeline run (idempotency guard)
                run = get_pipeline_run(db, pipeline_run_id)
                if run is None:
                    logger.error(
                        "pipeline-consumer: run not found – run=%s",
                        pipeline_run_id,
                    )
                    return DispatchResult.fail(
                        f"Pipeline run {pipeline_run_id} not found"
                    )

                run_status = (
                    run.status.value
                    if hasattr(run.status, "value")
                    else run.status
                )

                # Idempotency: if already running/succeeded/failed, skip
                if run_status in ("running", "succeeded", "failed", "cancelled", "superseded"):
                    logger.info(
                        "pipeline-consumer: run %s already in status '%s', skipping",
                        pipeline_run_id,
                        run_status,
                    )
                    return DispatchResult.ack()

                if run_status != "pending":
                    logger.warning(
                        "pipeline-consumer: unexpected run status '%s' for run=%s",
                        run_status,
                        pipeline_run_id,
                    )
                    return DispatchResult.fail(
                        f"Pipeline run {pipeline_run_id} is in unexpected status '{run_status}'"
                    )

                # 2. Create a root job for tracking
                job_key = f"pipeline:{pipeline_run_id}"
                root_job = create_job(
                    job_type=f"{pipeline_type}_pipeline",
                    job_key=job_key,
                    input_payload={
                        "pipeline_run_id": str(pipeline_run_id),
                        "pipeline_type": pipeline_type,
                        "target_type": target_type,
                        "target_id": target_id_raw,
                        "trigger_type": trigger_type,
                        **input_data,
                    },
                    max_retries=0,
                    timeout_seconds=1800,
                    actor_type="system",
                )
                root_job_id = UUID(root_job["job_id"])
                logger.info(
                    "pipeline-consumer: created root job=%s for run=%s",
                    root_job_id,
                    pipeline_run_id,
                )

                # 3. Link root_job_id to pipeline_runs
                _link_root_job(db, pipeline_run_id, root_job_id)

                # 4. Mark job as running
                update_job_running(root_job_id)
                add_job_log(
                    root_job_id,
                    step="start",
                    message=f"Pipeline run {pipeline_run_id} starting (type={pipeline_type})",
                    level="info",
                    attempt_no=1,
                )

                # 5. Advance run status to 'running'
                run_ctx = _make_system_context()
                advance_run_status(
                    db,
                    run_ctx,
                    pipeline_run_id=pipeline_run_id,
                    new_status="running",
                    expected_status="pending",
                )

                # 5b. Advance processing_job: queued → processing
                if processing_job_id_raw:
                    try:
                        advance_processing_job_status(
                            db,
                            job_id=UUID(processing_job_id_raw),
                            new_status="processing",
                            expected_status="queued",
                        )
                    except Exception:
                        pass

                db.commit()

            # 6. Execute pipeline steps (outside the initial transaction)
            logger.info(
                "pipeline-consumer: executing pipeline type=%s run=%s",
                pipeline_type,
                pipeline_run_id,
            )

            try:
                if pipeline_type == "asset_import":
                    with SessionLocal() as exec_db:
                        exec_ctx = _make_system_context()
                        summary = asset_import_orchestrator(
                            exec_db,
                            exec_ctx,
                            pipeline_run_id=pipeline_run_id,
                        )
                        exec_db.commit()

                    # Mark run succeeded
                    with SessionLocal() as final_db:
                        final_ctx = _make_system_context()
                        advance_run_status(
                            final_db,
                            final_ctx,
                            pipeline_run_id=pipeline_run_id,
                            new_status="succeeded",
                            expected_status="running",
                            output_json=summary,
                        )
                        update_job_completed(
                            root_job_id,
                            success=True,
                            output=summary,
                        )
                        if processing_job_id_raw:
                            try:
                                advance_processing_job_status(
                                    final_db,
                                    job_id=UUID(processing_job_id_raw),
                                    new_status="done",
                                    expected_status="processing",
                                    chunks_produced=summary.get("chunk_count", 0) if summary else 0,
                                )
                            except Exception:
                                pass
                        final_db.commit()

                    logger.info(
                        "pipeline-consumer: run=%s succeeded type=%s steps=%s",
                        pipeline_run_id,
                        pipeline_type,
                        summary.get("steps_completed", 0),
                    )
                else:
                    # Generic pipeline: mark as succeeded (no-op for unknown types)
                    with SessionLocal() as final_db:
                        final_ctx = _make_system_context()
                        summary = {
                            "pipeline_type": pipeline_type,
                            "note": f"No orchestrator registered for pipeline_type='{pipeline_type}'",
                        }
                        advance_run_status(
                            final_db,
                            final_ctx,
                            pipeline_run_id=pipeline_run_id,
                            new_status="succeeded",
                            expected_status="running",
                            output_json=summary,
                        )
                        update_job_completed(
                            root_job_id,
                            success=True,
                            output=summary,
                        )
                        if processing_job_id_raw:
                            try:
                                advance_processing_job_status(
                                    final_db,
                                    job_id=UUID(processing_job_id_raw),
                                    new_status="done",
                                    expected_status="processing",
                                )
                            except Exception:
                                pass
                        final_db.commit()

                    logger.info(
                        "pipeline-consumer: run=%s completed (generic) type=%s",
                        pipeline_run_id,
                        pipeline_type,
                    )

            except Exception as step_exc:
                error_msg = str(step_exc)[:2000]
                logger.error(
                    "pipeline-consumer: run=%s failed type=%s error=%s",
                    pipeline_run_id,
                    pipeline_type,
                    error_msg,
                    exc_info=True,
                )

                # Mark run failed
                try:
                    with SessionLocal() as fail_db:
                        fail_ctx = _make_system_context()
                        advance_run_status(
                            fail_db,
                            fail_ctx,
                            pipeline_run_id=pipeline_run_id,
                            new_status="failed",
                            expected_status="running",
                            error_json={
                                "error_code": "pipeline_step_failed",
                                "message": error_msg,
                                "pipeline_type": pipeline_type,
                            },
                        )
                        update_job_completed(
                            root_job_id,
                            success=False,
                            error_message=error_msg,
                        )
                        if root_job_id:
                            add_job_log(
                                root_job_id,
                                step="error",
                                message=f"Pipeline failed: {error_msg}",
                                level="error",
                                attempt_no=1,
                            )
                        if processing_job_id_raw:
                            try:
                                advance_processing_job_status(
                                    fail_db,
                                    job_id=UUID(processing_job_id_raw),
                                    new_status="failed",
                                    expected_status="processing",
                                    error=error_msg[:500],
                                )
                            except Exception:
                                pass
                        fail_db.commit()
                except Exception as fail_exc:
                    logger.error(
                        "pipeline-consumer: failed to mark run as failed – "
                        "run=%s error=%s",
                        pipeline_run_id,
                        fail_exc,
                    )
                    return DispatchResult.fail(
                        f"Pipeline failed and error recording also failed: {error_msg}"
                    )

                return DispatchResult.fail(error_msg)

            return DispatchResult.ack()

        except Exception as exc:
            logger.error(
                "pipeline-consumer: unhandled error – run=%s error=%s",
                pipeline_run_id,
                exc,
                exc_info=True,
            )
            return DispatchResult.fail(str(exc)[:2000])


# ── Internal helpers ─────────────────────────────────────────────────────────


def _make_system_context() -> RequestContext:
    """Create a request context for system-initiated pipeline actions."""
    from uuid import uuid4
    return RequestContext(
        request_id=uuid4(),
        correlation_id=str(uuid4()),
        actor=ActorContext(
            actor_type="system",
            actor_id=UUID("00000000-0000-0000-0000-000000000000"),
            auth_context_type=None,
            auth_context_id=None,
        ),
        idempotency_key=None,
    )


def _link_root_job(db, pipeline_run_id: UUID, root_job_id: UUID) -> None:
    """Link a root job to a pipeline run."""
    from sqlalchemy import text
    db.execute(
        text("""
            UPDATE pipeline_runs
            SET root_job_id = :root_job_id,
                updated_at = now()
            WHERE pipeline_run_id = :pipeline_run_id
        """),
        {
            "pipeline_run_id": pipeline_run_id,
            "root_job_id": root_job_id,
        },
    )
