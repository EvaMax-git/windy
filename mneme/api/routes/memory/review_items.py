"""P2-05 审核项 API — 增删查改 + 批准 / 拒绝 / 取消 / 认领。

接口列表
--------
* ``POST   /api/v4/review/items``           – 创建审核项
* ``GET    /api/v4/review/items``           – 分页列表，支持过滤
* ``GET    /api/v4/review/items/{id}``      – 单条审核项详情
* ``POST   /api/v4/review/items/{id}/claim``   – 认领 (pending → in_review)
* ``POST   /api/v4/review/items/{id}/approve`` – 批准 (in_review → approved)
* ``POST   /api/v4/review/items/{id}/reject``  – 拒绝 (in_review → rejected)
* ``POST   /api/v4/review/items/{id}/cancel``  – 取消 (pending/in_review → cancelled)
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from sqlalchemy.exc import IntegrityError

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.base import SessionLocal
from mneme.db.review_items import (
    approve_review_item,
    batch_approve_review_items,
    batch_cancel_review_items,
    batch_claim_review_items,
    batch_reject_review_items,
    cancel_review_item,
    create_review_item,
    get_review_item_by_id,
    get_review_items,
    move_to_in_review,
    reject_review_item,
)
from mneme.db.audit import add_audit_event, add_outbox_event
from mneme.schemas import (
    PageInfo,
    PaginationParams,
    ResponseEnvelope,
)
from mneme.schemas.review_items import (
    ReviewItemApproveRequest,
    ReviewItemBatchRequest,
    ReviewItemCreate,
    ReviewItemFilterParams,
    ReviewItemListResponse,
    ReviewItemRead,
    ReviewItemRejectRequest,
)
from mneme.security.audit import (
    AuditEvent,
    OutboxEvent,
    audit_event_for_action,
    outbox_event_for_action,
)

router = APIRouter(prefix="/review/items", tags=["review"])


# ──────────────────────────────────────────────────────────────────────────────
# POST /review/items
# ──────────────────────────────────────────────────────────────────────────────


@router.post("", response_model=ResponseEnvelope[ReviewItemRead], status_code=201)
def create_review_item_endpoint(
    body: ReviewItemCreate,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """创建新的审核项。

    审核项创建时 ``status = 'pending'``。之后可以
    转为 ``in_review``、``approved``、``rejected``、``cancelled``、
    或 ``expired``。
    """
    idempotency_key = context.idempotency_key or str(uuid4())

    try:
        row = create_review_item(
            project_id=body.project_id,
            review_type=body.review_type.value,
            target_type=body.target_type.value,
            target_id=body.target_id,
            priority=body.priority,
            requester_actor_type=context.actor.actor_type,
            requester_actor_id=context.actor.actor_id,
            due_at=body.due_at,
            expires_at=body.expires_at,
            decision_payload=body.decision_payload,
            correlation_id=context.correlation_id,
            request_id=context.request_id,
            idempotency_key=idempotency_key,
        )
    except IntegrityError:
        raise ApiError(
            409,
            "idempotency_conflict",
            f"review item with idempotency_key '{idempotency_key}' already exists",
        )

    # Write audit event
    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="review.created",
                result="success",
                object_type="review_item",
                object_id=UUID(row["review_item_id"]),
                project_id=body.project_id,
            ),
        )
        # Write outbox event for review.created
        add_outbox_event(
            db,
            context,
            outbox_event_for_action(
                event_type="review.created",
                aggregate_type="review_item",
                aggregate_id=UUID(row["review_item_id"]),
                idempotency_key=f"{idempotency_key}.created",
                payload_json={
                    "review_type": row["review_type"],
                    "target_type": row["target_type"],
                    "target_id": row["target_id"],
                },
            ),
        )
        db.commit()

    item = ReviewItemRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# GET /review/items
# ──────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=ResponseEnvelope[ReviewItemListResponse])
def list_review_items(
    pagination: PaginationParams = Depends(),
    filters: ReviewItemFilterParams = Depends(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出审核项，支持可选过滤。

    Query parameters
    ----------------
    * ``page`` / ``page_size`` – pagination (default 1 / 50, max 200).
    * ``review_type`` – 按审核类型过滤。
    * ``status`` – 按状态过滤。
    * ``target_type`` – 按目标类型过滤。
    * ``created_after`` / ``created_before`` – time-range filter.
    """
    rows, total = get_review_items(
        page=pagination.page,
        page_size=pagination.page_size,
        review_type=filters.review_type.value if filters.review_type else None,
        status=filters.status.value if filters.status else None,
        target_type=filters.target_type.value if filters.target_type else None,
        created_after=filters.created_after,
        created_before=filters.created_before,
    )

    items = [ReviewItemRead(**row) for row in rows]
    total_pages = max(1, math.ceil(total / max(pagination.page_size, 1)))

    page_info = PageInfo(
        page=pagination.page,
        page_size=pagination.page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=pagination.page < total_pages,
        has_previous=pagination.page > 1,
    )

    data = ReviewItemListResponse(items=items, page_info=page_info)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# GET /review/items/{review_item_id}
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/{review_item_id}", response_model=ResponseEnvelope[ReviewItemRead])
def get_review_item_detail(
    review_item_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """通过主键返回单条审核项。"""
    row = get_review_item_by_id(review_item_id)
    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"审核项 'review_item_id' 未找到",
        )

    item = ReviewItemRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /review/items/{review_item_id}/claim
# ──────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{review_item_id}/claim",
    response_model=ResponseEnvelope[ReviewItemRead],
)
def claim_review_item_endpoint(
    review_item_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """认领审核项 (``pending`` → ``in_review``)。

    这是将审核项标记为正在审核的显式状态转换。
    只有 ``pending`` 状态的审核项才能被认领。

    认领时：
    * 审核项状态变更为 ``in_review``。
    * 发布发件箱事件 ``review.claimed``。
    """
    row = get_review_item_by_id(review_item_id)
    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"审核项 'review_item_id' 未找到",
        )

    current_status = row["status"]
    if current_status != "pending":
        raise ApiError(
            409,
            "bad_request",
            f"review_item '{review_item_id}' has status '{current_status}'; "
            "only 'pending' items can be claimed",
        )

    success = move_to_in_review(review_item_id)
    if not success:
        raise ApiError(
            409,
            "bad_request",
            f"review_item '{review_item_id}' could not be claimed "
            "(may have been modified concurrently)",
        )

    row = get_review_item_by_id(review_item_id)

    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="review.claimed",
                result="success",
                object_type="review_item",
                object_id=review_item_id,
                diff_summary={
                    "previous_status": "pending",
                    "new_status": "in_review",
                },
            ),
        )

        outbox_idempotency_key = f"{row['idempotency_key']}.claimed"
        add_outbox_event(
            db,
            context,
            outbox_event_for_action(
                event_type="review.claimed",
                aggregate_type="review_item",
                aggregate_id=review_item_id,
                idempotency_key=outbox_idempotency_key,
                payload_json={
                    "review_type": row["review_type"],
                    "target_type": row["target_type"],
                    "target_id": row["target_id"],
                },
            ),
        )
        db.commit()

    item = ReviewItemRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /review/items/{review_item_id}/approve
# ──────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{review_item_id}/approve",
    response_model=ResponseEnvelope[ReviewItemRead],
)
def approve_review_item_endpoint(
    review_item_id: UUID,
    body: ReviewItemApproveRequest = ReviewItemApproveRequest(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """批准审核项 (``in_review`` → ``approved``)。

    批准时：
    * 审核项状态变更为 ``approved``，``decision='approved'``。
    * 发布发件箱事件 ``review.approved``。
    * 如果 ``review_type = 'dlq_replay'``，则触发死信重放。
    """
    # ── Fetch current state ────────────────────────────────────────────────
    row = get_review_item_by_id(review_item_id)
    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"审核项 'review_item_id' 未找到",
        )

    current_status = row["status"]
    if current_status != "in_review":
        raise ApiError(
            409,
            "bad_request",
            f"review_item '{review_item_id}' has status '{current_status}'; "
            "only 'in_review' items can be approved",
        )

    # Use the request's actor as reviewer (if authenticated), else system
    reviewer_id = context.actor.actor_id

    # ── Execute state transition ───────────────────────────────────────────
    success = approve_review_item(
        review_item_id=review_item_id,
        reviewer_id=reviewer_id,
        reason=body.reason,
    )

    if not success:
        raise ApiError(
            409,
            "bad_request",
            f"review_item '{review_item_id}' could not be approved "
            "(may have been modified concurrently)",
        )

    # ── Reload after update ────────────────────────────────────────────────
    row = get_review_item_by_id(review_item_id)

    # ── Write audit + outbox ───────────────────────────────────────────────
    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="review.approved",
                result="success",
                object_type="review_item",
                object_id=review_item_id,
                diff_summary={
                    "previous_status": "in_review",
                    "new_status": "approved",
                    "decision": "approved",
                    "reviewer_id": str(reviewer_id) if reviewer_id else None,
                },
            ),
        )

        # Build a unique idempotency key for the outbox event
        outbox_idempotency_key = (
            f"{row['idempotency_key']}.approved"
        )

        add_outbox_event(
            db,
            context,
            outbox_event_for_action(
                event_type="review.approved",
                aggregate_type="review_item",
                aggregate_id=review_item_id,
                idempotency_key=outbox_idempotency_key,
                payload_json={
                    "review_type": row["review_type"],
                    "target_type": row["target_type"],
                    "target_id": row["target_id"],
                },
            ),
        )
        db.commit()

    # ── P2-04: Trigger DLQ replay for dlq_replay reviews ──────────────────
    if row["review_type"] == "dlq_replay":
        _execute_dlq_replay(
            dead_letter_id=UUID(row["target_id"]),
            review_item_id=review_item_id,
            context=context,
        )

    # ── P2-16: Execute restore for restore_confirm reviews ──────────────────
    if row["review_type"] == "restore_confirm":
        _execute_restore(
            review_item_id=review_item_id,
            decision_payload=row.get("decision_payload", {}),
            context=context,
        )

    item = ReviewItemRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /review/items/{review_item_id}/reject
# ──────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{review_item_id}/reject",
    response_model=ResponseEnvelope[ReviewItemRead],
)
def reject_review_item_endpoint(
    review_item_id: UUID,
    body: ReviewItemRejectRequest = ReviewItemRejectRequest(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """拒绝审核项 (``in_review`` → ``rejected``)。

    On rejection:
    * The review item status changes to ``rejected``, with ``decision='rejected'``.
    * An outbox event ``review.rejected`` is published.
    * If ``review_type = 'dlq_replay'``, the dead letter replay_state
      is set back to ``cancelled``.
    """
    row = get_review_item_by_id(review_item_id)
    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"审核项 'review_item_id' 未找到",
        )

    current_status = row["status"]
    if current_status != "in_review":
        raise ApiError(
            409,
            "bad_request",
            f"review_item '{review_item_id}' has status '{current_status}'; "
            "only 'in_review' items can be rejected",
        )

    reviewer_id = context.actor.actor_id

    success = reject_review_item(
        review_item_id=review_item_id,
        reviewer_id=reviewer_id,
        reason=body.reason,
    )

    if not success:
        raise ApiError(
            409,
            "bad_request",
            f"review_item '{review_item_id}' could not be rejected "
            "(may have been modified concurrently)",
        )

    row = get_review_item_by_id(review_item_id)

    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="review.rejected",
                result="success",
                object_type="review_item",
                object_id=review_item_id,
                diff_summary={
                    "previous_status": "in_review",
                    "new_status": "rejected",
                    "decision": "rejected",
                    "reviewer_id": str(reviewer_id) if reviewer_id else None,
                },
            ),
        )

        outbox_idempotency_key = f"{row['idempotency_key']}.rejected"
        add_outbox_event(
            db,
            context,
            outbox_event_for_action(
                event_type="review.rejected",
                aggregate_type="review_item",
                aggregate_id=review_item_id,
                idempotency_key=outbox_idempotency_key,
                payload_json={
                    "review_type": row["review_type"],
                    "target_type": row["target_type"],
                    "target_id": row["target_id"],
                },
            ),
        )
        db.commit()

    # ── P2-04: Cancel DLQ replay for rejected dlq_replay reviews ──────────
    if row["review_type"] == "dlq_replay":
        _cancel_dlq_replay(
            dead_letter_id=UUID(row["target_id"]),
            review_item_id=review_item_id,
            context=context,
        )

    item = ReviewItemRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /review/items/{review_item_id}/cancel
# ──────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{review_item_id}/cancel",
    response_model=ResponseEnvelope[ReviewItemRead],
)
def cancel_review_item_endpoint(
    review_item_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """取消审核项 (``pending`` 或 ``in_review`` → ``cancelled``)。"""
    row = get_review_item_by_id(review_item_id)
    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"审核项 'review_item_id' 未找到",
        )

    current_status = row["status"]
    if current_status not in ("pending", "in_review"):
        raise ApiError(
            409,
            "bad_request",
            f"review_item '{review_item_id}' has status '{current_status}'; "
            "only 'pending' or 'in_review' items can be cancelled",
        )

    success = cancel_review_item(review_item_id)
    if not success:
        raise ApiError(
            409,
            "bad_request",
            f"review_item '{review_item_id}' could not be cancelled "
            "(may have been modified concurrently)",
        )

    row = get_review_item_by_id(review_item_id)

    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="review.cancelled",
                result="success",
                object_type="review_item",
                object_id=review_item_id,
                diff_summary={
                    "previous_status": current_status,
                    "new_status": "cancelled",
                },
            ),
        )

        outbox_idempotency_key = f"{row['idempotency_key']}.cancelled"
        add_outbox_event(
            db,
            context,
            outbox_event_for_action(
                event_type="review.cancelled",
                aggregate_type="review_item",
                aggregate_id=review_item_id,
                idempotency_key=outbox_idempotency_key,
                payload_json={
                    "review_type": row["review_type"],
                    "target_type": row["target_type"],
                    "target_id": row["target_id"],
                },
            ),
        )
        db.commit()

    # If DLQ replay is cancelled while pending or in_review, reset the dead_letter
    if row["review_type"] == "dlq_replay" and current_status in ("pending", "in_review"):
        _cancel_dlq_replay(
            dead_letter_id=UUID(row["target_id"]),
            review_item_id=review_item_id,
            context=context,
        )

    item = ReviewItemRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Batch operations
# ══════════════════════════════════════════════════════════════════════════════


@router.post(
    "/claim",
    response_model=ResponseEnvelope[dict],
)
def batch_claim_review_items_endpoint(
    body: ReviewItemBatchRequest,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Batch claim multiple review items (``pending`` → ``in_review``).

    Each item is processed independently; items already claimed or in a
    different state are counted as ``skipped``.
    """
    result = batch_claim_review_items(body.review_item_ids)

    # Write audit events for each succeeded item
    with SessionLocal() as db:
        for item in result["results"]:
            if item["status"] == "succeeded":
                rid = UUID(item["review_item_id"])
                add_audit_event(
                    db,
                    context,
                    audit_event_for_action(
                        action="review.batch_claimed",
                        result="success",
                        object_type="review_item",
                        object_id=rid,
                        diff_summary={
                            "previous_status": "pending",
                            "new_status": "in_review",
                        },
                    ),
                )
        db.commit()

    return envelope(
        result,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.post(
    "/approve",
    response_model=ResponseEnvelope[dict],
)
def batch_approve_review_items_endpoint(
    body: ReviewItemBatchRequest,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Batch approve multiple review items (``in_review`` → ``approved``).

    Each item is processed independently.  Only items currently in
    ``in_review`` status will be approved; others are counted as ``skipped``.
    """
    reviewer_id = context.actor.actor_id

    result = batch_approve_review_items(
        body.review_item_ids,
        reviewer_id=reviewer_id,
        reason=body.reason,
    )

    # Write audit events and handle triggers for succeeded items
    with SessionLocal() as db:
        for item in result["results"]:
            if item["status"] != "succeeded":
                continue
            rid = UUID(item["review_item_id"])
            row = get_review_item_by_id(rid)
            if row is None:
                continue

            add_audit_event(
                db,
                context,
                audit_event_for_action(
                    action="review.batch_approved",
                    result="success",
                    object_type="review_item",
                    object_id=rid,
                    diff_summary={
                        "previous_status": "in_review",
                        "new_status": "approved",
                        "decision": "approved",
                        "reviewer_id": str(reviewer_id) if reviewer_id else None,
                    },
                ),
            )

            # Outbox event
            outbox_key = f"{row['idempotency_key']}.batch_approved"
            add_outbox_event(
                db,
                context,
                outbox_event_for_action(
                    event_type="review.batch_approved",
                    aggregate_type="review_item",
                    aggregate_id=rid,
                    idempotency_key=outbox_key,
                    payload_json={
                        "review_type": row["review_type"],
                        "target_type": row["target_type"],
                        "target_id": row["target_id"],
                    },
                ),
            )

            # Trigger DLQ replay or restore if applicable
            if row["review_type"] == "dlq_replay":
                _execute_dlq_replay(
                    dead_letter_id=UUID(row["target_id"]),
                    review_item_id=rid,
                    context=context,
                )
            if row["review_type"] == "restore_confirm":
                _execute_restore(
                    review_item_id=rid,
                    decision_payload=row.get("decision_payload", {}),
                    context=context,
                )
        db.commit()

    return envelope(
        result,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.post(
    "/reject",
    response_model=ResponseEnvelope[dict],
)
def batch_reject_review_items_endpoint(
    body: ReviewItemBatchRequest,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Batch reject multiple review items (``in_review`` → ``rejected``).

    Each item is processed independently.  Only items currently in
    ``in_review`` status will be rejected; others are counted as ``skipped``.
    """
    reviewer_id = context.actor.actor_id

    result = batch_reject_review_items(
        body.review_item_ids,
        reviewer_id=reviewer_id,
        reason=body.reason,
    )

    # Write audit events for succeeded items
    with SessionLocal() as db:
        for item in result["results"]:
            if item["status"] != "succeeded":
                continue
            rid = UUID(item["review_item_id"])
            row = get_review_item_by_id(rid)
            if row is None:
                continue

            add_audit_event(
                db,
                context,
                audit_event_for_action(
                    action="review.batch_rejected",
                    result="success",
                    object_type="review_item",
                    object_id=rid,
                    diff_summary={
                        "previous_status": "in_review",
                        "new_status": "rejected",
                        "decision": "rejected",
                        "reviewer_id": str(reviewer_id) if reviewer_id else None,
                    },
                ),
            )

            outbox_key = f"{row['idempotency_key']}.batch_rejected"
            add_outbox_event(
                db,
                context,
                outbox_event_for_action(
                    event_type="review.batch_rejected",
                    aggregate_type="review_item",
                    aggregate_id=rid,
                    idempotency_key=outbox_key,
                    payload_json={
                        "review_type": row["review_type"],
                        "target_type": row["target_type"],
                        "target_id": row["target_id"],
                    },
                ),
            )

            # Cancel DLQ replay for rejected dlq_replay reviews
            if row["review_type"] == "dlq_replay":
                _cancel_dlq_replay(
                    dead_letter_id=UUID(row["target_id"]),
                    review_item_id=rid,
                    context=context,
                )
        db.commit()

    return envelope(
        result,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.post(
    "/cancel",
    response_model=ResponseEnvelope[dict],
)
def batch_cancel_review_items_endpoint(
    body: ReviewItemBatchRequest,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Batch cancel multiple review items (``pending``|``in_review`` → ``cancelled``).

    Each item is processed independently.  Only items in ``pending`` or
    ``in_review`` status will be cancelled; others are counted as ``skipped``.
    """
    result = batch_cancel_review_items(body.review_item_ids)

    with SessionLocal() as db:
        for item in result["results"]:
            if item["status"] != "succeeded":
                continue
            rid = UUID(item["review_item_id"])
            row = get_review_item_by_id(rid)
            if row is None:
                continue

            add_audit_event(
                db,
                context,
                audit_event_for_action(
                    action="review.batch_cancelled",
                    result="success",
                    object_type="review_item",
                    object_id=rid,
                    diff_summary={
                        "new_status": "cancelled",
                    },
                ),
            )

            # Cancel DLQ replay if applicable
            if row.get("review_type") == "dlq_replay":
                _cancel_dlq_replay(
                    dead_letter_id=UUID(row["target_id"]),
                    review_item_id=rid,
                    context=context,
                )
        db.commit()

    return envelope(
        result,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers for P2-04 DLQ replay
# ──────────────────────────────────────────────────────────────────────────────


def _execute_dlq_replay(
    *,
    dead_letter_id: UUID,
    review_item_id: UUID,
    context: RequestContext,
) -> None:
    """审核批准后执行死信重放。

    Steps:
    1. Update ``dead_letters.replay_state`` to ``'replayed'``
    2. Reset ``event_deliveries.dispatch_attempts`` to 0
       and ``delivery_state`` to ``'pending'``
    3. Reset ``events.publish_state`` to ``'pending'``
    4. Write audit event
    """
    import logging
    from sqlalchemy import text

    log = logging.getLogger(__name__)

    _UPDATE_DL_REPLAYED = text("""
        UPDATE dead_letters
        SET replay_state = 'replayed',
            replayed_at = now(),
            updated_at = now()
        WHERE dead_letter_id = :dead_letter_id
          AND replay_state = 'under_review'
    """)

    _GET_SOURCE_DELIVERY = text("""
        SELECT source_id
        FROM dead_letters
        WHERE dead_letter_id = :dead_letter_id
          AND source_type = 'event_delivery'
    """)

    _RESET_DELIVERY_FOR_REPLAY = text("""
        UPDATE event_deliveries
        SET dispatch_attempts = 0,
            delivery_state = 'pending',
            last_error = NULL,
            failed_at = NULL,
            lease_expires_at = NULL,
            updated_at = now()
        WHERE delivery_id = :delivery_id
    """)

    _RESET_EVENT_FOR_REPLAY = text("""
        UPDATE events
        SET publish_state = 'pending',
            last_error = NULL,
            updated_at = now()
        WHERE event_id = :event_id
    """)

    _GET_RELATED_EVENT = text("""
        SELECT related_event_id
        FROM dead_letters
        WHERE dead_letter_id = :dead_letter_id
    """)

    with SessionLocal() as db:
        # 1. Update dead_letter replay_state
        result = db.execute(
            _UPDATE_DL_REPLAYED,
            {"dead_letter_id": dead_letter_id},
        )
        if result.rowcount == 0:
            log.warning(
                "DLQ replay: dead_letter %s not in 'under_review' state, skipping",
                dead_letter_id,
            )
            return

        # 2. Find the event_delivery to reset
        source_delivery = db.execute(
            _GET_SOURCE_DELIVERY,
            {"dead_letter_id": dead_letter_id},
        ).mappings().first()

        if source_delivery:
            delivery_id = source_delivery["source_id"]
            db.execute(
                _RESET_DELIVERY_FOR_REPLAY,
                {"delivery_id": delivery_id},
            )
            log.info(
                "DLQ replay: reset delivery %s for dead_letter %s",
                delivery_id,
                dead_letter_id,
            )

        # 3. Reset the related event
        related = db.execute(
            _GET_RELATED_EVENT,
            {"dead_letter_id": dead_letter_id},
        ).mappings().first()

        if related and related["related_event_id"]:
            event_id = related["related_event_id"]
            db.execute(
                _RESET_EVENT_FOR_REPLAY,
                {"event_id": event_id},
            )
            log.info(
                "DLQ replay: reset event %s for dead_letter %s",
                event_id,
                dead_letter_id,
            )

        # 4. Write audit event
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="dlq.replayed",
                result="success",
                object_type="dead_letter",
                object_id=dead_letter_id,
                metadata_json={
                    "review_item_id": str(review_item_id),
                },
            ),
        )

        db.commit()

    log.info(
        "DLQ replay completed: dead_letter=%s review_item=%s",
        dead_letter_id,
        review_item_id,
    )


def _cancel_dlq_replay(
    *,
    dead_letter_id: UUID,
    review_item_id: UUID,
    context: RequestContext,
) -> None:
    """取消死信重放 — 将 dead_letters.replay_state 重置为 'cancelled'。"""
    import logging
    from sqlalchemy import text

    log = logging.getLogger(__name__)

    _CANCEL_DL_REPLAY = text("""
        UPDATE dead_letters
        SET replay_state = 'cancelled',
            updated_at = now()
        WHERE dead_letter_id = :dead_letter_id
          AND replay_state = 'under_review'
    """)

    with SessionLocal() as db:
        result = db.execute(
            _CANCEL_DL_REPLAY,
            {"dead_letter_id": dead_letter_id},
        )
        if result.rowcount == 0:
            log.warning(
                "DLQ cancel: dead_letter %s not in 'under_review' state, skipping",
                dead_letter_id,
            )
            return

        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="dlq.replay_cancelled",
                result="success",
                object_type="dead_letter",
                object_id=dead_letter_id,
                metadata_json={
                    "review_item_id": str(review_item_id),
                },
            ),
        )
        db.commit()

    log.info(
        "DLQ replay cancelled: dead_letter=%s review_item=%s",
        dead_letter_id,
        review_item_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers for P2-16 Restore via Review
# ──────────────────────────────────────────────────────────────────────────────


def _execute_restore(
    *,
    review_item_id: UUID,
    decision_payload: dict,
    context: RequestContext,
) -> None:
    """审核批准后执行恢复操作 (P2-16)。

    Uses the existing ``mneme.backup.restore_engine.run_restore_live()``
    for the actual pg_restore execution, with jobs/job_logs tracking
    and audit event recording.

    Steps:
    1. Extract backup_id / target_database_url from decision_payload
    2. Create a jobs record to track the restore execution
    3. Call run_restore_live() for pg_restore + verification
    4. Write audit events and update job status
    """
    import logging

    from mneme.backup.restore_engine import run_restore_live, RestoreResult as EngineRestoreResult
    from mneme.db.jobs import (
        add_job_log,
        create_job,
        update_job_completed,
        update_job_running,
    )
    from mneme.db.audit import add_audit_event
    from mneme.db.base import SessionLocal
    from mneme.config import get_settings

    log = logging.getLogger(__name__)

    backup_id = decision_payload.get("backup_id", "")
    target_database_url = decision_payload.get("target_database_url")

    if not backup_id:
        log.error(
            "Restore review approved but no backup_id in decision_payload: %s",
            review_item_id,
        )
        return

    log.info(
        "Restore approved via review: review_item=%s, backup_id=%s",
        review_item_id,
        backup_id,
    )

    # Resolve target database URL from settings if not provided
    if not target_database_url:
        try:
            target_database_url = get_settings().database_url
        except Exception:
            log.error(
                "Cannot determine target_database_url for restore: review=%s, backup=%s",
                review_item_id,
                backup_id,
            )
            return

    # Create job record for tracking
    job_key = f"restore.{backup_id}.{review_item_id}"
    job = create_job(
        job_type="restore",
        job_key=job_key,
        input_payload={
            "backup_id": backup_id,
            "review_item_id": str(review_item_id),
            "target_database_url": target_database_url,
        },
        priority=80,
        queue_name="admin",
        max_retries=0,
        timeout_seconds=3600,
        actor_type=context.actor.actor_type,
        actor_id=context.actor.actor_id,
    )

    job_id = UUID(job["job_id"])
    add_job_log(job_id, step="job.created",
                message=f"Restore job queued for backup_id={backup_id} via review={review_item_id}")
    add_job_log(job_id, step="restore.starting",
                message=f"Starting pg_restore for backup_id={backup_id}, target={target_database_url}")
    update_job_running(job_id)

    # Execute restore using the existing restore engine
    try:
        add_job_log(job_id, step="restore.restoring",
                    message=f"Running pg_restore live for backup_id={backup_id}")

        result: EngineRestoreResult = run_restore_live(
            backup_id=backup_id,
            target_database_url=target_database_url,
        )

        if result.success and result.report:
            # Extract verification summary
            verification = result.report.verification if result.report else {}
            add_job_log(
                job_id,
                step="restore.completed",
                message=f"Restore succeeded: status={result.report.status}, "
                        f"target={result.report.target_database}",
            )
            update_job_completed(
                job_id,
                success=True,
                output={
                    "backup_id": backup_id,
                    "restore_id": result.report.restore_id,
                    "status": result.report.status,
                    "target_database": result.report.target_database,
                    "verification_summary": {
                        "table_count_match": verification.get("table_count", {}).get("match", False),
                        "row_counts_match": verification.get("row_counts", {}).get("match", False),
                        "foreign_keys_valid": verification.get("foreign_keys", {}).get("valid", False),
                        "alembic_revision_match": verification.get("alembic_revision", {}).get("match", False),
                    },
                    "report_path": str(result.output_dir) if result.output_dir else "",
                },
            )

            with SessionLocal() as db:
                add_audit_event(
                    db,
                    context,
                    audit_event_for_action(
                        action="restore.executed",
                        result="success",
                        object_type="restore_run",
                        object_id=job_id,
                        metadata_json={
                            "backup_id": backup_id,
                            "review_item_id": str(review_item_id),
                            "job_id": str(job_id),
                            "restore_id": result.report.restore_id,
                        },
                    ),
                )
                db.commit()

            log.info(
                "Restore succeeded via review: review=%s, backup=%s, job=%s, restore=%s",
                review_item_id,
                backup_id,
                job_id,
                result.report.restore_id,
            )
        else:
            error_msg = result.error_message or "未知恢复错误"
            report_msg = (
                f"status={result.report.status}" if result.report else "无报告"
            )
            add_job_log(
                job_id,
                step="restore.failed",
                message=f"Restore failed: {error_msg[:500]} ({report_msg})",
                level="error",
            )
            update_job_completed(
                job_id,
                success=False,
                error_message=error_msg,
                output={
                    "restore_id": result.report.restore_id if result.report else None,
                    "verification": result.report.verification if result.report else {},
                } if result.report else {},
            )

            with SessionLocal() as db:
                add_audit_event(
                    db,
                    context,
                    audit_event_for_action(
                        action="restore.executed",
                        result="failed",
                        object_type="restore_run",
                        object_id=job_id,
                        metadata_json={
                            "backup_id": backup_id,
                            "review_item_id": str(review_item_id),
                            "job_id": str(job_id),
                            "error": error_msg[:500],
                        },
                    ),
                )
                db.commit()

            log.error(
                "Restore failed via review: review=%s, backup=%s, job=%s, error=%s",
                review_item_id,
                backup_id,
                job_id,
                error_msg[:200],
            )

    except Exception as exc:
        error_msg = f"Restore exception: {exc}"
        log.exception("Restore exception: review=%s, backup=%s", review_item_id, backup_id)
        add_job_log(job_id, step="restore.error", message=error_msg[:500], level="error")
        update_job_completed(job_id, success=False, error_message=error_msg)

        with SessionLocal() as db:
            add_audit_event(
                db,
                context,
                audit_event_for_action(
                    action="restore.executed",
                    result="failed",
                    object_type="restore_run",
                    object_id=job_id,
                    metadata_json={
                        "backup_id": backup_id,
                        "review_item_id": str(review_item_id),
                        "job_id": str(job_id),
                        "error": error_msg[:500],
                    },
                ),
            )
            db.commit()
