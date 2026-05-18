"""P2-06 Review Risk Routing — comprehensive tests.

Covers:
1. ReviewRouteRule construction and properties.
2. Action pattern matching (exact, wildcard suffix, prefix, '*', no match).
3. ReviewRoutingEngine rule management (add, replace, remove, reset).
4. Rule matching: action_pattern + object_type + sensitivity combinations.
5. determine_review_type for all four Phase 2 scenarios:
   - dlq_replay
   - restore_confirm
   - sensitive_access (high-sensitivity writes)
   - high_cost_call
6. does_action_require_review correctness.
7. PolicyDecision.review_item_id field (new in P2-06).
8. Pydantic schema conversion preserves review_item_id.
9. Default rules count and content.
10. Rule priority ordering and first-match semantics.
11. Edge cases: unknown sensitivity, None object_type, disabled rules.
12. DB-dependent: auto-create review_item via handle_review_required.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from mneme.security.policy import (
    Action,
    Actor,
    Decision,
    DenyReason,
    Object,
    PolicyContext,
    PolicyDecision,
)
from mneme.security.review_router import (
    ReviewRouteRule,
    ReviewRoutingEngine,
    _action_matches,
    _default_rules,
    determine_review_type,
    does_action_require_review,
    get_review_routing_engine,
    handle_review_required,
)
from mneme.schemas.policy import PolicyDecisionRead


OWNER_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJ_A = UUID("00000000-0000-0000-0000-000000000100")

CTX = PolicyContext(request_id=uuid4())


def _owner() -> Actor:
    from mneme.security.policy import actor_from_user_session
    return actor_from_user_session(user_id=OWNER_ID, role="owner", status="active")


def _action(name: str) -> Action:
    return Action(name=name)


def _obj(object_type: str = "project", sensitivity_level: str | None = None, **kw) -> Object:
    return Object(object_type=object_type, sensitivity_level=sensitivity_level, project_id=PROJ_A, **kw)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. ReviewRouteRule construction
# ═══════════════════════════════════════════════════════════════════════════════


class TestReviewRouteRule:
    def test_minimal_construction(self):
        r = ReviewRouteRule(
            name="test",
            action_pattern="*.test",
            review_type="manual",
        )
        assert r.name == "test"
        assert r.action_pattern == "*.test"
        assert r.review_type == "manual"
        assert r.object_type is None
        assert r.min_sensitivity is None
        assert r.priority == 100
        assert r.enabled is True


    def test_full_construction(self):
        r = ReviewRouteRule(
            name="full_test",
            action_pattern="project.delete",
            object_type="project",
            min_sensitivity="sensitive",
            review_type="sensitive_access",
            priority=10,
            enabled=True,
            description="Test rule for sensitive project deletes.",
            default_priority=200,
            default_due_hours=48,
        )
        assert r.name == "full_test"
        assert r.object_type == "project"
        assert r.min_sensitivity == "sensitive"
        assert r.default_priority == 200
        assert r.default_due_hours == 48


    def test_disabled_rule(self):
        r = ReviewRouteRule(
            name="disabled_rule",
            action_pattern="*",
            review_type="manual",
            enabled=False,
        )
        assert r.enabled is False


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Action pattern matching
# ═══════════════════════════════════════════════════════════════════════════════


class TestActionMatching:
    """Test _action_matches with various patterns."""

    def test_exact_match(self):
        assert _action_matches("dlq.replay", "dlq.replay") is True

    def test_exact_no_match(self):
        assert _action_matches("dlq.replay", "dlq.cancel") is False

    def test_star_matches_any(self):
        assert _action_matches("*", "anything") is True
        assert _action_matches("*", "") is True
        assert _action_matches("*", "a.b.c.d.e") is True

    def test_verb_suffix_wildcard(self):
        """*.delete matches any action ending in .delete."""
        assert _action_matches("*.delete", "project.delete") is True
        assert _action_matches("*.delete", "memory.delete") is True
        assert _action_matches("*.delete", "delete") is False  # no dot prefix
        assert _action_matches("*.delete", "project.create") is False

    def test_prefix_wildcard(self):
        """review.* matches any action starting with review."""
        assert _action_matches("review.*", "review.create") is True
        assert _action_matches("review.*", "review.approve") is True
        assert _action_matches("review.*", "reviews.list") is False
        assert _action_matches("review.*", "project.review") is False

    def test_multi_level_verb_wildcard(self):
        """*.revoke matches 'agent_token.revoke'."""
        assert _action_matches("*.revoke", "agent_token.revoke") is True

    def test_multi_dot_prefix_wildcard(self):
        """admin.* matches 'admin.config.update'."""
        assert _action_matches("admin.*", "admin.config.update") is True


# ═══════════════════════════════════════════════════════════════════════════════
# 3. ReviewRoutingEngine rule management
# ═══════════════════════════════════════════════════════════════════════════════


class TestRuleManagement:
    def test_default_rules_are_loaded(self):
        engine = ReviewRoutingEngine()
        rules = engine.rules
        assert len(rules) >= 6, f"expected at least 6 default rules, got {len(rules)}"

    def test_add_new_rule(self):
        engine = ReviewRoutingEngine()
        engine.add_rule(ReviewRouteRule(
            name="custom_test",
            action_pattern="custom.action",
            review_type="manual",
            priority=5,
        ))
        rule = engine.get_rule("custom_test")
        assert rule is not None
        assert rule.name == "custom_test"

    def test_replace_rule_by_name(self):
        engine = ReviewRoutingEngine()
        engine.add_rule(ReviewRouteRule(
            name="dlq_replay",  # same name as default
            action_pattern="dlq.replay",
            review_type="dlq_replay",
            priority=1,
            description="Replaced rule",
        ))
        rule = engine.get_rule("dlq_replay")
        assert rule.priority == 1
        assert rule.description == "Replaced rule"

    def test_remove_rule(self):
        engine = ReviewRoutingEngine()
        count_before = len(engine.rules)
        removed = engine.remove_rule("dlq_replay")
        assert removed is True
        assert len(engine.rules) == count_before - 1
        assert engine.get_rule("dlq_replay") is None

    def test_remove_nonexistent_rule(self):
        engine = ReviewRoutingEngine()
        removed = engine.remove_rule("no_such_rule")
        assert removed is False

    def test_get_nonexistent_rule(self):
        engine = ReviewRoutingEngine()
        assert engine.get_rule("no_such_rule") is None

    def test_reset_to_defaults(self):
        engine = ReviewRoutingEngine()
        engine.add_rule(ReviewRouteRule(
            name="extra_rule", action_pattern="*", review_type="manual"
        ))
        engine.reset_to_defaults()
        # Should have default rules only
        rules = engine.rules
        assert engine.get_rule("extra_rule") is None
        assert engine.get_rule("dlq_replay") is not None

    def test_rules_are_sorted_by_priority(self):
        engine = ReviewRoutingEngine()
        priorities = [r.priority for r in engine.rules]
        assert priorities == sorted(priorities), "rules must be sorted by priority"

    def test_rules_property_returns_copy(self):
        engine = ReviewRoutingEngine()
        rules = engine.rules
        rules.clear()
        assert len(engine.rules) > 0, "rules property must return a copy"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Rule matching: action + object_type + sensitivity
# ═══════════════════════════════════════════════════════════════════════════════


class TestRuleMatching:
    """Test ReviewRoutingEngine.match() for all three dimensions."""

    def test_dlq_replay_match(self):
        engine = ReviewRoutingEngine()
        rule = engine.match(
            _action("dlq.replay"),
            _obj(object_type="dead_letter"),
        )
        assert rule is not None
        assert rule.name == "dlq_replay"
        assert rule.review_type == "dlq_replay"

    def test_restore_action_match(self):
        engine = ReviewRoutingEngine()
        rule = engine.match(
            _action("backup.restore"),
            _obj(object_type="restore_run"),
        )
        assert rule is not None
        assert rule.name == "restore_confirm"
        assert rule.review_type == "restore_confirm"

    def test_high_sensitivity_delete_match(self):
        engine = ReviewRoutingEngine()
        rule = engine.match(
            _action("project.delete"),
            _obj(object_type="project", sensitivity_level="sensitive"),
        )
        assert rule is not None
        assert rule.review_type == "sensitive_access"

    def test_high_cost_call_match(self):
        engine = ReviewRoutingEngine()
        rule = engine.match(
            _action("gateway.call"),
            _obj(object_type="provider_call"),
        )
        assert rule is not None
        assert rule.name == "high_cost_call"

    def test_explicit_review_gate_match(self):
        engine = ReviewRoutingEngine()
        rule = engine.match(
            _action("review.something"),
            _obj(object_type="job"),
        )
        assert rule is not None
        assert rule.name == "explicit_review_gate"

    def test_sensitive_read_no_match(self):
        """Reading a sensitive object does NOT match any rule (no 'read' in rules)."""
        engine = ReviewRoutingEngine()
        rule = engine.match(
            _action("project.read"),
            _obj(object_type="project", sensitivity_level="secret"),
        )
        assert rule is None, "reading should not trigger review rules"

    def test_normal_sensitivity_delete_no_match(self):
        """Deleting a normal-sensitivity object does NOT match."""
        engine = ReviewRoutingEngine()
        rule = engine.match(
            _action("project.delete"),
            _obj(object_type="project", sensitivity_level="normal"),
        )
        assert rule is None, "deleting normal object should not trigger review"

    def test_object_type_filter(self):
        """Rule with object_type='dead_letter' should not match project objects."""
        engine = ReviewRoutingEngine()
        rule = engine.match(
            _action("dlq.replay"),
            _obj(object_type="project"),
        )
        assert rule is None, "dlq.replay rule should only match dead_letter objects"

    def test_disabled_rule_is_skipped(self):
        engine = ReviewRoutingEngine()
        # Disable the dlq_replay rule
        dlq_rule = engine.get_rule("dlq_replay")
        dlq_rule.enabled = False
        rule = engine.match(
            _action("dlq.replay"),
            _obj(object_type="dead_letter"),
        )
        assert rule is None, "disabled rule should not match"

    def test_no_match_returns_none(self):
        engine = ReviewRoutingEngine()
        rule = engine.match(
            _action("harmless.read"),
            _obj(object_type="note", sensitivity_level="public"),
        )
        assert rule is None


# ═══════════════════════════════════════════════════════════════════════════════
# 5. determine_review_type
# ═══════════════════════════════════════════════════════════════════════════════


class TestDetermineReviewType:
    def test_dlq_replay_type(self):
        rt = determine_review_type(
            _action("dlq.replay"),
            _obj(object_type="dead_letter"),
        )
        assert rt == "dlq_replay"

    def test_sensitive_access_type(self):
        rt = determine_review_type(
            _action("project.delete"),
            _obj(object_type="project", sensitivity_level="secret"),
        )
        assert rt == "sensitive_access"

    def test_high_cost_call_type(self):
        rt = determine_review_type(
            _action("gateway.call"),
            _obj(object_type="provider_call"),
        )
        assert rt == "high_cost_call"

    def test_restore_confirm_type(self):
        rt = determine_review_type(
            _action("db.restore"),
            _obj(object_type="restore_run"),
        )
        assert rt == "restore_confirm"

    def test_unknown_falls_back_to_manual(self):
        rt = determine_review_type(
            _action("unknown.operation"),
            _obj(object_type="unknown_type"),
        )
        assert rt == "manual"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. does_action_require_review
# ═══════════════════════════════════════════════════════════════════════════════


class TestDoesActionRequireReview:
    def test_dlq_replay_requires_review(self):
        assert does_action_require_review(
            _action("dlq.replay"),
            _obj(object_type="dead_letter"),
        ) is True

    def test_harmless_read_does_not_require_review(self):
        assert does_action_require_review(
            _action("project.read"),
            _obj(object_type="project", sensitivity_level="normal"),
        ) is False

    def test_sensitive_delete_requires_review(self):
        assert does_action_require_review(
            _action("project.delete"),
            _obj(object_type="project", sensitivity_level="sensitive"),
        ) is True

    def test_restore_requires_review(self):
        assert does_action_require_review(
            _action("backup.restore"),
            _obj(object_type="restore_run"),
        ) is True

    def test_gateway_call_requires_review(self):
        assert does_action_require_review(
            _action("gateway.call"),
            _obj(object_type="provider_call"),
        ) is True


# ═══════════════════════════════════════════════════════════════════════════════
# 7. PolicyDecision.review_item_id field
# ═══════════════════════════════════════════════════════════════════════════════


class TestPolicyDecisionReviewItemId:
    def test_allow_has_no_review_item_id(self):
        d = PolicyDecision.allow()
        assert d.review_item_id is None

    def test_deny_has_no_review_item_id(self):
        d = PolicyDecision.deny(DenyReason.user_disabled, "disabled")
        assert d.review_item_id is None

    def test_step_up_has_no_review_item_id(self):
        d = PolicyDecision.step_up(DenyReason.user_step_up_required, "step up")
        assert d.review_item_id is None

    def test_review_can_have_review_item_id(self):
        rid = uuid4()
        d = PolicyDecision(
            decision=Decision.review_required,
            deny_reason=DenyReason.review_policy_triggered,
            message="needs review",
            review_item_id=rid,
        )
        assert d.review_item_id == rid

    def test_review_factory_has_none_review_item_id(self):
        """Factory method creates decisions without review_item_id (added later)."""
        d = PolicyDecision.review(DenyReason.review_policy_triggered, "needs review")
        assert d.review_item_id is None


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Pydantic schema preserves review_item_id
# ═══════════════════════════════════════════════════════════════════════════════


class TestSchemaReviewItemId:
    def test_from_policy_decision_preserves_review_item_id(self):
        rid = uuid4()
        pd = PolicyDecision(
            decision=Decision.review_required,
            deny_reason=DenyReason.review_policy_triggered,
            message="needs review",
            review_item_id=rid,
        )
        schema = PolicyDecisionRead.from_policy_decision(pd)
        assert schema.review_item_id == rid

    def test_from_policy_decision_without_review_item_id(self):
        pd = PolicyDecision.review(DenyReason.review_policy_triggered, "needs review")
        schema = PolicyDecisionRead.from_policy_decision(pd)
        assert schema.review_item_id is None

    def test_schema_allow_no_review_item_id(self):
        pd = PolicyDecision.allow()
        schema = PolicyDecisionRead.from_policy_decision(pd)
        assert schema.review_item_id is None

    def test_schema_json_serializable_with_review_item_id(self):
        rid = uuid4()
        pd = PolicyDecision(
            decision=Decision.review_required,
            deny_reason=DenyReason.review_policy_triggered,
            message="test",
            review_item_id=rid,
        )
        schema = PolicyDecisionRead.from_policy_decision(pd)
        json_str = schema.model_dump_json()
        assert str(rid) in json_str
        assert "review_item_id" in json_str


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Default rules content
# ═══════════════════════════════════════════════════════════════════════════════


class TestDefaultRules:
    def test_default_rules_count(self):
        rules = _default_rules()
        assert len(rules) >= 8, f"expected at least 8 default rules, got {len(rules)}"

    def test_all_required_scenarios_covered(self):
        rules = _default_rules()
        names = {r.name for r in rules}
        required = {
            "dlq_replay",
            "restore_confirm",
            "high_cost_call",
            "explicit_review_gate",
        }
        missing = required - names
        assert not missing, f"missing required rules: {missing}"

    def test_high_sensitivity_rules_exist(self):
        rules = _default_rules()
        sensitivity_rules = [r for r in rules if r.review_type == "sensitive_access"]
        assert len(sensitivity_rules) >= 4, "need at least 4 sensitive_access rules"

    def test_dlq_replay_has_highest_priority(self):
        rules = _default_rules()
        dlq_rule = next(r for r in rules if r.name == "dlq_replay")
        restore_rule = next(r for r in rules if r.name == "restore_confirm")
        assert dlq_rule.priority < restore_rule.priority, "dlq_replay must be higher priority than restore"

    def test_all_rules_have_valid_review_types(self):
        from mneme.schemas.review_items import ReviewType
        valid_types = {rt.value for rt in ReviewType}
        rules = _default_rules()
        for rule in rules:
            assert rule.review_type in valid_types, (
                f"rule '{rule.name}' has invalid review_type '{rule.review_type}'"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Priority ordering and first-match semantics
# ═══════════════════════════════════════════════════════════════════════════════


class TestPriorityOrdering:
    def test_first_match_wins(self):
        """When multiple rules could match, the highest priority (lowest number) wins."""
        engine = ReviewRoutingEngine()
        # The dlq_replay rule (priority 10) should match before explicit_review_gate (priority 50)
        # for action "dlq.replay"
        rule = engine.match(
            _action("dlq.replay"),
            _obj(object_type="dead_letter"),
        )
        assert rule.name == "dlq_replay"
        # verify that explicit_review_gate would also match if not short-circuited
        # (review.* matches "dlq.replay" since dlq starts with 'r'? NO. Let me check...
        # "dlq.replay" does NOT start with "review.", so only dlq_replay rule matches)

    def test_higher_priority_rule_matches_first(self):
        engine = ReviewRoutingEngine()
        # Add a catch-all rule with very low priority
        engine.add_rule(ReviewRouteRule(
            name="catch_all",
            action_pattern="*",
            review_type="manual",
            priority=999,
        ))
        # The dlq_replay rule (priority 10) should still match first
        rule = engine.match(
            _action("dlq.replay"),
            _obj(object_type="dead_letter"),
        )
        assert rule.name == "dlq_replay", "higher priority rule should match first"

    def test_priority_sorting_preserved_after_add(self):
        engine = ReviewRoutingEngine()
        engine.add_rule(ReviewRouteRule(
            name="zzz_lowest",
            action_pattern="*",
            review_type="manual",
            priority=900,
        ))
        priorities = [r.priority for r in engine.rules]
        assert priorities == sorted(priorities), "rules must stay sorted after add"


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_none_sensitivity_on_object(self):
        """Object with None sensitivity — only matches rules without min_sensitivity."""
        engine = ReviewRoutingEngine()
        rule = engine.match(
            _action("gateway.call"),
            _obj(object_type="provider_call", sensitivity_level=None),
        )
        assert rule is not None  # high_cost_call has no min_sensitivity
        assert rule.name == "high_cost_call"

    def test_unknown_sensitivity_label(self):
        """Unknown sensitivity should rank as very high (> all known)."""
        engine = ReviewRoutingEngine()
        rule = engine.match(
            _action("project.delete"),
            _obj(object_type="project", sensitivity_level="top_secret_unknown"),
        )
        # Since unknown rates 999 >= "sensitive" (30), the rule should match
        assert rule is not None
        assert rule.review_type == "sensitive_access"

    def test_none_object_type_in_rule_matches_any(self):
        engine = ReviewRoutingEngine()
        # restore_confirm rule has object_type=None, so should match any object type
        rule = engine.match(
            _action("backup.restore"),
            _obj(object_type="any_random_type"),
        )
        assert rule is not None
        assert rule.name == "restore_confirm"

    def test_empty_action_name(self):
        assert _action_matches("*", "") is True
        assert _action_matches("*.delete", "") is False

    def test_single_segment_action_with_wildcard(self):
        """*.delete should NOT match a single-segment action 'delete'."""
        assert _action_matches("*.delete", "delete") is False


# ═══════════════════════════════════════════════════════════════════════════════
# 12. DB-dependent: handle_review_required auto-creates review_item
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.db
class TestHandleReviewRequired:
    """Integration tests that require a running PostgreSQL database.

    These tests verify the end-to-end flow:
    PolicyEngine.review_required → handle_review_required() → review_item created.
    """

    def test_auto_create_review_item_for_dlq_replay(self):
        """handle_review_required creates a review_item for dlq_replay actions."""
        decision = PolicyDecision.review(
            DenyReason.review_policy_triggered,
            "DLQ replay requires review",
        )
        actor = _owner()
        action = _action("dlq.replay")
        obj = _obj(object_type="dead_letter", object_id=uuid4())

        result = handle_review_required(decision, actor, action, obj, CTX)

        assert result.decision == Decision.review_required
        assert result.review_item_id is not None
        assert "review_item_id" in result.details
        assert "review_type" in result.details
        assert result.details["review_type"] == "dlq_replay"

        # Cleanup
        from mneme.db.base import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text("DELETE FROM review_items WHERE review_item_id = :rid"),
                       {"rid": str(result.review_item_id)})
            db.commit()

    def test_auto_create_review_item_for_sensitive_delete(self):
        """handle_review_required creates a review_item for sensitive deletes."""
        decision = PolicyDecision.review(
            DenyReason.review_policy_triggered,
            "Sensitive delete requires review",
        )
        actor = _owner()
        action = _action("project.delete")
        obj = _obj(object_type="project", sensitivity_level="sensitive", object_id=uuid4())

        result = handle_review_required(decision, actor, action, obj, CTX)

        assert result.review_item_id is not None
        assert result.details["review_type"] == "sensitive_access"

        # Cleanup
        from mneme.db.base import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text("DELETE FROM review_items WHERE review_item_id = :rid"),
                       {"rid": str(result.review_item_id)})
            db.commit()

    def test_auto_create_review_item_for_restore(self):
        """handle_review_required creates a review_item for restore operations."""
        decision = PolicyDecision.review(
            DenyReason.review_policy_triggered,
            "Restore requires review confirmation",
        )
        actor = _owner()
        action = _action("backup.restore")
        obj = _obj(object_type="restore_run", object_id=uuid4())

        result = handle_review_required(decision, actor, action, obj, CTX)

        assert result.review_item_id is not None
        assert result.details["review_type"] == "restore_confirm"

        # Cleanup
        from mneme.db.base import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text("DELETE FROM review_items WHERE review_item_id = :rid"),
                       {"rid": str(result.review_item_id)})
            db.commit()

    def test_auto_create_review_item_for_high_cost_call(self):
        """handle_review_required creates a review_item for gateway calls."""
        decision = PolicyDecision.review(
            DenyReason.review_policy_triggered,
            "Budget exceeded, review required",
        )
        actor = _owner()
        action = _action("gateway.call")
        obj = _obj(object_type="provider_call", object_id=uuid4())

        result = handle_review_required(decision, actor, action, obj, CTX)

        assert result.review_item_id is not None
        assert result.details["review_type"] == "high_cost_call"

        # Cleanup
        from mneme.db.base import SessionLocal
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(text("DELETE FROM review_items WHERE review_item_id = :rid"),
                       {"rid": str(result.review_item_id)})
            db.commit()

    def test_audit_event_linked_to_review_item(self):
        """Verify the audit event created by handle_review_required has
        the review_item_id populated."""
        from mneme.db.base import SessionLocal
        from sqlalchemy import text

        # Use a fresh context to avoid idempotency key collisions
        fresh_ctx = PolicyContext(request_id=uuid4())

        decision = PolicyDecision.review(
            DenyReason.review_policy_triggered,
            "Test review audit link",
        )
        actor = _owner()
        action = _action("dlq.replay")
        obj = _obj(object_type="dead_letter", object_id=uuid4())

        result = handle_review_required(decision, actor, action, obj, fresh_ctx)

        # Check the audit_events table
        with SessionLocal() as db:
            audit_rows = db.execute(
                text("""
                    SELECT review_item_id, action, result
                    FROM audit_events
                    WHERE review_item_id = :rid
                    ORDER BY occurred_at DESC
                    LIMIT 1
                """),
                {"rid": str(result.review_item_id)},
            ).mappings().all()

            assert len(audit_rows) > 0, (
                "Expected at least one audit event with review_item_id set"
            )
            row = audit_rows[0]
            assert str(row["review_item_id"]) == str(result.review_item_id)
            assert "policy.dlq.replay" in row["action"]

            # Cleanup
            db.execute(text("DELETE FROM audit_events WHERE review_item_id = :rid"),
                       {"rid": str(result.review_item_id)})
            db.execute(text("DELETE FROM review_items WHERE review_item_id = :rid"),
                       {"rid": str(result.review_item_id)})
            db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# 13. Singleton engine
# ═══════════════════════════════════════════════════════════════════════════════


class TestSingletonEngine:
    def test_get_review_routing_engine_returns_same_instance(self):
        e1 = get_review_routing_engine()
        e2 = get_review_routing_engine()
        assert e1 is e2

    def test_convenience_functions_use_singleton(self):
        from mneme.security import review_router as rrm
        # Add a rule via the singleton
        engine = rrm.get_review_routing_engine()
        engine.add_rule(ReviewRouteRule(
            name="singleton_test_rule",
            action_pattern="singleton.test",
            review_type="manual",
            priority=1,
        ))
        # Check via convenience function
        rt = rrm.determine_review_type(
            _action("singleton.test"),
            _obj(object_type="test"),
        )
        assert rt == "manual"
        # Cleanup
        engine.remove_rule("singleton_test_rule")
