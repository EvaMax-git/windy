"""Mneme Policy Engine.

Implements the stable interface required by Phase 1 gate 7/8/9:

    can(actor, action, object, context) -> decision

Decisions: allow | deny | review_required | step_up_required

This module provides:

* Data-classes for Actor, Action, Object, PolicyContext.
* The ``can`` entry-point that runs the full policy pipeline.
* ``DenyReason`` enum with values distinguishable between user and agent.
* Helper factories to build policy inputs from authentication results
  (``AuthenticatedSession`` / ``AuthenticatedAgent``).

Rules applied (Phase 1 minimum combination):

1. **Actor status** – disabled/locked actors are always denied.
2. **RBAC** – ``owner`` / ``operator`` / ``viewer`` / ``auditor`` role-to-action
   mapping for *user* actors.
3. **Project Scope** – agent token ``project_scope`` restricts which project
   objects the agent can touch.
4. **Capability Scope** – agent token ``capability_scope`` restricts which
   actions (high-level verbs) the agent may perform.
5. **Sensitivity Ceiling** – an agent may not access objects whose
   ``sensitivity_level`` is above the token's ``sensitivity_ceiling``.
6. **Step-Up** – actions tagged ``requires_step_up`` must be accompanied by a
   recently verified step-up session; otherwise ``step_up_required`` is
   returned.
7. **Review Policy** – actions on high-sensitivity objects or admin-sensitive
   endpoints may yield ``review_required``.  Full review workflow is out of
   scope for Phase 1, but the decision contract is stable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import UUID


# ── Decision ──────────────────────────────────────────────────────────────────


class Decision(str, Enum):
    """Policy decision returned by :func:`can`."""

    allow = "allow"
    deny = "deny"
    review_required = "review_required"
    step_up_required = "step_up_required"


# ── Deny / Step-Up / Review reasons ───────────────────────────────────────────


class DenyReason(str, Enum):
    """Reasons returned in ``PolicyDecision.deny_reason``.

    Values are grouped so that user vs. agent origin is trivially
    distinguishable:

    * ``_user_*``   – caused by the user actor (e.g. disabled, locked, role).
    * ``_agent_*``  – caused by the agent actor (e.g. disabled, token revoked).
    * others        – scope / sensitivity / step-up / review reasons.
    """

    # ── user reasons ──
    user_disabled = "user_disabled"
    user_locked = "user_locked"
    user_role_forbidden = "user_role_forbidden"
    user_session_expired = "user_session_expired"
    user_session_revoked = "user_session_revoked"
    user_step_up_required = "user_step_up_required"
    user_not_authenticated = "user_not_authenticated"

    # ── agent reasons ──
    agent_disabled = "agent_disabled"
    agent_archived = "agent_archived"
    agent_token_revoked = "agent_token_revoked"
    agent_token_expired = "agent_token_expired"

    # ── scope reasons ──
    project_out_of_scope = "project_out_of_scope"
    capability_out_of_scope = "capability_out_of_scope"

    # ── sensitivity reasons ──
    sensitivity_ceiling_exceeded = "sensitivity_ceiling_exceeded"

    # ── review / step-up reasons ──
    review_policy_triggered = "review_policy_triggered"
    step_up_expired = "step_up_expired"

    # ── system reasons ──
    system_only = "system_only"


# ── Utility ───────────────────────────────────────────────────────────────────

_SENSITIVITY_ORDER: dict[str, int] = {
    "public": 0,
    "normal": 10,
    "private": 20,
    "sensitive": 30,
    "secret": 40,
}


def _sensitivity_rank(level: str | None) -> int:
    """Return numeric rank for a sensitivity label.

    Unknown / None values are treated as *most sensitive* (fail-secure).
    """
    if level is None:
        return 999
    return _SENSITIVITY_ORDER.get(level.lower(), 999)


# ── Policy input data-classes ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Actor:
    """Represents the subject requesting an action.

    For *user* actors the key fields are ``role``, ``status``, and
    ``step_up_verified``.

    For *agent* actors the key fields are ``project_scope``,
    ``capability_scope``, and ``sensitivity_ceiling``.
    """

    actor_type: str  # "user" | "agent" | "service" | "system"
    actor_id: UUID | None = None
    # ── user fields ──
    role: str | None = None  # "owner" | "operator" | "viewer" | "auditor"
    status: str | None = None  # "active" | "disabled" | "locked" | ...
    step_up_verified: bool = False
    # ── agent / token fields ──
    sensitivity_ceiling: str | None = None
    project_scope: list[str] | None = None
    capability_scope: list[str] | None = None
    # ── auth context (for observability) ──
    auth_context_type: str | None = None  # "user_session" | "agent_token" | ...
    auth_context_id: UUID | None = None
    # ── extension point ──
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Action:
    """Represents the operation the actor wants to perform.

    The ``name`` follows a convention of ``[resource.]verb``, e.g.
    ``"create"``, ``"project.create"``, ``"memory.write"``.
    """

    name: str
    requires_step_up: bool = False


@dataclass(frozen=True)
class Object:
    """Represents the target of an action.

    ``object_type`` is a logical resource name (``"project"``, ``"agent"``,
    ``"memory"``, ``"audit_event"``, …).

    ``project_id`` binds the object to a project so that project-scope checks
    can be enforced.

    ``sensitivity_level`` is one of the five
    :class:`mneme.schemas.common.SensitivityLevel` values.
    """

    object_type: str
    object_id: UUID | None = None
    project_id: UUID | None = None
    sensitivity_level: str | None = None
    owner_user_id: UUID | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyContext:
    """Carries metadata about the request environment.

    ``extra`` may hold request_id, correlation_id, ip, geo, etc.
    """

    request_id: UUID | None = None
    correlation_id: UUID | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ── Policy decision data-class ────────────────────────────────────────────────


@dataclass(frozen=True)
class PolicyDecision:
    """Result of a :func:`can` call.

    ``decision`` is always one of the four :class:`Decision` values.

    When ``decision != allow`` the fields ``deny_reason`` and ``message`` are
    always populated so callers can log / audit / display meaningful feedback
    without extra inspection.

    When ``decision == review_required`` and
    :func:`~mneme.security.review_router.handle_review_required` has been
    called, ``review_item_id`` contains the UUID of the auto-created
    ``review_item`` for callers to track the review workflow.
    """

    decision: Decision
    deny_reason: DenyReason | None = None
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    review_item_id: UUID | None = None

    @classmethod
    def allow(cls) -> PolicyDecision:
        return cls(decision=Decision.allow)

    @classmethod
    def deny(cls, reason: DenyReason, message: str, **details: Any) -> PolicyDecision:
        return cls(
            decision=Decision.deny,
            deny_reason=reason,
            message=message,
            details=details,
        )

    @classmethod
    def review(cls, reason: DenyReason, message: str, **details: Any) -> PolicyDecision:
        return cls(
            decision=Decision.review_required,
            deny_reason=reason,
            message=message,
            details=details,
        )

    @classmethod
    def step_up(cls, reason: DenyReason, message: str, **details: Any) -> PolicyDecision:
        return cls(
            decision=Decision.step_up_required,
            deny_reason=reason,
            message=message,
            details=details,
        )


# ── RBAC matrix ───────────────────────────────────────────────────────────────

# Mapping of (verb, object_type) → allowed roles.
#
# The key is (verb, object_type) where verb is the last segment of the action
# name.  For example, "project.create" → verb "create", object_type "project".
#
# A special '*' wildcard matches any object_type.

_RBAC_RULES: dict[tuple[str, str], set[str]] = {
    # ── read-only verbs ──
    ("read", "*"): {"owner", "operator", "viewer", "auditor"},
    ("list", "*"): {"owner", "operator", "viewer", "auditor"},
    ("get", "*"): {"owner", "operator", "viewer", "auditor"},
    ("search", "*"): {"owner", "operator", "viewer", "auditor"},
    ("export", "*"): {"owner", "operator", "auditor"},  # viewer must not export
    # ── mutations ──
    ("create", "*"): {"owner", "operator"},
    ("update", "*"): {"owner", "operator"},
    ("delete", "*"): {"owner"},
    ("write", "*"): {"owner", "operator"},
    ("execute", "*"): {"owner", "operator"},
    ("revoke", "*"): {"owner", "operator"},
    # ── admin verbs ──
    ("admin", "*"): {"owner"},
}


def _allowed_roles_for_action(action: Action, object_type: str) -> set[str]:
    """Return the set of role codes that are permitted for *action* on
    *object_type*."""
    # Split action name: "project.create" → parts=["project","create"]
    parts = action.name.split(".")
    # The verb is the last segment; treat everything before as resource prefix.
    if len(parts) >= 2:
        verb = parts[-1]
    else:
        verb = action.name

    # Try exact verb match with specific object_type first, then wildcard.
    key = (verb, object_type)
    if key in _RBAC_RULES:
        return _RBAC_RULES[key]
    key_wild = (verb, "*")
    if key_wild in _RBAC_RULES:
        return _RBAC_RULES[key_wild]

    # Fallback: allow only owner for unknown verb/object combos (fail-secure).
    return {"owner"}


# ── Pipeline helpers ──────────────────────────────────────────────────────────


def _check_actor_status(actor: Actor) -> PolicyDecision | None:
    """Return a deny decision if the actor is disabled / locked / archived."""
    if actor.status is None:
        return None  # system actors skip status checks

    status = actor.status.lower()
    if status in ("active", "pending_bootstrap"):
        return None

    if actor.actor_type == "user":
        if status == "disabled":
            return PolicyDecision.deny(
                DenyReason.user_disabled,
                f"user {actor.actor_id} is disabled",
                status=actor.status,
            )
        if status == "locked":
            return PolicyDecision.deny(
                DenyReason.user_locked,
                f"user {actor.actor_id} is locked",
                status=actor.status,
            )
    elif actor.actor_type == "agent":
        if status == "disabled":
            return PolicyDecision.deny(
                DenyReason.agent_disabled,
                f"agent {actor.actor_id} is disabled",
                status=actor.status,
            )
        if status == "archived":
            return PolicyDecision.deny(
                DenyReason.agent_archived,
                f"agent {actor.actor_id} is archived",
                status=actor.status,
            )

    # Unknown non-active status → deny (fail-secure).
    return PolicyDecision.deny(
        DenyReason.user_disabled if actor.actor_type == "user" else DenyReason.agent_disabled,
        f"{actor.actor_type} {actor.actor_id} has status {actor.status}",
        status=actor.status,
    )


def _check_rbac(actor: Actor, action: Action, obj: Object) -> PolicyDecision | None:
    """Return a deny decision if the user's role is not allowed."""
    if actor.role is None:
        return None  # system / service actors skip RBAC

    allowed = _allowed_roles_for_action(action, obj.object_type)
    if actor.role.lower() in allowed:
        return None

    return PolicyDecision.deny(
        DenyReason.user_role_forbidden,
        f"role '{actor.role}' is not allowed to perform '{action.name}' on '{obj.object_type}'",
        role=actor.role,
        action=action.name,
        object_type=obj.object_type,
        allowed_roles=sorted(allowed),
    )


def _check_project_scope(actor: Actor, obj: Object) -> PolicyDecision | None:
    """Return a deny decision if the object's project is outside the actor's scope."""
    if actor.project_scope is None:
        return None  # no scope restriction

    if obj.project_id is None:
        # Object has no project → allow (e.g. global resources).
        # If you need to deny project-less objects for scoped agents, tighten here.
        return None

    if not actor.project_scope:
        # Empty list means explicitly "no projects allowed"
        return PolicyDecision.deny(
            DenyReason.project_out_of_scope,
            f"agent {actor.actor_id} has no projects in scope",
            project_scope=actor.project_scope,
        )

    if str(obj.project_id) not in actor.project_scope:
        return PolicyDecision.deny(
            DenyReason.project_out_of_scope,
            f"project {obj.project_id} is not in agent {actor.actor_id} project scope",
            project_id=str(obj.project_id),
            project_scope=actor.project_scope,
        )
    return None


def _check_capability_scope(actor: Actor, action: Action) -> PolicyDecision | None:
    """Return a deny decision if the action is outside the actor's capabilities."""
    if actor.capability_scope is None:
        return None  # no capability restriction (e.g. user actors)

    if not actor.capability_scope:
        # Explicitly empty list → nothing allowed
        return PolicyDecision.deny(
            DenyReason.capability_out_of_scope,
            f"agent {actor.actor_id} has no capabilities in scope",
            capability_scope=actor.capability_scope,
        )

    # Try exact match first, then prefix match (e.g. capability "project.*"
    # matches action "project.create").
    action_name = action.name
    for cap in actor.capability_scope:
        if cap == action_name:
            return None
        if cap.endswith(".*") and action_name.startswith(cap[:-2]):
            return None
        if cap.endswith(".") and action_name.startswith(cap):
            return None

    return PolicyDecision.deny(
        DenyReason.capability_out_of_scope,
        f"action '{action_name}' is not in agent {actor.actor_id} capability scope",
        action=action_name,
        capability_scope=actor.capability_scope,
    )


def _check_sensitivity_ceiling(actor: Actor, obj: Object) -> PolicyDecision | None:
    """Return a deny decision if the object's sensitivity exceeds the actor's ceiling."""
    if actor.sensitivity_ceiling is None:
        return None  # no ceiling restriction (e.g. user actors)

    if obj.sensitivity_level is None:
        # Object has no sensitivity label → allow.
        return None

    ceiling_rank = _sensitivity_rank(actor.sensitivity_ceiling)
    object_rank = _sensitivity_rank(obj.sensitivity_level)

    if object_rank <= ceiling_rank:
        return None

    return PolicyDecision.deny(
        DenyReason.sensitivity_ceiling_exceeded,
        (
            f"object sensitivity '{obj.sensitivity_level}' (rank {object_rank}) "
            f"exceeds agent {actor.actor_id} ceiling "
            f"'{actor.sensitivity_ceiling}' (rank {ceiling_rank})"
        ),
        object_sensitivity=obj.sensitivity_level,
        ceiling=actor.sensitivity_ceiling,
    )


def _check_step_up(actor: Actor, action: Action) -> PolicyDecision | None:
    """Return step_up_required if the action needs step-up but actor hasn't done it."""
    if not action.requires_step_up:
        return None

    if actor.step_up_verified:
        return None

    reason = (
        DenyReason.user_step_up_required
        if actor.actor_type == "user"
        else DenyReason.step_up_expired
    )
    return PolicyDecision.step_up(
        reason,
        f"action '{action.name}' requires step-up verification",
        action=action.name,
    )


def _check_review_policy(_actor: Actor, action: Action, obj: Object) -> PolicyDecision | None:
    """Return review_required for actions that trigger review policy.

    Phase 1 rules (illustrative – can be extended in later phases):

    * Deleting or administering objects with sensitivity >= "sensitive" → review.
    * Any action explicitly prefixed ``review.*`` (placeholder for future
      explicit review-gate).
    """
    # Admin- / destroy- / reveal- sensitive objects → review
    dangerous_verbs = {"delete", "admin", "revoke", "purge", "reveal"}
    parts = action.name.split(".")
    verb = parts[-1] if len(parts) >= 2 else action.name

    if verb in dangerous_verbs and obj.sensitivity_level is not None:
        object_rank = _sensitivity_rank(obj.sensitivity_level)
        if object_rank >= _sensitivity_rank("sensitive"):
            return PolicyDecision.review(
                DenyReason.review_policy_triggered,
                (
                    f"action '{action.name}' on '{obj.object_type}' "
                    f"with sensitivity '{obj.sensitivity_level}' requires review"
                ),
                action=action.name,
                object_type=obj.object_type,
                sensitivity_level=obj.sensitivity_level,
            )

    # Future: check explicit review gate via action prefix
    if action.name.startswith("review."):
        return PolicyDecision.review(
            DenyReason.review_policy_triggered,
            f"action '{action.name}' is gated by review policy",
            action=action.name,
        )

    return None


# ── Pipeline runner ───────────────────────────────────────────────────────────


def can(
    actor: Actor,
    action: Action,
    object: Object,
    context: PolicyContext | None = None,
) -> PolicyDecision:
    """Run the full policy pipeline and return a :class:`PolicyDecision`.

    This is the **single entry point** for all authorization checks in Mneme.
    Every protected write endpoint MUST call this function before mutating
    state.

    The pipeline executes checks in order of increasing cost:

    1. Actor status (disabled / locked / archived).
    2. RBAC role-to-action mapping (user actors only).
    3. Project scope (agent actors only).
    4. Capability scope (agent actors only).
    5. Sensitivity ceiling (agent actors only).
    6. Step-up requirement.
    7. Review policy trigger.

    The *first* non-allow decision short-circuits the pipeline and is returned
    immediately.  Later checks are intentionally not evaluated so that attackers
    cannot probe scope / ceiling rules by receiving different reasons.

    Parameters
    ----------
    actor : Actor
        The authenticated subject.
    action : Action
        The operation being requested.
    object : Object
        The resource being accessed.
    context : PolicyContext or None
        Request metadata for future logging / audit enrichment.

    Returns
    -------
    PolicyDecision
        Always a concrete decision; never ``None``.
    """
    del context  # reserved for future enrichment

    # 1. Actor status – must come first to catch disabled / locked actors
    #    before any other policy check.
    decision = _check_actor_status(actor)
    if decision is not None:
        return decision

    # 2. RBAC – user role-to-action mapping.
    decision = _check_rbac(actor, action, object)
    if decision is not None:
        return decision

    # 3. Project scope – agent token project_scope.
    decision = _check_project_scope(actor, object)
    if decision is not None:
        return decision

    # 4. Capability scope – agent token capability_scope.
    decision = _check_capability_scope(actor, action)
    if decision is not None:
        return decision

    # 5. Sensitivity ceiling – agent token sensitivity_ceiling.
    decision = _check_sensitivity_ceiling(actor, object)
    if decision is not None:
        return decision

    # 6. Step-up – action requires step-up verification.
    decision = _check_step_up(actor, action)
    if decision is not None:
        return decision

    # 7. Review policy – high-sensitivity or admin endpoints require review.
    decision = _check_review_policy(actor, action, object)
    if decision is not None:
        return decision

    return PolicyDecision.allow()


# ── Factory helpers ───────────────────────────────────────────────────────────
#
# These convert the domain-level auth results (AuthenticatedSession /
# AuthenticatedAgent) into policy Actor instances so route handlers
# don't have to manually map fields.


def actor_from_user_session(
    user_id: UUID,
    *,
    role: str,
    status: str,
    step_up_verified: bool = False,
    session_id: UUID | None = None,
) -> Actor:
    """Build a policy :class:`Actor` from an authenticated user session."""
    return Actor(
        actor_type="user",
        actor_id=user_id,
        role=role,
        status=status,
        step_up_verified=step_up_verified,
        auth_context_type="user_session",
        auth_context_id=session_id,
    )


def actor_from_agent_token(
    agent_id: UUID,
    *,
    status: str,
    sensitivity_ceiling: str,
    project_scope: list[str] | None = None,
    capability_scope: list[str] | None = None,
    token_id: UUID | None = None,
) -> Actor:
    """Build a policy :class:`Actor` from an authenticated agent token."""
    return Actor(
        actor_type="agent",
        actor_id=agent_id,
        status=status,
        sensitivity_ceiling=sensitivity_ceiling,
        project_scope=project_scope,
        capability_scope=capability_scope,
        auth_context_type="agent_token",
        auth_context_id=token_id,
    )


def actor_system() -> Actor:
    """Return a system actor (used for internal / background jobs)."""
    return Actor(actor_type="system")
