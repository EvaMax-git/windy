"""P2-06 审核策略 API — 规则管理和预演评估。

接口列表
--------
* ``GET    /api/v4/review/policy/rules``       — 列出所有路由规则
* ``GET    /api/v4/review/policy/rules/{name}`` — 获取单条规则
* ``POST   /api/v4/review/policy/rules``       — 添加或更新规则
* ``DELETE /api/v4/review/policy/rules/{name}`` — 删除规则
* ``POST   /api/v4/review/policy/reset``       — 重置为默认规则
* ``POST   /api/v4/review/policy/evaluate``    — 预演：评估操作/对象
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from pydantic import Field

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.schemas.common import ApiSchema, ResponseEnvelope
from mneme.security.review_router import (
    ReviewRouteRule,
    ReviewRoutingEngine,
    get_review_routing_engine,
)
from mneme.security.policy import Action, Object as PolicyObject

router = APIRouter(prefix="/review/policy", tags=["review-policy"])


# ═══════════════════════════════════════════════════════════════════════════════
# API Schemas
# ═══════════════════════════════════════════════════════════════════════════════


class ReviewRouteRuleRead(ApiSchema):
    """API 返回的公开规则表示。"""

    name: str
    action_pattern: str
    object_type: str | None = None
    min_sensitivity: str | None = None
    review_type: str
    priority: int
    enabled: bool
    description: str = ""
    default_priority: int = 100
    default_due_hours: int | None = None

    @classmethod
    def from_rule(cls, rule: ReviewRouteRule) -> "ReviewRouteRuleRead":
        return cls(
            name=rule.name,
            action_pattern=rule.action_pattern,
            object_type=rule.object_type,
            min_sensitivity=rule.min_sensitivity,
            review_type=rule.review_type,
            priority=rule.priority,
            enabled=rule.enabled,
            description=rule.description,
            default_priority=rule.default_priority,
            default_due_hours=rule.default_due_hours,
        )


class ReviewRouteRuleCreate(ApiSchema):
    """创建或更新规则的请求体。"""

    name: str = Field(description="Unique rule identifier.")
    action_pattern: str = Field(description="Glob-style action pattern, e.g. '*.delete', 'dlq.replay'.")
    object_type: str | None = Field(default=None, description="Object type to match, or null for any.")
    min_sensitivity: str | None = Field(default=None, description="Minimum sensitivity level, or null.")
    review_type: str = Field(default="manual", description="review_type to use for auto-created review_items.")
    priority: int = Field(default=100, ge=0, le=1000, description="Rule priority (lower = higher).")
    enabled: bool = Field(default=True, description="Whether the rule is active.")
    description: str = Field(default="", description="Human-readable description.")
    default_priority: int = Field(default=100, ge=0, le=1000, description="Default priority on created review_item.")
    default_due_hours: int | None = Field(default=None, ge=0, description="Default due offset in hours, or null.")

    def to_rule(self) -> ReviewRouteRule:
        return ReviewRouteRule(
            name=self.name,
            action_pattern=self.action_pattern,
            object_type=self.object_type,
            min_sensitivity=self.min_sensitivity,
            review_type=self.review_type,
            priority=self.priority,
            enabled=self.enabled,
            description=self.description,
            default_priority=self.default_priority,
            default_due_hours=self.default_due_hours,
        )


class ReviewRouteRuleListResponse(ApiSchema):
    """规则列表接口的响应。"""

    rules: list[ReviewRouteRuleRead]
    total: int


class EvaluateRequest(ApiSchema):
    """预演评估的请求体。"""

    action_name: str = Field(description="Action name, e.g. 'project.delete'.")
    object_type: str = Field(default="job", description="Object type being acted upon.")
    sensitivity_level: str | None = Field(default=None, description="Object sensitivity level.")


class EvaluateResponse(ApiSchema):
    """预演评估的响应。"""

    review_required: bool = Field(description="Whether this action/object would trigger review.")
    review_type: str | None = Field(default=None, description="The review_type that would be used.")
    matched_rule: str | None = Field(default=None, description="Name of the matching rule, if any.")


def _engine() -> ReviewRoutingEngine:
    return get_review_routing_engine()


# ═══════════════════════════════════════════════════════════════════════════════
# GET /review/policy/rules
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/rules", response_model=ResponseEnvelope[ReviewRouteRuleListResponse])
def list_rules(
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """按优先级顺序列出所有路由规则。

    Returns all rules (enabled and disabled) sorted by priority.
    """
    engine = _engine()
    rules = [ReviewRouteRuleRead.from_rule(r) for r in engine.rules]
    data = ReviewRouteRuleListResponse(rules=rules, total=len(rules))
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# GET /review/policy/rules/{name}
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/rules/{name}", response_model=ResponseEnvelope[ReviewRouteRuleRead])
def get_rule(
    name: str,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """按名称获取单条规则。"""
    engine = _engine()
    rule = engine.get_rule(name)
    if rule is None:
        raise ApiError(
            404,
            "bad_request",
            f"审核策略规则 'name' 未找到",
        )
    data = ReviewRouteRuleRead.from_rule(rule)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# POST /review/policy/rules
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/rules", response_model=ResponseEnvelope[ReviewRouteRuleRead], status_code=201)
def upsert_rule(
    body: ReviewRouteRuleCreate,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """添加新规则或更新已有规则（按名称）。

    Rules with the same name are replaced.  Rules are sorted by priority
    (lower = higher priority).  The first matching enabled rule wins during
    evaluation.
    """
    engine = _engine()
    rule = body.to_rule()
    engine.add_rule(rule)

    data = ReviewRouteRuleRead.from_rule(rule)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# DELETE /review/policy/rules/{name}
# ═══════════════════════════════════════════════════════════════════════════════


@router.delete("/rules/{name}", response_model=ResponseEnvelope[dict])
def delete_rule(
    name: str,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """按名称删除规则。

    Returns 404 if the rule does not exist.
    """
    engine = _engine()
    removed = engine.remove_rule(name)
    if not removed:
        raise ApiError(
            404,
            "bad_request",
            f"审核策略规则 'name' 未找到",
        )
    return envelope(
        {"deleted": name},
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# POST /review/policy/reset
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/reset", response_model=ResponseEnvelope[ReviewRouteRuleListResponse])
def reset_rules(
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """将所有规则重置为内置默认值。

    This removes any custom rules and restores the default rule set.
    """
    engine = _engine()
    engine.reset_to_defaults()
    rules = [ReviewRouteRuleRead.from_rule(r) for r in engine.rules]
    data = ReviewRouteRuleListResponse(rules=rules, total=len(rules))
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# POST /review/policy/evaluate
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/evaluate", response_model=ResponseEnvelope[EvaluateResponse])
def evaluate(
    body: EvaluateRequest,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """预演：评估某个操作/对象组合是否会触发
    a review based on the current routing rules.

    This is a read-only operation that does not create any review_item or
    audit event.  Use it for debugging rule configuration and pre-flight checks.
    """
    engine = _engine()
    action = Action(name=body.action_name)
    obj = PolicyObject(
        object_type=body.object_type,
        sensitivity_level=body.sensitivity_level,
    )

    rule = engine.match(action, obj)
    review_required = rule is not None
    review_type = rule.review_type if rule else None
    matched_rule = rule.name if rule else None

    data = EvaluateResponse(
        review_required=review_required,
        review_type=review_type,
        matched_rule=matched_rule,
    )
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
