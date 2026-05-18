"""P4-10 Memory Auto-Extract Sweeper — periodically scans new conversations and triggers Memory Extract Pipeline.

This sweeper is the **periodic scanner** counterpart to the event-driven
``MemoryEventConsumer``.  While the consumer reacts to ``message.created``
outbox events in real time, the sweeper provides a configurable
batch/orchestrated path that can:

* **Instant window** (``window_seconds=0``): scan frequently and extract from
  each unprocessed message immediately — a fallback for missed events.
* **Batch window** (``window_seconds>0``): accumulate messages from the same
  conversation within a time window, then extract them together with full
  conversation context for better LLM recall quality.

Architecture
------------
1. Runs as a periodic sweeper inside the worker main loop (similar to
   ``RetrySweeper`` / ``DispatchingRecoverySweeper``).
2. Each cycle queries for conversations that have messages **not yet**
   represented in ``memory_candidates`` (i.e. never extracted).
3. For each such conversation, finds the unextracted messages.
4. In **instant mode** each message triggers its own pipeline run.
5. In **batch mode** all unextracted messages from the conversation are
   passed together with conversation_context for richer extraction.
6. Pipeline dedup (``candidate_hash`` UNIQUE) makes re-processing safe.

Configuration
-------------
All tunables from ``mneme.config.Settings``:

* ``worker_memory_auto_extract_enabled`` (default ``True``)
* ``worker_memory_auto_extract_interval_seconds`` (default 15)
* ``worker_memory_auto_extract_window_seconds`` (default 0 = instant)
* ``worker_memory_auto_extract_batch_size`` (default 20)
* ``worker_memory_auto_extract_max_messages_per_conv`` (default 50)
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from mneme.api.context import ActorContext, RequestContext
from mneme.config import get_settings
from mneme.db.base import SessionLocal
from mneme.db.pipelines import get_pipeline_def_by_code
from mneme.memory.extract_pipeline import MemoryExtractPipeline, ExtractPipelineError

logger = logging.getLogger(__name__)

_MEMORY_EXTRACT_PIPELINE_CODE = "memory_extract"

# Sentinel UUID for system actor
_SYSTEM_USER_ID = UUID("00000000-0000-0000-0000-000000000000")


# -- Data types --------------------------------------------------------------

@dataclass
class UnprocessedConversation:
    """A conversation with unextracted messages found by the sweeper."""

    conversation_id: UUID
    project_id: UUID
    message_ids: list[UUID] = field(default_factory=list)
    first_message_at: datetime | None = None
    last_message_at: datetime | None = None
    total_unprocessed: int = 0


@dataclass
class SweepResult:
    """Result of a single sweep cycle."""

    conversations_scanned: int = 0
    conversations_processed: int = 0
    messages_extracted: int = 0
    candidates_submitted: int = 0
    candidates_deduped: int = 0
    errors: int = 0
    skipped: int = 0


# -- SQL Queries -------------------------------------------------------------

# Find conversations with unextracted messages.
# A message is "unprocessed" when no memory_candidates row references it
# (source_type='message', source_id=message.message_id).
_FIND_UNPROCESSED_CONVERSATIONS = text("""
    SELECT
        m.conversation_id,
        m.message_id,
        m.message_time
    FROM messages m
    WHERE NOT EXISTS (
        SELECT 1 FROM memory_candidates mc
        WHERE mc.source_type = 'message'
          AND mc.source_id = m.message_id
    )
    ORDER BY m.conversation_id, m.message_time ASC
    LIMIT :limit
""")


# Count total unprocessed messages (for monitoring).
_COUNT_UNPROCESSED_MESSAGES = text("""
    SELECT count(*)
    FROM messages m
    WHERE NOT EXISTS (
        SELECT 1 FROM memory_candidates mc
        WHERE mc.source_type = 'message'
          AND mc.source_id = m.message_id
    )
""")


# Find unprocessed messages for a specific conversation.
_FIND_UNPROCESSED_FOR_CONV = text("""
    SELECT
        m.message_id,
        m.content_text,
        m.message_time,
        m.role_code,
        m.sender_label
    FROM messages m
    WHERE m.conversation_id = :conversation_id
      AND NOT EXISTS (
        SELECT 1 FROM memory_candidates mc
        WHERE mc.source_type = 'message'
          AND mc.source_id = m.message_id
    )
    ORDER BY m.message_time ASC
    LIMIT :limit
""")


# Get preceding messages for conversation context (last N messages before the
# first unprocessed one, up to a token budget).
_FIND_CONVERSATION_CONTEXT = text("""
    SELECT
        m.role_code,
        m.sender_label,
        m.content_text
    FROM messages m
    WHERE m.conversation_id = :conversation_id
      AND m.message_time < :before_time
    ORDER BY m.message_time DESC
    LIMIT :context_limit
""")


# Get conversation project_id.
_GET_CONV_PROJECT = text("""
    SELECT project_id FROM conversations
    WHERE conversation_id = :conversation_id
""")


# -- Helpers -----------------------------------------------------------------

def _make_system_context() -> RequestContext:
    """Build a minimal RequestContext for system-initiated actions."""
    return RequestContext(
        request_id=uuid4(),
        correlation_id=uuid4(),
        actor=ActorContext(
            actor_type="system",
            actor_id=_SYSTEM_USER_ID,
        ),
        idempotency_key=None,
    )


def _build_context_for_extract(
    event_id: UUID, source_type: str, source_id: UUID,
) -> RequestContext:
    """Build a RequestContext with a deterministic idempotency key."""
    raw = f"autoextract:{source_type}:{source_id}"
    ikey = hashlib.sha256(raw.encode()).hexdigest()
    return RequestContext(
        request_id=uuid4(),
        correlation_id=event_id,
        actor=ActorContext(
            actor_type="system",
            actor_id=_SYSTEM_USER_ID,
        ),
        idempotency_key=ikey,
    )


# -- MemoryAutoExtractSweeper ------------------------------------------------

class MemoryAutoExtractSweeper:
    """Periodic sweeper that finds unextracted messages and runs extract pipeline.

    Two operating modes, controlled by ``window_seconds``:

    **Instant mode** (``window_seconds=0``)
        Each unprocessed message is extracted individually as soon as it's found.

    **Batch mode** (``window_seconds>0``)
        Messages from the same conversation that arrived within
        ``window_seconds`` of the conversation's first unprocessed message are
        batched together.  The pipeline receives the conversation context
        (preceding messages) for richer extraction.

    Usage::

        sweeper = MemoryAutoExtractSweeper(
            window_seconds=60,
            batch_size=20,
            max_messages_per_conv=50,
        )
        result = sweeper.sweep()
        # result.conversations_processed, .messages_extracted, .candidates_submitted
    """

    def __init__(
        self,
        *,
        window_seconds: int = 0,
        batch_size: int = 20,
        max_messages_per_conv: int = 50,
        context_message_limit: int = 10,
    ) -> None:
        self._window_seconds = window_seconds
        self._batch_size = batch_size
        self._max_messages_per_conv = max_messages_per_conv
        self._context_message_limit = context_message_limit
        self._pipeline = MemoryExtractPipeline()

    @property
    def window_seconds(self) -> int:
        return self._window_seconds

    @property
    def is_instant_mode(self) -> bool:
        """Return True when operating in instant (per-message) mode."""
        return self._window_seconds <= 0

    # -- Public API ----------------------------------------------------------

    def sweep(self) -> SweepResult:
        """Execute one full sweep cycle.

        Returns
        -------
        SweepResult
            Summary counters for the sweep cycle.
        """
        result = SweepResult()

        try:
            # Phase 1: Find conversations with unprocessed messages
            convs = self._find_unprocessed_conversations()
            result.conversations_scanned = len(convs)

            if not convs:
                return result

            # Phase 2: Process each conversation
            for conv in convs:
                try:
                    conv_result = self._process_conversation(conv)
                    result.conversations_processed += 1
                    result.messages_extracted += conv_result.messages_extracted
                    result.candidates_submitted += conv_result.candidates_submitted
                    result.candidates_deduped += conv_result.candidates_deduped
                    result.errors += conv_result.errors
                except Exception as exc:
                    logger.error(
                        "auto-extract: failed to process conv=%s: %s",
                        conv.conversation_id, exc,
                        exc_info=True,
                    )
                    result.errors += 1

        except Exception as exc:
            logger.error("auto-extract sweep cycle failed: %s", exc, exc_info=True)
            result.errors += 1

        return result

    # -- Phase 1: Find unprocessed conversations -----------------------------

    def _find_unprocessed_conversations(self) -> list[UnprocessedConversation]:
        """Find conversations that have unextracted messages.

        Returns a deduplicated list grouped by conversation_id.
        """
        with SessionLocal() as db:
            try:
                rows = (
                    db.execute(
                        _FIND_UNPROCESSED_CONVERSATIONS,
                        {"limit": self._batch_size * self._max_messages_per_conv},
                    )
                    .mappings()
                    .all()
                )
            except Exception:
                db.rollback()
                raise

        if not rows:
            return []

        # Group by conversation_id
        conv_map: dict[UUID, UnprocessedConversation] = {}
        for row in rows:
            cid = row["conversation_id"]
            mid = row["message_id"]
            mtime = row["message_time"]

            if cid not in conv_map:
                conv_map[cid] = UnprocessedConversation(
                    conversation_id=cid,
                    project_id=_SYSTEM_USER_ID,  # placeholder, resolved below
                )
            conv = conv_map[cid]

            if len(conv.message_ids) < self._max_messages_per_conv:
                conv.message_ids.append(mid)
            conv.total_unprocessed += 1

            if mtime:
                if conv.first_message_at is None or mtime < conv.first_message_at:
                    conv.first_message_at = mtime
                if conv.last_message_at is None or mtime > conv.last_message_at:
                    conv.last_message_at = mtime

        # Resolve project_ids (can be done in same DB session if we keep it,
        # but we already closed the session; open a new one)
        with SessionLocal() as db:
            try:
                for cid, conv in conv_map.items():
                    row = db.execute(
                        _GET_CONV_PROJECT,
                        {"conversation_id": cid},
                    ).first()
                    if row and row[0]:
                        conv.project_id = row[0]
            except Exception:
                db.rollback()
                raise

        return list(conv_map.values())

    # -- Phase 2: Process one conversation -----------------------------------

    def _process_conversation(
        self, conv: UnprocessedConversation,
    ) -> SweepResult:
        """Process one conversation's unextracted messages.

        In instant mode each message is extracted individually.
        In batch mode all messages are extracted together with conversation context.
        """
        result = SweepResult()

        if not conv.message_ids:
            result.skipped += 1
            return result

        # Build conversation context from preceding messages (batch mode only)
        conversation_context = ""
        if not self.is_instant_mode and conv.first_message_at:
            conversation_context = self._fetch_conversation_context(
                conv.conversation_id, conv.first_message_at,
            )

        if self.is_instant_mode:
            # Instant mode: extract each message individually
            for msg_id in conv.message_ids:
                try:
                    msg_result = self._extract_single_message(
                        conv=conv,
                        message_id=msg_id,
                        project_id=conv.project_id,
                        conversation_context=conversation_context,
                    )
                    result.messages_extracted += 1
                    result.candidates_submitted += msg_result.get("candidates_submitted", 0)
                    result.candidates_deduped += msg_result.get("candidates_deduped", 0)
                except Exception as exc:
                    logger.error(
                        "auto-extract: failed on msg=%s conv=%s: %s",
                        msg_id, conv.conversation_id, exc, exc_info=True,
                    )
                    result.errors += 1
        else:
            # Batch mode: extract all messages together (each individually for
            # dedup, but sharing context and processed in sequence)
            for msg_id in conv.message_ids:
                try:
                    msg_result = self._extract_single_message(
                        conv=conv,
                        message_id=msg_id,
                        project_id=conv.project_id,
                        conversation_context=conversation_context,
                    )
                    result.messages_extracted += 1
                    result.candidates_submitted += msg_result.get("candidates_submitted", 0)
                    result.candidates_deduped += msg_result.get("candidates_deduped", 0)
                except Exception as exc:
                    logger.error(
                        "auto-extract: batch failed on msg=%s conv=%s: %s",
                        msg_id, conv.conversation_id, exc, exc_info=True,
                    )
                    result.errors += 1

        return result

    # -- Extract helpers -----------------------------------------------------

    def _extract_single_message(
        self,
        *,
        conv: UnprocessedConversation,
        message_id: UUID,
        project_id: UUID,
        conversation_context: str,
    ) -> dict[str, int]:
        """Extract from a single message with optional conversation context."""
        event_id = uuid4()
        context = _build_context_for_extract(event_id, "message", message_id)

        db = SessionLocal()
        try:
            output = self._pipeline.extract_from_source(
                db,
                context,
                source_type="message",
                source_id=message_id,
                project_id=project_id,
                conversation_context=conversation_context,
            )
            db.commit()

            if output.error:
                logger.warning(
                    "auto-extract: msg=%s completed with error: %s",
                    message_id, output.error,
                )

            return {
                "candidates_submitted": output.candidates_submitted,
                "candidates_deduped": output.candidates_deduped,
                "llm_candidates_found": output.llm_candidates_found,
            }
        except ExtractPipelineError as exc:
            db.rollback()
            logger.info(
                "auto-extract: msg=%s pipeline error (code=%s): %s",
                message_id, exc.code, exc.message,
            )
            return {"candidates_submitted": 0, "candidates_deduped": 0}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # -- Conversation context ------------------------------------------------

    def _fetch_conversation_context(
        self, conversation_id: UUID, before_time: datetime,
    ) -> str:
        """Fetch recent messages before the unprocessed window as LLM context.

        Returns a formatted string suitable for the extract pipeline's
        ``conversation_context`` parameter.
        """
        with SessionLocal() as db:
            try:
                rows = (
                    db.execute(
                        _FIND_CONVERSATION_CONTEXT,
                        {
                            "conversation_id": conversation_id,
                            "before_time": before_time,
                            "context_limit": self._context_message_limit,
                        },
                    )
                    .mappings()
                    .all()
                )
            except Exception:
                db.rollback()
                return ""

        if not rows:
            return ""

        # Rows come in DESC order (most recent first), reverse to chronological
        rows = list(reversed(rows))
        lines = []
        for r in rows:
            role = r.get("role_code") or "unknown"
            sender = r.get("sender_label") or ""
            label = f"{role}" + (f" ({sender})" if sender else "")
            content = (r.get("content_text") or "")[:500]
            lines.append(f"[{label}]: {content}")

        return "\n".join(lines)


# -- Stats helper ------------------------------------------------------------

def get_unprocessed_stats() -> dict[str, int]:
    """Return the total number of messages that have not been extracted yet.

    Useful for monitoring / health endpoints.
    """
    with SessionLocal() as db:
        try:
            count = db.execute(_COUNT_UNPROCESSED_MESSAGES).scalar_one()
            return {"unprocessed_messages": int(count)}
        except Exception as exc:
            logger.error("auto-extract: stats query failed: %s", exc)
            return {"unprocessed_messages": -1}


# -- Convenience Factory -----------------------------------------------------

def create_memory_auto_extract_sweeper() -> MemoryAutoExtractSweeper:
    """Create a :class:`MemoryAutoExtractSweeper` from application settings."""
    settings = get_settings()
    return MemoryAutoExtractSweeper(
        window_seconds=settings.worker_memory_auto_extract_window_seconds,
        batch_size=settings.worker_memory_auto_extract_batch_size,
        max_messages_per_conv=settings.worker_memory_auto_extract_max_messages_per_conv,
    )
