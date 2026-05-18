"""Pydantic schemas for the Policy Engine.

These models expose the policy contract for API documentation, OpenAPI
generation, and contract testing.  The dataclass runtime objects in
:mod:`mneme.security.policy` are kept separate from these Pydantic schemas
so that the policy engine itself has zero Pydantic dependency.

Phase 1 guarantees:
* ``PolicyDecisionRead`` stays stable -- new deny reasons may be added, but
  existing field names and semantics will not change.
* ``DenyReason`` enum values are never removed, only appended.
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema


class Decision(str, Enum):
    """Mirrors :class:`mneme.security.policy.Decision` for OpenAPI generation."""

    allow = "allow"
    deny = "deny"
    review_required = "review_required"
    step_up_required = "step_up_required"


class DenyReason(str, Enum):
    """Mirrors :class:`mneme.security.policy.DenyReason` for OpenAPI generation.

    Grouping convention:
    * ``user_*`` -- caused by user actor properties.
    * ``agent_*`` -- caused by agent actor properties.
    * Others -- scope / sensitivity / step-up / review / system reasons.
    """

    # -- user reasons --
    user_disabled = "user_disabled"
    user_locked = "user_locked"
    user_role_forbidden = "user_role_forbidden"
    user_session_expired = "user_session_expired"
    user_session_revoked = "user_session_revoked"
    user_step_up_required = "user_step_up_required"
    user_not_authenticated = "user_not_authenticated"

    # -- agent reasons --
    agent_disabled = "agent_disabled"
    agent_archived = "agent_archived"
    agent_token_revoked = "agent_token_revoked"
    agent_token_expired = "agent_token_expired"

    # -- scope reasons --
    project_out_of_scope = "project_out_of_scope"
    capability_out_of_scope = "capability_out_of_scope"

    # -- sensitivity reasons --
    sensitivity_ceiling_exceeded = "sensitivity_ceiling_exceeded"

    # -- review / step-up reasons --
    review_policy_triggered = "review_policy_triggered"
    step_up_expired = "step_up_expired"

    # -- system reasons --
    system_only = "system_only"


class PolicyDecisionRead(ApiSchema):
    """Public-facing representation of a policy decision.

    Returned by API endpoints that expose authorization checks, and embedded
    in error responses when a request is denied or gated by policy.

    When ``decision != "allow"`` both ``deny_reason`` and ``message`` are
    always populated so callers receive actionable feedback without inspecting
    internal details.

    When ``decision == "review_required"`` and a review item has been
    auto-created, ``review_item_id`` contains the UUID of the review_item
    for callers to track the review workflow.
    """

    decision: Decision = Field(
        description="Policy decision: allow, deny, review_required, or step_up_required."
    )
    deny_reason: DenyReason | None = Field(
        default=None,
        description="Machine-readable reason for non-allow decisions.  Values prefixed "
        "'user_' originate from user-actor properties; 'agent_' from agent-actor properties.",
    )
    message: str | None = Field(
        default=None,
        description="Human-readable explanation of the decision.",
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Contextual details (e.g. allowed_roles, project_scope, sensitivity_level).",
    )
    review_item_id: UUID | None = Field(
        default=None,
        description="UUID of the auto-created review_item when decision=review_required "
        "and handle_review_required() has been called.",
    )

    @classmethod
    def from_policy_decision(cls, pd: Any) -> PolicyDecisionRead:
        """Convert a runtime PolicyDecision dataclass into this Pydantic schema."""
        return cls(
            decision=Decision(pd.decision.value),
            deny_reason=DenyReason(pd.deny_reason.value) if pd.deny_reason else None,
            message=pd.message,
            details=pd.details,
            review_item_id=pd.review_item_id,
        )


class ActorRef(ApiSchema):
    """Lightweight actor reference used in policy evaluation context."""

    actor_type: str = Field(
        description="One of 'user', 'agent', 'service', 'system'."
    )
    actor_id: UUID | None = Field(
        default=None,
        description="UUID of the acting user, agent, or service account.",
    )
    role: str | None = Field(
        default=None,
        description="User role when actor_type='user'.",
    )
    status: str | None = Field(
        default=None,
        description="Actor status.",
    )


class PolicyIssue(ApiSchema):
    """Describes a single policy issue in a diagnostic / dry-run response."""

    check: str = Field(
        description="Name of the policy check that produced this issue "
        "(e.g. 'actor_status', 'rbac', 'project_scope').",
    )
    decision: Decision = Field(
        description="Decision this check yielded.",
    )
    deny_reason: DenyReason | None = Field(
        default=None,
        description="Reason code if decision != allow.",
    )
    message: str | None = Field(
        default=None,
        description="Human-readable description of the issue.",
    )


class PolicyDryRunResult(ApiSchema):
    """Result of a policy dry-run that evaluates all checks without
    short-circuiting (useful for debugging and contract tests)."""

    final_decision: Decision = Field(
        description="The canonical decision that would be returned by a live can() call."
    )
    issues: list[PolicyIssue] = Field(
        default_factory=list,
        description="Every non-allow check result, ordered by pipeline stage.",
    )
    allow_count: int = Field(
        default=0,
        description="Number of checks that passed.",
    )
    total_checks: int = Field(
        default=0,
        description="Total number of checks executed.",
    )
