"""Admin 审计事件 API — 为治理界面列出和查看审计事件。

接口列表
--------
* ``GET /api/v4/admin/audit-events``       – 分页列表，支持过滤
* ``GET /api/v4/admin/audit-events/{id}``  – 单条审计事件详情
"""

from __future__ import annotations

import math
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.admin_queries import get_audit_event_by_id, get_audit_events
from mneme.schemas import (
    PageInfo,
    PaginationParams,
    ResponseEnvelope,
)
from mneme.schemas.admin import (
    AdminAuditEventRead,
    AdminAuditFilterParams,
    AdminAuditListResponse,
)

router = APIRouter(prefix="/admin/audit-events", tags=["admin", "audit"])


@router.get("", response_model=ResponseEnvelope[AdminAuditListResponse])
def list_audit_events(
    pagination: PaginationParams = Depends(),
    filters: AdminAuditFilterParams = Depends(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出审计事件，支持可选过滤。

    查询参数
    --------
    * ``page`` / ``page_size`` – 分页（默认 1 / 50，最大 200）。
    * ``actor_type`` – 按操作者类型过滤（user / agent / service / system）。
    * ``action`` – 按操作名称过滤（支持 LIKE 部分匹配）。
    * ``result`` – 按结果过滤（success / denied / failed）。
    * ``object_type`` – 按对象类型过滤。
    * ``occurred_after`` / ``occurred_before`` – 时间区间过滤（ISO-8601）。
    """
    rows, total = get_audit_events(
        page=pagination.page,
        page_size=pagination.page_size,
        actor_type=filters.actor_type,
        action=filters.action,
        result=filters.result,
        object_type=filters.object_type,
        occurred_after=filters.occurred_after,
        occurred_before=filters.occurred_before,
    )

    items = [AdminAuditEventRead(**row) for row in rows]
    total_pages = max(1, math.ceil(total / max(pagination.page_size, 1)))

    page_info = PageInfo(
        page=pagination.page,
        page_size=pagination.page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=pagination.page < total_pages,
        has_previous=pagination.page > 1,
    )

    data = AdminAuditListResponse(items=items, page_info=page_info)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get(
    "/{audit_id}",
    response_model=ResponseEnvelope[AdminAuditEventRead],
)
def get_audit_event_detail(
    audit_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """通过主键返回单条审计事件。"""
    row = get_audit_event_by_id(audit_id)
    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"审计事件 'audit_id' 未找到",
        )

    item = AdminAuditEventRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
