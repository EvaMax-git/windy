"""P4-09 Memory Extract Pipeline — orchestrate LLM extraction → candidate submission.

This is the core pipeline that:
1. Validates input (source message/event exists).
2. Calls the LLM via Gateway to extract memory candidates.
3. Submits candidates via ``submit_candidate()`` (idempotent via ``candidate_hash``).
4. Stores evidence spans in candidate ``metadata_json`` for P4-05 memory activation.

The pipeline can be triggered:
* **Manually** via ``POST /api/v4/memory/extract`` (API endpoint).
* **Automatically** via Worker consuming ``message.created`` outbox events.

Architecture::

    Source (message/raw_event)
        │
        ▼
    [1] Input validation — resolve source text + project
        │
        ▼
    [2] LLM call via Gateway — prompt with source text
        │
        ▼
    [3] Parse response — extract candidates + evidence_spans
        │
        ▼
    [4] Submit candidates — idempotent via candidate_hash UNIQUE
        │
        ▼
    [5] Store evidence spans in candidate metadata_json (P4-05 creates memory_sources)
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from mneme.api.context import RequestContext
from mneme.db.memory_candidates import submit_candidate
from mneme.db.messages import get_message
from mneme.db.raw_events import get_raw_event
from mneme.gateway.call import Gateway, GatewayError
from mneme.memory.llm_extract import (
    ExtractResult,
    build_extract_prompt,
    parse_extract_response,
)
from mneme.schemas.memory_candidates import (
    CandidateSourceType,
    MemoryCandidateCreate,
    MemoryCandidateRead,
)

logger = logging.getLogger(__name__)


# ── Error types ─────────────────────────────────────────────────────────────


class ExtractPipelineError(Exception):
    """Classified error from the Memory Extract Pipeline.

    Attributes
    ----------
    code : str
        Machine-readable error code for client-side branching.
        Known codes: ``source_not_found``, ``empty_source``,
        ``gateway_timeout``, ``gateway_rate_limited``, ``llm_parse_error``,
        ``project_not_found``, ``gateway_error``.
    message : str
        Human-readable description.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── Data types ──────────────────────────────────────────────────────────────


@dataclass
class ExtractOutput:
    """Result of a single pipeline execution."""

    pipeline_run_id: UUID | None = None
    """Pipeline run tracking ID (None if not tracked)."""

    candidates_submitted: int = 0
    """Number of new candidates created (excludes dedup hits)."""

    candidates_deduped: int = 0
    """Number of candidates that were deduplicated (already existed)."""

    sources_created: int = 0
    """Number of memory_sources rows created."""

    llm_candidates_found: int = 0
    """Number of candidates the LLM returned."""

    error: str | None = None
    """Error message if the pipeline failed."""

    candidates: list[dict[str, Any]] = field(default_factory=list)
    """Submitted candidate summaries (for API response)."""


# ── Source resolution ───────────────────────────────────────────────────────


def _resolve_source(
    db,
    *,
    source_type: str,
    source_id: UUID,
) -> tuple[str, UUID | None, str | None]:
    """Resolve source text and project_id from a source reference.

    Returns
    -------
    (source_text, project_id, title_hint)
        Source text for LLM extraction, owning project UUID, and optional title hint.
    """
    source_text = ""
    project_id = None
    title_hint = None

    if source_type == "message":
        msg = get_message(db, source_id)
        if msg is None:
            raise ValueError(f"Message {source_id} not found")
        source_text = msg.content_text

        # Resolve project_id from conversation (message has no direct project_id)
        from mneme.db.conversations import get_conversation
        conv = get_conversation(db, msg.conversation_id)
        if conv:
            project_id = conv.project_id

        title_hint = f"From message by {msg.sender_label or msg.role_code}"

    elif source_type == "raw_event":
        event = get_raw_event(db, source_id)
        if event is None:
            raise ValueError(f"Raw event {source_id} not found")
        source_text = event.text_preview or ""
        if not source_text and event.payload_json:
            source_text = json.dumps(event.payload_json)[:5000]
        project_id = event.project_id
        title_hint = f"From event {event.raw_event_type}"

    else:
        raise ValueError(f"Unsupported source_type: {source_type}")

    if not source_text.strip():
        raise ValueError(f"Source {source_type}/{source_id} has no extractable text")

    return source_text, project_id, title_hint


# ── Pipeline main ───────────────────────────────────────────────────────────


def _make_pipeline_idempotency_key(source_type: str, source_id: UUID) -> str:
    """Generate a stable idempotency key for the pipeline run."""
    raw = f"memextract:{source_type}:{source_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def run_extract_pipeline(
    db,
    context: RequestContext,
    *,
    source_type: str,
    source_id: UUID,
    project_id: UUID | None = None,
    gateway: Gateway | None = None,
    conversation_context: str = "",
) -> ExtractOutput:
    """Execute the Memory Extract Pipeline for a single source.

    Parameters
    ----------
    db : Session
        Active database session (caller manages transaction).
    context : RequestContext
        The API request context for audit trail.
    source_type : str
        ``"message"`` or ``"raw_event"``.
    source_id : UUID
        The source record primary key.
    project_id : UUID or None
        Override project (resolved from source if None).
    gateway : Gateway or None
        Pre-configured Gateway instance (created if None).
    conversation_context : str
        Optional preceding conversation text for LLM context.

    Returns
    -------
    ExtractOutput
        Summary of pipeline execution.
    """
    output = ExtractOutput()

    # ── Step 1: Resolve source ──────────────────────────────────────────
    try:
        source_text, resolved_project_id, title_hint = _resolve_source(
            db, source_type=source_type, source_id=source_id,
        )
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise ExtractPipelineError("source_not_found", msg) from exc
        if "no extractable text" in msg.lower():
            raise ExtractPipelineError("empty_source", msg) from exc
        if "unsupported" in msg.lower():
            raise ExtractPipelineError("invalid_request", msg) from exc
        raise ExtractPipelineError("source_not_found", msg) from exc

    effective_project_id = project_id or resolved_project_id
    if effective_project_id is None:
        raise ExtractPipelineError(
            "project_not_found",
            "Cannot determine project_id for source",
        )

    # ── Step 2: Call LLM via Gateway ────────────────────────────────────
    gw = gateway or Gateway()
    messages = build_extract_prompt(
        source_text=source_text,
        source_type=source_type,
        conversation_context=conversation_context,
    )

    try:
        gw_result = gw.call(
            capability_code="chat.completion",
            params={
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 2048,
            },
            project_id=effective_project_id,
            sensitivity="private",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            idempotency_key=_make_pipeline_idempotency_key(source_type, source_id),
            call_type="memory_extract",
        )
    except GatewayError as exc:
        logger.error("Gateway call failed for extract: %s", exc)
        from mneme.gateway.call import ProviderTimeoutError
        if isinstance(exc, ProviderTimeoutError):
            raise ExtractPipelineError(
                "gateway_timeout", f"Gateway timeout: {exc}"
            ) from exc
        if "429" in str(exc) or "rate" in str(exc).lower():
            raise ExtractPipelineError(
                "gateway_rate_limited", f"Gateway rate limited: {exc}"
            ) from exc
        raise ExtractPipelineError(
            "gateway_error", f"Gateway error: {exc.code} - {exc}"
        ) from exc

    raw_response = gw_result.get("data", {}).get("choices", [{}])[0].get(
        "message", {}
    ).get("content", "")

    if not raw_response:
        # Try alternative response format
        raw_response = str(gw_result.get("data", ""))

    # ── Step 3: Parse LLM response ──────────────────────────────────────
    extract_result = parse_extract_response(raw_response, source_text=source_text)
    output.llm_candidates_found = len(extract_result.candidates)

    if extract_result.parse_error and not extract_result.candidates:
        raise ExtractPipelineError(
            "llm_parse_error",
            f"LLM parse error: {extract_result.parse_error}",
        )

    # ── Step 4: Submit candidates (idempotent) ──────────────────────────
    for candidate in extract_result.candidates:
        submitted = _submit_one_candidate(
            db,
            context,
            candidate=candidate,
            source_type=source_type,
            source_id=source_id,
            project_id=effective_project_id,
        )
        output.candidates.append(submitted)

        if submitted.get("is_new"):
            output.candidates_submitted += 1

            # ── Step 5: Store evidence spans in candidate metadata ───────
            # memory_sources require memory_id/memory_version FK, so spans
            # are stored in candidate metadata_json for P4-05 activation.
            if submitted.get("candidate_id") and candidate.evidence_spans:
                _store_evidence_in_metadata(
                    db,
                    candidate_id=submitted["candidate_id"],
                    evidence_spans=candidate.evidence_spans,
                )
                output.sources_created += len(candidate.evidence_spans)
        else:
            output.candidates_deduped += 1

    return output


# ── Helpers ─────────────────────────────────────────────────────────────────


def _submit_one_candidate(
    db,
    context: RequestContext,
    *,
    candidate,
    source_type: str,
    source_id: UUID,
    project_id: UUID,
) -> dict[str, Any]:
    """Submit a single extracted candidate with dedup handling.

    Returns a dict with:
    - candidate_id: UUID
    - is_new: bool (True if new, False if dedup)
    - title: str
    - candidate_hash: str
    """
    from mneme.db.memory_candidates import (
        compute_candidate_hash,
        get_candidate_by_hash,
    )

    candidate_hash = compute_candidate_hash(
        title=candidate.title,
        candidate_text=candidate.candidate_text,
        source_type=source_type,
        source_id=source_id,
    )

    existing = get_candidate_by_hash(
        db, project_id=project_id, candidate_hash=candidate_hash,
    )

    if existing is not None:
        return {
            "candidate_id": existing.candidate_id,
            "is_new": False,
            "title": existing.title,
            "candidate_hash": candidate_hash,
            "candidate_status": existing.candidate_status,
        }

    # Build metadata_json with evidence_spans stored for later use by P4-05
    metadata = {}
    spans_data = [
        {
            "span_start": s.span_start,
            "span_end": s.span_end,
            "text_fragment": s.text_fragment,
            "confidence": s.confidence,
        }
        for s in candidate.evidence_spans
    ]
    if spans_data:
        metadata["evidence_spans"] = spans_data

    payload = MemoryCandidateCreate(
        project_id=project_id,
        source_type=CandidateSourceType(source_type),
        source_id=source_id,
        title=candidate.title or "",
        candidate_text=candidate.candidate_text,
        confidence_score=candidate.confidence_score,
        review_required=True,
        metadata_json=metadata,
    )

    result = submit_candidate(db, context, payload=payload)
    return {
        "candidate_id": result.candidate_id,
        "is_new": True,
        "title": result.title,
        "candidate_hash": result.candidate_hash,
        "candidate_status": result.candidate_status,
    }


def _store_evidence_in_metadata(
    db,
    *,
    candidate_id: UUID,
    evidence_spans: list,
) -> None:
    """Store evidence spans in the candidate's ``metadata_json`` field.

    ``memory_sources`` requires ``memory_id``/``memory_version`` FK to
    ``memory_versions``, so spans are stored here for P4-05 to use during
    ``activate_memory()``.
    """
    from sqlalchemy import text
    import json

    spans_data = [
        {
            "span_start": s.span_start,
            "span_end": s.span_end,
            "text_fragment": s.text_fragment,
            "confidence": s.confidence,
        }
        for s in evidence_spans
    ]

    db.execute(
        text("""
            UPDATE memory_candidates
            SET metadata_json = COALESCE(metadata_json, '{}'::jsonb)
                || CAST(:spans_json AS jsonb),
                updated_at = now()
            WHERE candidate_id = :candidate_id
        """),
        {
            "candidate_id": candidate_id,
            "spans_json": json.dumps({"evidence_spans": spans_data}),
        },
    )


# ── MemoryExtractPipeline (class-based wrapper for worker usage) ────────────


class MemoryExtractPipeline:
    """Class-based wrapper for the memory extract pipeline.

    Provides a stable interface for worker consumers to call.
    """

    def __init__(self, gateway: Gateway | None = None) -> None:
        self._gateway = gateway or Gateway()

    def extract_from_source(
        self,
        db,
        context: RequestContext,
        *,
        source_type: str,
        source_id: UUID,
        project_id: UUID | None = None,
        conversation_context: str = "",
    ) -> ExtractOutput:
        """Run extraction pipeline for a single source."""
        return run_extract_pipeline(
            db,
            context,
            source_type=source_type,
            source_id=source_id,
            project_id=project_id,
            gateway=self._gateway,
            conversation_context=conversation_context,
        )
