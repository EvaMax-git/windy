"""P2-03 / P2-04 死信队列管理 API — 查询、查看和重放死信记录。

接口列表
--------
* ``GET  /api/v4/admin/dead-letters``          – 分页列表，支持过滤
* ``GET  /api/v4/admin/dead-letters/{id}``     – 单条记录详情
* ``POST /api/v4/admin/dead-letters/{id}/replay`` – 提交重放请求（P2-04）
"""

from __future__ import annotations

import math
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from sqlalchemy.exc import IntegrityError

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.base import SessionLocal
from mneme.db.dead_letters import (
    count_active_reviews_for_dead_letter,
    get_dead_letter_by_id,
    get_dead_letters,
    update_replay_state,
)
from mneme.db.review_items import create_review_item
from mneme.db.audit import add_audit_event, add_outbox_event
from mneme.schemas import (
    PageInfo,
    PaginationParams,
    ResponseEnvelope,
)
from mneme.schemas.common import ApiSchema
from mneme.schemas.dead_letters import (
    DeadLetterFilterParams,
    DeadLetterListResponse,
    DeadLetterRead,
)
from mneme.security.audit import (
    audit_event_for_action,
    outbox_event_for_action,
)


# ── Reply response schema ──────────────────────────────────────────────────────


class DeadLetterReplayResponse(ApiSchema):
    """``POST /admin/dead-letters/{id}/replay`` 的响应体。"""

    dead_letter_id: UUID
    review_item_id: UUID
    message: str = "重放请求已提交审核"

router = APIRouter(prefix="/admin/dead-letters", tags=["admin", "dlq"])


# ──────────────────────────────────────────────────────────────────────────────
# GET /admin/dead-letters
# ──────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=ResponseEnvelope[DeadLetterListResponse])
def list_dead_letters(
    pagination: PaginationParams = Depends(),
    filters: DeadLetterFilterParams = Depends(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出死信记录，支持可选过滤。

    查询参数
    --------
    * ``page`` / ``page_size`` – 分页（默认 1 / 50，最大 200）。
    * ``failure_class`` – 按故障类型过滤。
    * ``replay_state`` – 按重放状态过滤。
    * ``source_type`` – 按来源类型过滤。
    * ``created_after`` / ``created_before`` – 时间区间过滤。
    """
    rows, total = get_dead_letters(
        page=pagination.page,
        page_size=pagination.page_size,
        failure_class=filters.failure_class.value if filters.failure_class else None,
        replay_state=filters.replay_state.value if filters.replay_state else None,
        source_type=filters.source_type.value if filters.source_type else None,
        created_after=filters.created_after,
        created_before=filters.created_before,
    )

    items = [DeadLetterRead(**row) for row in rows]

    total_pages = max(1, math.ceil(total / max(pagination.page_size, 1)))

    page_info = PageInfo(
        page=pagination.page,
        page_size=pagination.page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=pagination.page < total_pages,
        has_previous=pagination.page > 1,
    )

    data = DeadLetterListResponse(items=items, page_info=page_info)

    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# GET /admin/dead-letters/{dead_letter_id}
# ──────────────────────────────────────────────────────────────────────────────


@router.get(
    "/{dead_letter_id}",
    response_model=ResponseEnvelope[DeadLetterRead],
)
def get_dead_letter_detail(
    dead_letter_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """通过主键返回单条死信记录。"""
    row = get_dead_letter_by_id(dead_letter_id)
    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"死信 'dead_letter_id' 未找到",
        )

    item = DeadLetterRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /admin/dead-letters/{dead_letter_id}/replay  (P2-04)
# ──────────────────────────────────────────────────────────────────────────────


@router.post(
    "/{dead_letter_id}/replay",
    response_model=ResponseEnvelope[DeadLetterReplayResponse],
    status_code=201,
)
def submit_dead_letter_replay(
    dead_letter_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """提交死信记录的重放请求，需经过审核。

    流程
    ----
    1. 验证死信记录存在且 ``replay_state = 'pending'``。
    2. 检查该死信记录没有活跃（非终态）的审核项
       （同一死信记录不能创建两个并行的重放审核）。
    3. 创建 ``review_item``，设置 ``review_type='dlq_replay'`` 和
       ``target_type='dead_letter'``。
    4. 将 ``dead_letters.replay_state`` 转为 ``'under_review'``。
    5. 写入审计事件和发件箱事件。

    返回创建的 ``review_item_id`` 供跟踪使用。
    """
    # 1. Validate dead_letter
    dl = get_dead_letter_by_id(dead_letter_id)
    if dl is None:
        raise ApiError(
            404,
            "bad_request",
            f"死信 'dead_letter_id' 未找到",
        )

    if dl["replay_state"] != "pending":
        raise ApiError(
            409,
            "bad_request",
            f"死信 '{dead_letter_id}' 当前重放状态为 '{dl['replay_state']}'；"
            "只有 'pending' 状态的记录才能提交重放",
        )

    # 2. Check for existing active reviews
    active_count = count_active_reviews_for_dead_letter(dead_letter_id)
    if active_count > 0:
        raise ApiError(
            409,
            "bad_request",
            f"死信 '{dead_letter_id}' 已有 {active_count} 个活跃审核项； "
            "不能创建重复的重放审核",
        )

    # 3. Transition dead_letter to under_review
    updated = update_replay_state(
        dead_letter_id=dead_letter_id,
        new_state="under_review",
        expected_state="pending",
    )
    if not updated:
        raise ApiError(
            409,
            "bad_request",
            f"死信 '{dead_letter_id}' 重放状态被并发修改； "
            "请重试",
        )

    # 4. Create review_item
    idempotency_key = context.idempotency_key or str(uuid4())

    try:
        row = create_review_item(
            project_id=None,
            review_type="dlq_replay",
            target_type="dead_letter",
            target_id=dead_letter_id,
            status="pending",
            priority=100,
            requester_actor_type=context.actor.actor_type,
            requester_actor_id=context.actor.actor_id,
            decision_payload={
                "source_type": dl["source_type"],
                "source_id": dl["source_id"],
                "failure_class": dl["failure_class"],
                "error_message": dl.get("error_message", ""),
            },
            correlation_id=context.correlation_id,
            request_id=context.request_id,
            idempotency_key=idempotency_key,
        )
    except IntegrityError:
        # Roll back the dead_letter state change
        update_replay_state(
            dead_letter_id=dead_letter_id,
            new_state="pending",
            expected_state="under_review",
        )
        raise ApiError(
            409,
            "idempotency_conflict",
            f"幂等键 '{idempotency_key}' 的审核项已存在",
        )

    review_item_id = UUID(row["review_item_id"])

    # 5. Move review_item to 'in_review' so it's ready for approval
    from mneme.db.review_items import move_to_in_review
    move_to_in_review(review_item_id)

    # 6. Write audit + outbox events
    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="dlq.replay_submitted",
                result="success",
                object_type="dead_letter",
                object_id=dead_letter_id,
                metadata_json={
                    "review_item_id": str(review_item_id),
                    "replay_state": "under_review",
                },
            ),
        )

        add_outbox_event(
            db,
            context,
            outbox_event_for_action(
                event_type="review.created",
                aggregate_type="review_item",
                aggregate_id=review_item_id,
                idempotency_key=f"{idempotency_key}.created",
                payload_json={
                    "review_type": "dlq_replay",
                    "target_type": "dead_letter",
                    "target_id": str(dead_letter_id),
                },
            ),
        )
        db.commit()

    data = DeadLetterReplayResponse(
        dead_letter_id=dead_letter_id,
        review_item_id=review_item_id,
        message="重放请求已提交审核",
    )

    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
