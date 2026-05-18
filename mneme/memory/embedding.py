"""P5-02 memory embedding pipeline.

All provider calls go through the Gateway capability ``embedding.create``.
This module only parses the Gateway response and persists vectors onto
``memory_index_entries``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.db.api_call_logs import get_api_call_log
from mneme.db.memory_index_entries import (
    get_index_entry,
    mark_entry_vector_failed,
    update_entry_embedding,
)
from mneme.gateway.call import GatewayError, get_gateway


class EmbeddingError(Exception):
    """Base embedding pipeline error."""


class EmbeddingEntryNotFound(EmbeddingError):
    """The requested memory index entry does not exist."""


class EmbeddingGatewayError(EmbeddingError):
    """Gateway failed to produce an embedding."""


class EmbeddingResponseError(EmbeddingError):
    """Gateway returned a malformed embedding payload."""


@dataclass(frozen=True)
class EmbeddingCallResult:
    embedding: list[float]
    api_call_log_id: UUID | None = None
    embedding_model_id: UUID | None = None
    raw: dict[str, Any] | None = None


def _coerce_uuid(value: Any) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _extract_embedding_vector(payload: Any) -> list[float]:
    """Extract an embedding vector from common provider response shapes."""
    data = payload.get("data") if isinstance(payload, dict) else payload

    vector: Any = None
    if isinstance(data, dict):
        if isinstance(data.get("data"), list) and data["data"]:
            first = data["data"][0]
            if isinstance(first, dict):
                vector = first.get("embedding")
            else:
                vector = first
        elif isinstance(data.get("embedding"), list):
            vector = data["embedding"]
        elif isinstance(data.get("embeddings"), list) and data["embeddings"]:
            vector = data["embeddings"][0]
    elif isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            vector = first.get("embedding")
        else:
            vector = first

    if not isinstance(vector, list) or not vector:
        raise EmbeddingResponseError("embedding response did not include a vector")

    cleaned: list[float] = []
    for value in vector:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise EmbeddingResponseError("embedding vector contains a non-number") from exc
        if not math.isfinite(number):
            raise EmbeddingResponseError("embedding vector contains a non-finite number")
        cleaned.append(number)

    return cleaned


def _model_id_from_gateway_result(result: dict[str, Any]) -> UUID | None:
    api_call_log_id = _coerce_uuid(result.get("api_call_log_id"))
    if api_call_log_id is None:
        return None
    try:
        log = get_api_call_log(api_call_log_id)
    except Exception:
        return None
    if not log:
        return None
    return _coerce_uuid(log.get("provider_model_id"))


def embed_text(
    text: str,
    *,
    project_id: UUID | None = None,
    sensitivity: str = "private",
    context: RequestContext | None = None,
    idempotency_key: str | None = None,
) -> EmbeddingCallResult:
    """Embed text through Gateway and return the parsed vector."""
    cleaned_text = text.strip()
    if not cleaned_text:
        raise EmbeddingResponseError("cannot embed empty text")

    actor = context.actor if context else None
    try:
        result = get_gateway().call(
            capability_code="embedding.create",
            params={"input": cleaned_text},
            project_id=project_id,
            sensitivity=sensitivity,
            actor_type=actor.actor_type if actor else "system",
            actor_id=actor.actor_id if actor else None,
            auth_context_type=actor.auth_context_type if actor else None,
            auth_context_id=actor.auth_context_id if actor else None,
            request_id=context.request_id if context else None,
            correlation_id=context.correlation_id if context else None,
            idempotency_key=idempotency_key or (context.idempotency_key if context else None),
            call_type="embedding",
        )
    except GatewayError as exc:
        raise EmbeddingGatewayError(str(exc)) from exc

    vector = _extract_embedding_vector(result.get("data", result))
    api_call_log_id = _coerce_uuid(result.get("api_call_log_id"))
    return EmbeddingCallResult(
        embedding=vector,
        api_call_log_id=api_call_log_id,
        embedding_model_id=_model_id_from_gateway_result(result),
        raw=result,
    )


def embed_index_entry(
    db: Session,
    *,
    entry_id: UUID,
    context: RequestContext | None = None,
) -> dict:
    """Embed one ``memory_index_entries`` row and persist the vector."""
    entry = get_index_entry(db, entry_id)
    if entry is None:
        raise EmbeddingEntryNotFound(f"memory_index_entry {entry_id} not found")

    project_id = _coerce_uuid(entry.get("project_id"))
    try:
        call_result = embed_text(
            entry["index_text"],
            project_id=project_id,
            context=context,
            idempotency_key=(
                f"memory-index-vector-{entry_id}"
                if context is None or not context.idempotency_key
                else context.idempotency_key
            ),
        )
        updated = update_entry_embedding(
            db,
            entry_id=entry_id,
            embedding=call_result.embedding,
            embedding_model_id=call_result.embedding_model_id,
        )
    except EmbeddingError as exc:
        mark_entry_vector_failed(db, entry_id=entry_id, error=str(exc))
        raise
    except Exception as exc:
        mark_entry_vector_failed(db, entry_id=entry_id, error=str(exc))
        raise EmbeddingGatewayError(str(exc)) from exc

    if updated is None:
        raise EmbeddingEntryNotFound(f"memory_index_entry {entry_id} not found")

    return {
        "entry": updated,
        "embedding_dimensions": len(call_result.embedding),
        "api_call_log_id": str(call_result.api_call_log_id)
        if call_result.api_call_log_id
        else None,
        "embedding_model_id": str(call_result.embedding_model_id)
        if call_result.embedding_model_id
        else None,
    }
