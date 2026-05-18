"""P4-09 Memory Event Consumer — handles ``message.created`` / ``raw_event.created`` outbox events.

When a new message or raw event is written, this consumer automatically triggers
the Memory Extract Pipeline to extract candidate memories.

Architecture
------------
1. Receives ``message.created`` or ``raw_event.created`` event from the dispatcher.
2. Creates a ``pipeline_runs`` entry for tracking (if pipeline def exists).
3. Creates a root ``job`` for the extraction.
4. Calls ``MemoryExtractPipeline.extract_from_source()``.
5. Updates pipeline run and job status on success/failure.

The consumer is **idempotent**: re-delivery of the same event is safe because
``candidate_hash`` UNIQUE constraint prevents duplicate candidate creation.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from mneme.api.context import ActorContext, RequestContext
from mneme.db.base import SessionLocal
from mneme.db.conversations import get_conversation
from mneme.db.jobs import (
    add_job_log,
    create_job,
    update_job_completed,
    update_job_running,
)
from mneme.db.messages import get_message
from mneme.db.pipelines import (
    advance_run_status,
    create_pipeline_run,
    get_pipeline_def_by_code,
)
from mneme.memory.extract_pipeline import MemoryExtractPipeline
from mneme.worker.dispatcher import Consumer, DispatchResult, DispatchOutcome

logger = logging.getLogger(__name__)

_HANDLED_EVENT_TYPES = frozenset({"message.created", "raw_event.created"})
_MEMORY_EXTRACT_PIPELINE_CODE = "memory_extract"

# Sentinel UUID for system actor
_SYSTEM_USER_ID = UUID("00000000-0000-0000-0000-000000000000")


def _make_system_context(event_id: UUID, ikey: str) -> RequestContext:
    """Build a minimal RequestContext for the system actor."""
    return RequestContext(
        request_id=uuid4(),
        correlation_id=event_id,
        actor=ActorContext(
            actor_type="system",
            actor_id=_SYSTEM_USER_ID,
        ),
        idempotency_key=ikey,
    )


class MemoryEventConsumer(Consumer):
    """Outbox consumer that triggers Memory Extract Pipeline on new messages/events.

    Registered in the worker's Dispatcher, this consumer:
    1. Resolves the source message/event.
    2. Creates pipeline run + job for tracking.
    3. Executes the Memory Extract Pipeline.
    4. Updates tracking records on success/failure.
    """

    @property
    def name(self) -> str:
        return "memory-consumer"

    def can_handle(self, event_type: str) -> bool:
        """Return ``True`` for message.created and raw_event.created events."""
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
        """Process a message.created or raw_event.created event.

        Steps:
        1. Determine source_type and source_id from event.
        2. Resolve project_id from the source record.
        3. Create pipeline_run + job for tracking.
        4. Execute Memory Extract Pipeline.
        5. Update tracking on success/failure.
        """
        source_type = _event_type_to_source_type(event_type)
        source_id = aggregate_id

        logger.info(
            "memory-consumer: processing %s for %s/%s (event=%s)",
            event_type, source_type, source_id, event_id,
        )

        # Idempotency key for this dispatch
        ikey = f"memconsume:{event_type}:{source_id}:{event_id}"
        context = _make_system_context(event_id, ikey)

        try:
            output = self._run_with_tracking(
                context=context,
                source_type=source_type,
                source_id=source_id,
                event_id=event_id,
                payload=payload,
            )

            if output.error:
                logger.warning(
                    "memory-consumer: extract completed with error for %s/%s: %s",
                    source_type, source_id, output.error,
                )
                return DispatchResult.fail(output.error)

            logger.info(
                "memory-consumer: extract done — %d submitted, %d deduped, %d sources",
                output.candidates_submitted,
                output.candidates_deduped,
                output.sources_created,
            )
            return DispatchResult.ack()

        except Exception as exc:
            logger.exception(
                "memory-consumer: failed for %s/%s", source_type, source_id,
            )
            return DispatchResult.fail(str(exc))

    # ── Internal helpers ──────────────────────────────────────────────────

    def _run_with_tracking(
        self,
        *,
        context: RequestContext,
        source_type: str,
        source_id: UUID,
        event_id: UUID,
        payload: dict[str, Any],
    ):
        """Execute pipeline with pipeline_run + job tracking."""
        # Resolve project_id from source
        project_id = _resolve_project(source_type, source_id, payload)

        pipeline = MemoryExtractPipeline()

        # ── Phase 1: Create tracking records ─────────────────────────
        pipeline_run_id = None
        root_job_id = None

        db = SessionLocal()
        try:
            pipeline_def = get_pipeline_def_by_code(
                db, _MEMORY_EXTRACT_PIPELINE_CODE,
            )
            if pipeline_def is not None:
                pipeline_def_id = (
                    pipeline_def["pipeline_def_id"]
                    if isinstance(pipeline_def, dict)
                    else getattr(pipeline_def, "pipeline_def_id", None)
                )

                if pipeline_def_id:
                    run_ctx = _make_system_context(
                        event_id,
                        f"memextract:run:{source_type}:{source_id}",
                    )
                    pipeline_run_id = create_pipeline_run(
                        db,
                        pipeline_def_id=pipeline_def_id,
                        trigger_type="event",
                        input_json={
                            "source_type": source_type,
                            "source_id": str(source_id),
                            "event_type": "message.created",
                            "event_id": str(event_id),
                        },
                        target_type=source_type,
                        target_id=source_id,
                    )
                    advance_run_status(
                        db,
                        run_ctx,
                        pipeline_run_id=pipeline_run_id,
                        new_status="running",
                        expected_status="pending",
                    )

                    # Create root job
                    job_key = f"memextract:{source_type}:{source_id}"
                    root_job = create_job(
                        job_type="memory_extract",
                        job_key=job_key,
                        input_payload={
                            "source_type": source_type,
                            "source_id": str(source_id),
                            "pipeline_run_id": str(pipeline_run_id),
                        },
                        max_retries=2,
                        timeout_seconds=600,
                        actor_type="system",
                    )
                    root_job_id = UUID(root_job["job_id"])
                    update_job_running(root_job_id)
                    add_job_log(
                        root_job_id,
                        step="start",
                        message=f"Memory extract for {source_type}/{source_id}",
                        level="info",
                        attempt_no=1,
                    )

            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning(
                "memory-consumer: failed to create tracking records: %s", exc,
            )
            pipeline_run_id = None
            root_job_id = None
        finally:
            db.close()

        # ── Phase 2: Execute pipeline ────────────────────────────────
        db2 = SessionLocal()
        try:
            output = pipeline.extract_from_source(
                db2,
                context,
                source_type=source_type,
                source_id=source_id,
                project_id=project_id,
            )

            if output.error:
                _mark_failed(pipeline_run_id, root_job_id, output.error)
            else:
                _mark_succeeded(pipeline_run_id, root_job_id, output)

            db2.commit()
        except Exception as exc:
            db2.rollback()
            _mark_failed(pipeline_run_id, root_job_id, str(exc))
            raise
        finally:
            db2.close()

        return output


# ── Module-level helpers ────────────────────────────────────────────────────


def _event_type_to_source_type(event_type: str) -> str:
    """Map outbox event_type to source_type for memory_candidates."""
    mapping = {
        "message.created": "message",
        "raw_event.created": "raw_event",
    }
    return mapping.get(event_type, "message")


def _resolve_project(
    source_type: str, source_id: UUID, payload: dict[str, Any],
) -> UUID | None:
    """Resolve project_id from source record or event payload."""
    # Try payload first
    pid = payload.get("project_id")
    if pid:
        try:
            return UUID(pid) if isinstance(pid, str) else pid
        except (ValueError, AttributeError):
            pass

    # Look up the source record
    db = SessionLocal()
    try:
        if source_type == "message":
            msg = get_message(db, source_id)
            if msg:
                # Message has no project_id; resolve via conversation
                conv = get_conversation(db, msg.conversation_id)
                if conv:
                    return conv.project_id
        elif source_type == "raw_event":
            from mneme.db.raw_events import get_raw_event
            event = get_raw_event(db, source_id)
            if event:
                return event.project_id
    except Exception:
        pass
    finally:
        db.close()

    return None


def _mark_succeeded(
    pipeline_run_id: UUID | None,
    root_job_id: UUID | None,
    output,
) -> None:
    """Update pipeline run and job to succeeded."""
    result_data = {
        "candidates_submitted": output.candidates_submitted,
        "candidates_deduped": output.candidates_deduped,
        "sources_created": output.sources_created,
        "llm_candidates_found": output.llm_candidates_found,
    }

    if root_job_id:
        try:
            update_job_completed(
                root_job_id,
                success=True,
                output=result_data,
            )
        except Exception as exc:
            logger.warning("Failed to mark job %s as succeeded: %s", root_job_id, exc)

    if pipeline_run_id:
        try:
            db = SessionLocal()
            ctx = _make_system_context(
                uuid4(), f"memextract:succeed:{pipeline_run_id}",
            )
            advance_run_status(
                db,
                ctx,
                pipeline_run_id=pipeline_run_id,
                new_status="succeeded",
                expected_status="running",
                output_json=result_data,
            )
            db.commit()
        except Exception as exc:
            logger.warning(
                "Failed to mark pipeline run %s as succeeded: %s",
                pipeline_run_id, exc,
            )


def _mark_failed(
    pipeline_run_id: UUID | None,
    root_job_id: UUID | None,
    error: str,
) -> None:
    """Update pipeline run and job to failed."""
    if root_job_id:
        try:
            update_job_completed(
                root_job_id,
                success=False,
                error_message=error[:500],
            )
        except Exception as exc:
            logger.warning("Failed to mark job %s as failed: %s", root_job_id, exc)

    if pipeline_run_id:
        try:
            db = SessionLocal()
            ctx = _make_system_context(
                uuid4(), f"memextract:fail:{pipeline_run_id}",
            )
            advance_run_status(
                db,
                ctx,
                pipeline_run_id=pipeline_run_id,
                new_status="failed",
                expected_status="running",
                error_json={"error": error[:1000]},
            )
            db.commit()
        except Exception as exc:
            logger.warning(
                "Failed to mark pipeline run %s as failed: %s",
                pipeline_run_id, exc,
            )
