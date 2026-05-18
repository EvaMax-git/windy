"""Dashboard stats endpoint — aggregated counts for the overview page.

Provides a single API call that returns memory counts, candidate counts,
review item counts, and document statistics to power the DashboardPage.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.base import get_db
from mneme.observability.health import check_database, check_redis, check_outbox_pending, DependencyStatus

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/stats")
def dashboard_stats(
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
):
    """Return aggregated counts for the dashboard overview.

    Returns counts across: memory, memory candidates, review items, knowledge
    documents, agents, and audit events — all in one efficient query batch.
    """
    try:
        # ── Aggregate counts ──
        mem_result = db.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'active') AS active_memories,
                COUNT(*) FILTER (WHERE status = 'expired') AS expired_memories,
                COUNT(*) FILTER (WHERE status = 'deleted') AS deleted_memories,
                COUNT(*) AS total_memories
            FROM memories
        """)).mappings().one()

        cand_result = db.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE candidate_status = 'pending') AS pending_candidates,
                COUNT(*) AS total_candidates
            FROM memory_candidates
        """)).mappings().one()

        rev_result = db.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'pending') AS pending_reviews,
                COUNT(*) AS total_reviews
            FROM review_items
        """)).mappings().one()

        doc_result = db.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE document_status = 'active') AS active_documents,
                COUNT(*) AS total_documents
            FROM knowledge_documents
        """)).mappings().one()

        # ── Agent list (full objects for the Agent card section) ──
        agent_rows = db.execute(text("""
            SELECT agent_id, project_id, agent_code, name, description, status,
                   owner_user_id, store_id, sensitivity_ceiling, policy_json,
                   disabled_at, created_at, updated_at
            FROM agents
            ORDER BY created_at DESC
        """)).mappings().all()

        agents_list = [
            {
                "agent_id": str(r["agent_id"]),
                "project_id": str(r["project_id"]) if r["project_id"] else None,
                "agent_code": r["agent_code"],
                "name": r["name"],
                "description": r["description"],
                "status": r["status"],
                "owner_user_id": str(r["owner_user_id"]) if r["owner_user_id"] else None,
                "store_id": str(r["store_id"]) if r["store_id"] else None,
                "sensitivity_ceiling": r["sensitivity_ceiling"],
                "policy_json": r["policy_json"] if r["policy_json"] else {},
                "disabled_at": r["disabled_at"].isoformat() if r["disabled_at"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in agent_rows
        ]

        # ── Sensitivity distribution (from knowledge_documents) ──
        sens_rows = db.execute(text("""
            SELECT sensitivity_level, COUNT(*) AS cnt
            FROM knowledge_documents
            WHERE document_status = 'active'
            GROUP BY sensitivity_level
        """)).mappings().all()
        sensitivity_distribution = {r["sensitivity_level"]: r["cnt"] for r in sens_rows}

        # ── Recent activity (last 20 audit events) ──
        audit_rows = db.execute(text("""
            SELECT audit_id, occurred_at, actor_type, actor_id, auth_context_type, auth_context_id,
                   action, object_type, object_id, project_id, result, reason_code,
                   sensitivity_level, correlation_id, request_id
            FROM audit_events
            ORDER BY occurred_at DESC
            LIMIT 20
        """)).mappings().all()

        recent_activity = [
            {
                "audit_id": str(r["audit_id"]),
                "occurred_at": r["occurred_at"].isoformat() if r["occurred_at"] else None,
                "actor": {
                    "actor_type": r["actor_type"],
                    "actor_id": str(r["actor_id"]) if r["actor_id"] else None,
                    "auth_context_type": r["auth_context_type"],
                    "auth_context_id": str(r["auth_context_id"]) if r["auth_context_id"] else None,
                },
                "action": r["action"],
                "object_type": r["object_type"],
                "object_id": str(r["object_id"]) if r["object_id"] else None,
                "project_id": str(r["project_id"]) if r["project_id"] else None,
                "result": r["result"],
                "reason_code": r["reason_code"],
                "sensitivity_level": r["sensitivity_level"],
                "correlation_id": str(r["correlation_id"]),
                "request_id": str(r["request_id"]),
            }
            for r in audit_rows
        ]

        data = {
            "agents": agents_list,
            "total_memories": mem_result["total_memories"],
            "pending_candidates": cand_result["pending_candidates"],
            "pending_reviews": rev_result["pending_reviews"],
            "total_documents": doc_result["total_documents"],
            "sensitivity_distribution": sensitivity_distribution,
            "recent_activity": recent_activity,
        }

        return envelope(
            data,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )
    except Exception as exc:
        raise ApiError(
            500,
            "internal_error",
            f"Failed to load dashboard stats: {str(exc)}",
        )


@router.get("/health-summary")
def health_summary(
    context: RequestContext = Depends(get_request_context),
):
    """Return a lightweight health summary for the dashboard status bar."""
    db_status = check_database()
    redis_status = check_redis()
    outbox = check_outbox_pending()

    overall = "ok"
    if db_status == DependencyStatus.unavailable:
        overall = "unavailable"
    elif redis_status == DependencyStatus.degraded:
        overall = "degraded"

    data = {
        "status": overall,
        "database": str(db_status),
        "redis": str(redis_status),
        "outbox_pending": outbox,
    }

    return envelope(
        data,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
