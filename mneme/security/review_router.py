"""P2-06 Review Risk Routing & Policy Engine Integration.

This module provides the configurable review routing engine that bridges the
Policy Engine's ``review_required`` decision with the ``review_items`` workflow.

Architecture
------------

1. **ReviewRouteRule** – a named configuration entry that maps an
   ``(action_pattern, object_type, min_sensitivity)`` combination to a
   ``review_type`` with a ``priority``.

2. **ReviewRoutingEngine** – the central registry of rules.  Evaluates an
   action/object pair against all enabled rules and returns the best-matching
   ``review_type``.  Also provides the high-level
   ``handle_review_required()`` helper that:

   * determines the appropriate ``review_type`` via rule matching,
   * auto-creates a ``review_item`` in the database,
   * returns a new ``PolicyDecision`` with ``review_item_id`` populated,
   * writes an audit event linking the ``review_item``.

3. **Default rules** – cover the four scenarios required by Phase 2:

   * High-sensitivity writes → ``sensitive_access``
   * High-cost / budget-exceeded calls → ``high_cost_call``
   * DLQ replay → ``dlq_replay``
   * Restore operations → ``restore_confirm``

Usage
-----

.. code-block:: python

    from mneme.security.policy import can
    from mneme.security.review_router import handle_review_required

    decision = can(actor, action, object, context)
    if decision.decision == Decision.review_required:
        decision = handle_review_required(decision, actor, action, object, context)
        # decision.review_item_id is now populated
        return error_with_review_item(decision)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from mneme.api.context import ActorContext, RequestContext
from mneme.db.base import SessionLocal
from mneme.db.review_items import create_review_item
from mneme.db.audit import add_audit_event, add_outbox_event
from mneme.security.audit import (
    audit_event_for_policy_review_required,
    outbox_event_for_action,
)
from mneme.security.policy import (
    Action,
    Actor,
    Decision,
    Object,
    PolicyContext,
    PolicyDecision,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Sensitivity ordering (mirrors policy.py for standalone use)
# ═══════════════════════════════════════════════════════════════════════════════

_SENSITIVITY_ORDER: dict[str, int] = {
    "public": 0,
    "normal": 10,
    "private": 20,
    "sensitive": 30,
    "secret": 40,
}


def _sensitivity_rank(level: str | None) -> int:
    if level is None:
        return -1  # No sensitivity → lowest rank
    return _SENSITIVITY_ORDER.get(level.lower(), 999)


# ═══════════════════════════════════════════════════════════════════════════════
# Review Route Rule
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ReviewRouteRule:
    """A single review-routing rule.

    When an action matches ``action_pattern`` (glob-style, e.g. ``"*.delete"``
    or ``"dlq.replay"``), the object type matches ``object_type``
    (``None`` = any), and the object's sensitivity is at least
    ``min_sensitivity``, the rule fires and recommends ``review_type``.

    Rules are sorted by ``priority`` (lower = higher priority).  The first
    matching rule wins.
    """

    name: str
    """Human-readable rule identifier, e.g. ``"high_sensitivity_write"``."""

    action_pattern: str
    """Glob-style action pattern.  ``"*"`` matches any action, ``"*.delete"``
    matches ``"project.delete"``, ``"memory.delete"`` etc., and an exact string
    like ``"dlq.replay"`` matches only that action."""

    object_type: str | None = None
    """Object type to match, or ``None`` to match any object type."""

    min_sensitivity: str | None = None
    """Minimum sensitivity level required to trigger, or ``None`` to trigger
    regardless of sensitivity."""

    review_type: str = "manual"
    """The ``review_type`` to use when creating a ``review_item`` for this
    rule.  Must be a valid :class:`mneme.schemas.review_items.ReviewType` value."""

    priority: int = 100
    """Rule priority (lower = higher).  Used to resolve ties when multiple
    rules match the same action/object."""

    enabled: bool = True
    """Whether this rule is active."""

    description: str = ""
    """Human-readable description of when this rule applies."""

    # ── context to pass through to review_item ──
    default_priority: int = 100
    """Default priority to set on the created review_item."""

    default_due_hours: int | None = None
    """Default ``due_at`` offset in hours from now.  ``None`` = no due date."""


# ═══════════════════════════════════════════════════════════════════════════════
# Default rules
# ═══════════════════════════════════════════════════════════════════════════════

def _default_rules() -> list[ReviewRouteRule]:
    """Return the built-in review routing rules for Phase 2.

    These cover the four mandatory scenarios.  Additional rules can be
    registered at runtime via :func:`ReviewRoutingEngine.add_rule`.
    """
    return [
        # ── 1. DLQ replay — always requires review ─────────────────────────
        ReviewRouteRule(
            name="dlq_replay",
            action_pattern="dlq.replay",
            object_type="dead_letter",
            review_type="dlq_replay",
            priority=10,
            description="DLQ replay must be approved via Review before re-dispatching.",
            default_priority=200,
        ),
        # ── 1b. Vault credential reveal — always requires review ────────────
        ReviewRouteRule(
            name="vault_credential_reveal",
            action_pattern="vault.credential.reveal",
            object_type="credential",
            review_type="sensitive_access",
            priority=12,
            description="Revealing Vault credentials requires review approval.",
            default_priority=250,
        ),
        # ── 2. Restore operations — always requires review ─────────────────
        ReviewRouteRule(
            name="restore_confirm",
            action_pattern="*.restore",
            review_type="restore_confirm",
            priority=20,
            description="Database restore operations must be confirmed via Review.",
            default_priority=300,
            default_due_hours=72,
        ),
        # ── 3. High-sensitivity writes ─────────────────────────────────────
        ReviewRouteRule(
            name="high_sensitivity_delete",
            action_pattern="*.delete",
            min_sensitivity="sensitive",
            review_type="sensitive_access",
            priority=30,
            description="Deleting sensitive objects requires review.",
            default_priority=150,
        ),
        ReviewRouteRule(
            name="high_sensitivity_admin",
            action_pattern="*.admin",
            min_sensitivity="sensitive",
            review_type="sensitive_access",
            priority=31,
            description="Admin actions on sensitive objects require review.",
            default_priority=150,
        ),
        ReviewRouteRule(
            name="high_sensitivity_revoke",
            action_pattern="*.revoke",
            min_sensitivity="sensitive",
            review_type="sensitive_access",
            priority=32,
            description="Revoking access to sensitive objects requires review.",
            default_priority=150,
        ),
        ReviewRouteRule(
            name="high_sensitivity_purge",
            action_pattern="*.purge",
            min_sensitivity="sensitive",
            review_type="sensitive_access",
            priority=33,
            description="Purging sensitive objects requires review.",
            default_priority=200,
        ),
        # ── 4. High-cost calls — triggered by budget exceeded context ─────
        ReviewRouteRule(
            name="high_cost_call",
            action_pattern="gateway.call",
            review_type="high_cost_call",
            priority=40,
            description="Gateway calls that exceed budget or cost thresholds require review.",
            default_priority=100,
            default_due_hours=24,
        ),
        # ── 5. Review-prefixed actions (explicit review gate) ──────────────
        ReviewRouteRule(
            name="explicit_review_gate",
            action_pattern="review.*",
            review_type="manual",
            priority=50,
            description="Actions explicitly prefixed with 'review.' are gated by review.",
            default_priority=100,
        ),
        # ── 6. Generic sensitive write catch-all ───────────────────────────
        ReviewRouteRule(
            name="sensitive_write_catchall",
            action_pattern="*.write",
            min_sensitivity="sensitive",
            review_type="sensitive_access",
            priority=60,
            description="Any write operation on sensitive objects may require review.",
            default_priority=100,
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# Glob matching helper
# ═══════════════════════════════════════════════════════════════════════════════


def _action_matches(pattern: str, action_name: str) -> bool:
    """Check whether *action_name* matches *pattern*.

    Supported patterns:

    * ``"*"`` – matches any action.
    * ``"*.verb"`` – matches any action ending in ``.verb`` (e.g. ``"*.delete"``
      matches ``"project.delete"``, ``"memory.delete"``).
    * ``"prefix.*"`` – matches any action starting with ``prefix.`` (e.g.
      ``"review.*"`` matches ``"review.something"``).
    * exact match – ``"dlq.replay"`` matches only ``"dlq.replay"``.
    """
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        suffix = pattern[1:]  # ".delete"
        return action_name.endswith(suffix)
    if pattern.endswith(".*"):
        prefix = pattern[:-2]  # "review"
        return action_name.startswith(prefix + ".")
    return action_name == pattern


# ═══════════════════════════════════════════════════════════════════════════════
# Review Routing Engine
# ═══════════════════════════════════════════════════════════════════════════════


class ReviewRoutingEngine:
    """Registry of review-routing rules with match / auto-create capability.

    This is a singleton used by the policy pipeline and API endpoints.
    Rules are evaluated in priority order (lower = higher priority).
    The first matching enabled rule wins.

    Thread-safe for read operations.  Write operations (``add_rule``,
    ``remove_rule``, ``reset_to_defaults``) are NOT safe for concurrent
    modification; they are intended for admin API usage during maintenance.
    """

    def __init__(self) -> None:
        self._rules: list[ReviewRouteRule] = []
        self.reset_to_defaults()

    # ── Rule management ──────────────────────────────────────────────────

    @property
    def rules(self) -> list[ReviewRouteRule]:
        """Return a copy of the current rules list (read-only snapshot)."""
        return list(self._rules)

    def add_rule(self, rule: ReviewRouteRule) -> None:
        """Add or replace a rule by name.

        If a rule with the same ``name`` already exists, it is replaced.
        """
        for i, existing in enumerate(self._rules):
            if existing.name == rule.name:
                self._rules[i] = rule
                logger.info("Review route rule replaced: name=%s", rule.name)
                return
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority)
        logger.info("Review route rule added: name=%s priority=%d", rule.name, rule.priority)

    def remove_rule(self, name: str) -> bool:
        """Remove a rule by name.  Returns ``True`` if a rule was removed."""
        for i, rule in enumerate(self._rules):
            if rule.name == name:
                self._rules.pop(i)
                logger.info("Review route rule removed: name=%s", name)
                return True
        return False

    def get_rule(self, name: str) -> ReviewRouteRule | None:
        """Return a rule by name, or ``None``."""
        for rule in self._rules:
            if rule.name == name:
                return rule
        return None

    def reset_to_defaults(self) -> None:
        """Reset the rule registry to built-in defaults."""
        self._rules = sorted(_default_rules(), key=lambda r: r.priority)
        logger.info("Review route rules reset to defaults (%d rules)", len(self._rules))

    # ── Rule matching ─────────────────────────────────────────────────────

    def match(
        self,
        action: Action,
        object: Object,
    ) -> ReviewRouteRule | None:
        """Return the first matching enabled rule for *action* + *object*.

        Returns ``None`` if no rule matches.
        """
        for rule in self._rules:
            if not rule.enabled:
                continue

            # Check action pattern
            if not _action_matches(rule.action_pattern, action.name):
                continue

            # Check object_type (None = match any)
            if rule.object_type is not None and rule.object_type != object.object_type:
                continue

            # Check sensitivity floor
            if rule.min_sensitivity is not None:
                obj_rank = _sensitivity_rank(object.sensitivity_level)
                min_rank = _sensitivity_rank(rule.min_sensitivity)
                if obj_rank < min_rank:
                    continue

            return rule

        return None

    def determine_review_type(
        self,
        action: Action,
        object: Object,
    ) -> str:
        """Return the best-matching ``review_type`` for *action* + *object*.

        If no rule matches, returns ``"manual"`` as a safe default.
        """
        rule = self.match(action, object)
        if rule is not None:
            return rule.review_type
        return "manual"

    def does_action_require_review(
        self,
        action: Action,
        object: Object,
    ) -> bool:
        """Check whether *action* on *object* would trigger a review (based on
        routing rules alone).

        This is independent of the Policy Engine's ``can()`` check and can be
        used for pre-flight / dry-run evaluations.
        """
        rule = self.match(action, object)
        return rule is not None

    # ── Auto-create review item ───────────────────────────────────────────

    def handle_review_required(
        self,
        decision: PolicyDecision,
        actor: Actor,
        action: Action,
        object: Object,
        context: PolicyContext | None = None,
        *,
        db: Session | None = None,
    ) -> PolicyDecision:
        """Auto-create a ``review_item`` when the Policy Engine returns
        ``review_required``.

        This is the **primary integration point** between the Policy Engine
        and the Review workflow.  Callers should invoke this immediately after
        :func:`~mneme.security.policy.can` when the decision is
        ``review_required``.

        Steps
        -----
        1. Match action/object against the routing rules to determine the
           appropriate ``review_type``.
        2. Create a ``review_item`` row in the database.
        3. Write an audit event linking the review item.
        4. Write an outbox event ``review.created``.
        5. Return a new ``PolicyDecision`` with ``review_item_id`` populated.

        Parameters
        ----------
        decision : PolicyDecision
            The ``review_required`` decision returned by ``can()``.
        actor : Actor
            The actor that triggered the policy check.
        action : Action
            The action being attempted.
        object : Object
            The target object of the action.
        context : PolicyContext | None
            Optional request metadata.
        db : Session | None
            Optional existing DB session.  If ``None``, a new session is
            created and committed.

        Returns
        -------
        PolicyDecision
            A new decision with ``review_item_id`` filled in.
        """
        # 1. Determine review_type via routing rules
        review_type = self.determine_review_type(action, object)
        rule = self.match(action, object)

        # 2. Build IDs
        request_id = context.request_id if (context and context.request_id) else uuid4()
        correlation_id = (
            context.correlation_id if (context and context.correlation_id) else request_id
        )
        idempotency_key = f"review.auto.{request_id}.{action.name}"

        # 3. Compute due_at from rule if specified
        due_at = None
        if rule is not None and rule.default_due_hours is not None:
            from datetime import timedelta
            due_at = datetime.now(timezone.utc) + timedelta(hours=rule.default_due_hours)

        # 4. Map actor info to requester fields
        requester_actor_type = actor.actor_type if actor.actor_type else "system"
        requester_actor_id = actor.actor_id

        # 5. Build the target_type from object.object_type
        target_type = object.object_type if object.object_type else "job"

        # 6. Determine review item priority
        item_priority = 100
        if rule is not None:
            item_priority = rule.default_priority

        # 7. Build decision_payload with policy context
        decision_payload: dict[str, Any] = {
            "action": action.name,
            "object_type": object.object_type,
            "policy_message": decision.message,
            "deny_reason": decision.deny_reason.value if decision.deny_reason else None,
            "auto_created": True,
        }
        if rule is not None:
            decision_payload["rule_name"] = rule.name

        # 8. Create review_item
        own_db = db is None
        if own_db:
            db = SessionLocal()

        try:
            row = create_review_item(
                project_id=object.project_id,
                review_type=review_type,
                target_type=target_type,
                target_id=object.object_id or uuid4(),
                priority=item_priority,
                requester_actor_type=requester_actor_type,
                requester_actor_id=requester_actor_id,
                due_at=due_at,
                decision_payload=decision_payload,
                correlation_id=correlation_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
            )
            review_item_id = UUID(row["review_item_id"])

            # 9. Write audit event
            audit_event = audit_event_for_policy_review_required(
                action=f"policy.{action.name}",
                decision=decision,
                object_type=object.object_type,
                object_id=object.object_id,
                project_id=object.project_id,
                review_item_id=review_item_id,
            )

            # Build a minimal context for audit
            audit_ctx = RequestContext(
                request_id=request_id,
                correlation_id=correlation_id,
                actor=ActorContext(
                    actor_type=requester_actor_type,
                    actor_id=requester_actor_id,
                ),
            )

            add_audit_event(db, audit_ctx, audit_event)

            # 10. Write outbox event
            outbox_event = outbox_event_for_action(
                event_type="review.created",
                aggregate_type="review_item",
                aggregate_id=review_item_id,
                idempotency_key=f"{idempotency_key}.created",
                payload_json={
                    "review_type": review_type,
                    "target_type": target_type,
                    "target_id": str(object.object_id) if object.object_id else None,
                    "auto_created": True,
                    "action": action.name,
                },
            )
            add_outbox_event(db, audit_ctx, outbox_event)

            if own_db:
                db.commit()

            logger.info(
                "Auto-created review_item: id=%s type=%s action=%s rule=%s",
                review_item_id,
                review_type,
                action.name,
                rule.name if rule else "default",
            )

        finally:
            if own_db:
                db.close()

        # 11. Return enriched PolicyDecision
        return PolicyDecision(
            decision=Decision.review_required,
            deny_reason=decision.deny_reason,
            message=decision.message,
            details={
                **decision.details,
                "review_item_id": str(review_item_id),
                "review_type": review_type,
            },
            review_item_id=review_item_id,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Module-level singleton
# ═══════════════════════════════════════════════════════════════════════════════

_engine: ReviewRoutingEngine | None = None


def get_review_routing_engine() -> ReviewRoutingEngine:
    """Return the module-level :class:`ReviewRoutingEngine` singleton."""
    global _engine
    if _engine is None:
        _engine = ReviewRoutingEngine()
    return _engine


def handle_review_required(
    decision: PolicyDecision,
    actor: Actor,
    action: Action,
    object: Object,
    context: PolicyContext | None = None,
    *,
    db: Session | None = None,
) -> PolicyDecision:
    """Convenience wrapper around :meth:`ReviewRoutingEngine.handle_review_required`.

    Uses the module-level singleton engine.
    """
    return get_review_routing_engine().handle_review_required(
        decision=decision,
        actor=actor,
        action=action,
        object=object,
        context=context,
        db=db,
    )


def determine_review_type(
    action: Action,
    object: Object,
) -> str:
    """Convenience wrapper around :meth:`ReviewRoutingEngine.determine_review_type`."""
    return get_review_routing_engine().determine_review_type(action, object)


def does_action_require_review(
    action: Action,
    object: Object,
) -> bool:
    """Check if an action/object combination would trigger a review.

    Convenience wrapper around :meth:`ReviewRoutingEngine.does_action_require_review`.
    """
    return get_review_routing_engine().does_action_require_review(action, object)
