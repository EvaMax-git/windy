"""Comprehensive tests for the Mneme Policy Engine (P1-07).

Covers:
* All four decisions: allow, deny, review_required, step_up_required.
* All DenyReason variants, grouped as user / agent / scope / sensitivity /
  step-up / review / system.
* Actor status checks for user and agent actors.
* RBAC role-to-action mapping for all four roles.
* Project scope and capability scope for agent actors.
* Sensitivity ceiling enforcement.
* Step-up requirement on actions flagged requires_step_up.
* Review policy triggers for dangerous verbs on sensitive objects.
* Short-circuit behavior: first deny stops the pipeline.
* Factory helpers: actor_from_user_session, actor_from_agent_token, actor_system.
* Edge cases: unknown sensitivity, empty scopes, None project_id, system actor.
* Pydantic schema conversion (PolicyDecision -> PolicyDecisionRead).
* Contract: all DenyReason values are stable and user/agent distinguishable.
* Contract: Decision enum contains exactly four values.
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
    actor_from_agent_token,
    actor_from_user_session,
    actor_system,
    can,
)
from mneme.schemas.policy import (
    Decision as SchemaDecision,
)
from mneme.schemas.policy import (
    DenyReason as SchemaDenyReason,
)
from mneme.schemas.policy import (
    PolicyDecisionRead,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

OWNER_ID = UUID("00000000-0000-0000-0000-000000000001")
VIEWER_ID = UUID("00000000-0000-0000-0000-000000000002")
OPERATOR_ID = UUID("00000000-0000-0000-0000-000000000003")
AUDITOR_ID = UUID("00000000-0000-0000-0000-000000000004")
AGENT_ID = UUID("00000000-0000-0000-0000-000000000010")
PROJ_A = UUID("00000000-0000-0000-0000-000000000100")
PROJ_B = UUID("00000000-0000-0000-0000-000000000101")
PROJ_OUTSIDE = UUID("00000000-0000-0000-0000-000000000199")

CTX = PolicyContext(request_id=uuid4())


def _owner(**kw) -> Actor:
    return actor_from_user_session(user_id=OWNER_ID, role="owner", status="active", **kw)


def _viewer(**kw) -> Actor:
    return actor_from_user_session(user_id=VIEWER_ID, role="viewer", status="active", **kw)


def _operator(**kw) -> Actor:
    return actor_from_user_session(user_id=OPERATOR_ID, role="operator", status="active", **kw)


def _auditor(**kw) -> Actor:
    return actor_from_user_session(user_id=AUDITOR_ID, role="auditor", status="active", **kw)


def _agent(**kw) -> Actor:
    defaults = dict(
        agent_id=AGENT_ID,
        status="active",
        sensitivity_ceiling="normal",
        project_scope=[str(PROJ_A)],
        capability_scope=["project.*", "memory.*"],
    )
    defaults.update(kw)
    return actor_from_agent_token(**defaults)


def _obj(object_type="project", project_id=PROJ_A, sensitivity_level=None, **kw) -> Object:
    return Object(object_type=object_type, project_id=project_id, sensitivity_level=sensitivity_level, **kw)


def _allow(result: PolicyDecision) -> None:
    assert result.decision == Decision.allow, f"expected allow, got {result.decision.value}: {result.message}"


def _deny(result: PolicyDecision, reason: DenyReason | None = None) -> None:
    assert result.decision == Decision.deny, f"expected deny, got {result.decision.value}: {result.message}"
    if reason is not None:
        assert result.deny_reason == reason, f"expected reason {reason.value}, got {result.deny_reason.value if result.deny_reason else None}"


def _review(result: PolicyDecision, reason: DenyReason | None = None) -> None:
    assert result.decision == Decision.review_required, f"expected review_required, got {result.decision.value}: {result.message}"
    if reason is not None:
        assert result.deny_reason == reason


def _step_up(result: PolicyDecision, reason: DenyReason | None = None) -> None:
    assert result.decision == Decision.step_up_required, f"expected step_up_required, got {result.decision.value}: {result.message}"
    if reason is not None:
        assert result.deny_reason == reason


# ── Decision enum contract ─────────────────────────────────────────────────────


def test_decision_enum_has_exactly_four_values():
    """Phase 1 gate: Decision must contain exactly allow/deny/review_required/step_up_required."""
    assert set(Decision) == {Decision.allow, Decision.deny, Decision.review_required, Decision.step_up_required}


# ── DenyReason enum contract ───────────────────────────────────────────────────


def test_deny_reason_user_values_are_distinguishable():
    """All user reasons must start with 'user_' so agent tests can filter them."""
    user_reasons = [
        DenyReason.user_disabled,
        DenyReason.user_locked,
        DenyReason.user_role_forbidden,
        DenyReason.user_session_expired,
        DenyReason.user_session_revoked,
        DenyReason.user_step_up_required,
        DenyReason.user_not_authenticated,
    ]
    for r in user_reasons:
        assert r.value.startswith("user_"), f"{r.value} must start with 'user_'"


def test_deny_reason_agent_values_are_distinguishable():
    """All agent reasons must start with 'agent_' so user tests can filter them."""
    agent_reasons = [
        DenyReason.agent_disabled,
        DenyReason.agent_archived,
        DenyReason.agent_token_revoked,
        DenyReason.agent_token_expired,
    ]
    for r in agent_reasons:
        assert r.value.startswith("agent_"), f"{r.value} must start with 'agent_'"


def test_deny_reason_user_and_agent_are_disjoint():
    """No reason value is shared between user and agent categories."""
    user = {r for r in DenyReason if r.value.startswith("user_")}
    agent = {r for r in DenyReason if r.value.startswith("agent_")}
    assert user.isdisjoint(agent)


def test_deny_reason_enum_is_stable():
    """Ensure no DenyReason values are accidentally removed (append-only contract)."""
    required = {
        "user_disabled",
        "user_locked",
        "user_role_forbidden",
        "user_session_expired",
        "user_session_revoked",
        "user_step_up_required",
        "user_not_authenticated",
        "agent_disabled",
        "agent_archived",
        "agent_token_revoked",
        "agent_token_expired",
        "project_out_of_scope",
        "capability_out_of_scope",
        "sensitivity_ceiling_exceeded",
        "review_policy_triggered",
        "step_up_expired",
        "system_only",
    }
    actual = {r.value for r in DenyReason}
    missing = required - actual
    assert not missing, f"DenyReason enum missing required values: {missing}"


# ── Actor status checks ────────────────────────────────────────────────────────


class TestActorStatus:
    def test_active_user_is_allowed(self):
        _allow(can(_owner(), Action(name="read"), _obj(), CTX))

    def test_pending_bootstrap_is_allowed(self):
        """Bootstrap user must be allowed to create resources so initial setup works."""
        actor = actor_from_user_session(user_id=OWNER_ID, role="owner", status="pending_bootstrap")
        _allow(can(actor, Action(name="project.create"), _obj(), CTX))

    def test_disabled_user_is_denied(self):
        actor = actor_from_user_session(user_id=OWNER_ID, role="owner", status="disabled")
        _deny(can(actor, Action(name="read"), _obj(), CTX), DenyReason.user_disabled)

    def test_locked_user_is_denied(self):
        actor = actor_from_user_session(user_id=OWNER_ID, role="owner", status="locked")
        _deny(can(actor, Action(name="read"), _obj(), CTX), DenyReason.user_locked)

    def test_disabled_agent_is_denied(self):
        agent = _agent(status="disabled")
        _deny(can(agent, Action(name="project.read"), _obj(), CTX), DenyReason.agent_disabled)

    def test_archived_agent_is_denied(self):
        agent = _agent(status="archived")
        _deny(can(agent, Action(name="project.read"), _obj(), CTX), DenyReason.agent_archived)

    def test_unknown_user_status_is_denied(self):
        """Fail-secure: unrecognized status must deny."""
        actor = actor_from_user_session(user_id=OWNER_ID, role="owner", status="bogus_status")
        result = can(actor, Action(name="read"), _obj(), CTX)
        assert result.decision == Decision.deny
        assert result.deny_reason.value.startswith("user_")


# ── RBAC checks ────────────────────────────────────────────────────────────────


class TestRBAC:
    """Test RBAC role-to-action matrix for all four roles and key verbs."""

    # read verbs (all roles allowed)
    @pytest.mark.parametrize("verb", ["read", "list", "get", "search"])
    @pytest.mark.parametrize("actor_factory,role", [
        (_owner, "owner"),
        (_operator, "operator"),
        (_viewer, "viewer"),
        (_auditor, "auditor"),
    ])
    def test_read_verbs_allow_all_roles(self, verb, actor_factory, role):
        _allow(can(actor_factory(), Action(name=f"project.{verb}"), _obj(), CTX))

    # export verb (viewer not allowed)
    def test_export_allows_auditor(self):
        _allow(can(_auditor(), Action(name="project.export"), _obj(), CTX))

    def test_export_denies_viewer(self):
        _deny(can(_viewer(), Action(name="project.export"), _obj(), CTX), DenyReason.user_role_forbidden)

    # mutation verbs (viewer/auditor not allowed)
    @pytest.mark.parametrize("verb", ["create", "update", "write", "execute", "revoke"])
    def test_mutation_allows_owner(self, verb):
        _allow(can(_owner(), Action(name=f"project.{verb}"), _obj(), CTX))

    @pytest.mark.parametrize("verb", ["create", "update", "write", "execute", "revoke"])
    def test_mutation_allows_operator(self, verb):
        _allow(can(_operator(), Action(name=f"project.{verb}"), _obj(), CTX))

    @pytest.mark.parametrize("verb", ["create", "update", "write", "execute"])
    def test_mutation_denies_viewer(self, verb):
        _deny(can(_viewer(), Action(name=f"project.{verb}"), _obj(), CTX), DenyReason.user_role_forbidden)

    @pytest.mark.parametrize("verb", ["create", "update", "write", "execute"])
    def test_mutation_denies_auditor(self, verb):
        _deny(can(_auditor(), Action(name=f"project.{verb}"), _obj(), CTX), DenyReason.user_role_forbidden)

    # delete verb (owner only)
    def test_delete_allows_owner(self):
        _allow(can(_owner(), Action(name="project.delete"), _obj(), CTX))

    def test_delete_denies_operator(self):
        _deny(can(_operator(), Action(name="project.delete"), _obj(), CTX), DenyReason.user_role_forbidden)

    # admin verb (owner only)
    def test_admin_allows_owner(self):
        _allow(can(_owner(), Action(name="project.admin"), _obj(), CTX))

    def test_admin_denies_operator(self):
        _deny(can(_operator(), Action(name="project.admin"), _obj(), CTX), DenyReason.user_role_forbidden)

    # unknown verbs fall back to owner-only (fail-secure)
    def test_unknown_verb_allows_owner(self):
        _allow(can(_owner(), Action(name="project.super_secret_verb"), _obj(), CTX))

    def test_unknown_verb_denies_operator(self):
        _deny(can(_operator(), Action(name="project.super_secret_verb"), _obj(), CTX), DenyReason.user_role_forbidden)


# ── Project scope checks ───────────────────────────────────────────────────────


class TestProjectScope:
    def test_agent_without_project_scope_is_skipped(self):
        agent = _agent(project_scope=None)
        _allow(can(agent, Action(name="project.read"), _obj(project_id=PROJ_OUTSIDE), CTX))

    def test_agent_project_in_scope_is_allowed(self):
        agent = _agent(project_scope=[str(PROJ_A), str(PROJ_B)])
        _allow(can(agent, Action(name="project.read"), _obj(project_id=PROJ_A), CTX))

    def test_agent_project_out_of_scope_is_denied(self):
        agent = _agent(project_scope=[str(PROJ_A)])
        result = can(agent, Action(name="project.read"), _obj(project_id=PROJ_B), CTX)
        _deny(result, DenyReason.project_out_of_scope)
        assert str(PROJ_B) in result.message

    def test_empty_project_scope_denies_all(self):
        """Empty list means explicitly nothing allowed."""
        agent = _agent(project_scope=[])
        _deny(can(agent, Action(name="project.read"), _obj(project_id=PROJ_A), CTX), DenyReason.project_out_of_scope)

    def test_object_without_project_id_is_allowed(self):
        """Objects with no project_id (e.g. global resources) bypass project scope.

        Capability scope is cleared so only the project-scope check is exercised.
        """
        agent = _agent(project_scope=[str(PROJ_A)], capability_scope=None)
        _allow(can(agent, Action(name="read"), _obj(project_id=None, object_type="system"), CTX))


# ── Capability scope checks ────────────────────────────────────────────────────


class TestCapabilityScope:
    def test_exact_capability_match_is_allowed(self):
        agent = _agent(capability_scope=["project.create", "project.read"])
        _allow(can(agent, Action(name="project.create"), _obj(), CTX))

    def test_capability_out_of_scope_is_denied(self):
        agent = _agent(capability_scope=["project.read"])
        _deny(can(agent, Action(name="project.delete"), _obj(), CTX), DenyReason.capability_out_of_scope)

    def test_prefix_wildcard_match_is_allowed(self):
        agent = _agent(capability_scope=["memory.*"])
        _allow(can(agent, Action(name="memory.write"), _obj(object_type="memory"), CTX))
        _allow(can(agent, Action(name="memory.read"), _obj(object_type="memory"), CTX))

    def test_prefix_dot_match_is_allowed(self):
        agent = _agent(capability_scope=["memory."])
        _allow(can(agent, Action(name="memory.write"), _obj(object_type="memory"), CTX))

    def test_empty_capability_scope_denies_all(self):
        agent = _agent(capability_scope=[])
        _deny(can(agent, Action(name="project.read"), _obj(), CTX), DenyReason.capability_out_of_scope)

    def test_no_capability_scope_is_skipped(self):
        agent = _agent(capability_scope=None)
        _allow(can(agent, Action(name="any.action"), _obj(), CTX))


# ── Sensitivity ceiling checks ─────────────────────────────────────────────────


class TestSensitivityCeiling:
    def test_object_at_or_below_ceiling_is_allowed(self):
        for ceiling, obj_level in [("normal", "public"), ("normal", "normal"), ("sensitive", "normal"), ("sensitive", "private")]:
            agent = _agent(sensitivity_ceiling=ceiling, project_scope=None, capability_scope=None)
            _allow(can(agent, Action(name="memory.read"), _obj(object_type="memory", sensitivity_level=obj_level), CTX))

    def test_object_above_ceiling_is_denied(self):
        agent = _agent(sensitivity_ceiling="normal", project_scope=None, capability_scope=None)
        _deny(
            can(agent, Action(name="memory.read"), _obj(object_type="memory", sensitivity_level="secret"), CTX),
            DenyReason.sensitivity_ceiling_exceeded,
        )

    def test_object_without_sensitivity_is_allowed(self):
        agent = _agent(sensitivity_ceiling="normal", project_scope=None, capability_scope=None)
        _allow(can(agent, Action(name="memory.read"), _obj(sensitivity_level=None), CTX))

    def test_unknown_sensitivity_on_object_is_denied(self):
        """Fail-secure: unknown sensitivity labels are treated as most sensitive."""
        agent = _agent(sensitivity_ceiling="normal", project_scope=None, capability_scope=None)
        result = can(agent, Action(name="memory.read"), _obj(sensitivity_level="top_secret_unknown"), CTX)
        assert result.decision == Decision.deny


# ── Step-up checks ─────────────────────────────────────────────────────────────


class TestStepUp:
    def test_action_without_step_up_is_allowed(self):
        """Actions not flagged requires_step_up must not trigger step-up checks."""
        _allow(can(_owner(step_up_verified=False), Action(name="project.read", requires_step_up=False), _obj(), CTX))

    def test_action_requiring_step_up_without_verification_returns_step_up_required(self):
        _step_up(
            can(_owner(step_up_verified=False), Action(name="admin.config", requires_step_up=True), _obj(), CTX),
            DenyReason.user_step_up_required,
        )

    def test_action_requiring_step_up_with_verification_is_allowed(self):
        _allow(can(_owner(step_up_verified=True), Action(name="admin.config", requires_step_up=True), _obj(), CTX))

    def test_agent_step_up_returns_step_up_expired(self):
        """Agent actors use DenyReason.step_up_expired for step-up gating."""
        agent = Actor(
            actor_type="agent",
            actor_id=AGENT_ID,
            status="active",
            step_up_verified=False,
            sensitivity_ceiling="normal",
        )
        result = can(agent, Action(name="admin.config", requires_step_up=True), _obj(), CTX)
        _step_up(result, DenyReason.step_up_expired)


# ── Review policy checks ───────────────────────────────────────────────────────


class TestReviewPolicy:
    def test_delete_normal_object_is_allowed(self):
        """Deleting a normal-sensitivity object should NOT trigger review."""
        _allow(can(_owner(), Action(name="project.delete"), _obj(sensitivity_level="normal"), CTX))

    def test_delete_sensitive_object_triggers_review(self):
        _review(
            can(_owner(), Action(name="project.delete"), _obj(sensitivity_level="sensitive"), CTX),
            DenyReason.review_policy_triggered,
        )

    def test_admin_secret_object_triggers_review(self):
        _review(
            can(_owner(), Action(name="project.admin"), _obj(sensitivity_level="secret"), CTX),
            DenyReason.review_policy_triggered,
        )

    def test_revoke_sensitive_triggers_review(self):
        _review(
            can(_owner(), Action(name="agent_token.revoke"), _obj(object_type="agent_token", sensitivity_level="sensitive"), CTX),
            DenyReason.review_policy_triggered,
        )

    def test_review_prefixed_action_triggers_review(self):
        _review(
            can(_owner(), Action(name="review.something"), _obj(sensitivity_level="normal"), CTX),
            DenyReason.review_policy_triggered,
        )

    def test_read_sensitive_object_does_not_trigger_review(self):
        """Read operations on sensitive objects should NOT trigger review."""
        _allow(can(_owner(), Action(name="project.read"), _obj(sensitivity_level="secret"), CTX))


# ── Short-circuit behavior ─────────────────────────────────────────────────────


class TestShortCircuit:
    """The pipeline must stop at the first non-allow decision.

    This prevents attackers from probing internal policy rules by observing
    different deny reasons.
    """

    def test_disabled_user_short_circuits_before_rbac(self):
        actor = actor_from_user_session(user_id=OWNER_ID, role="owner", status="disabled")
        result = can(actor, Action(name="project.delete"), _obj(sensitivity_level="secret"), CTX)
        _deny(result, DenyReason.user_disabled)
        # If it hadn't short-circuited we'd see review_policy_triggered

    def test_viewer_short_circuits_before_project_scope(self):
        """A viewer user is denied by RBAC before agent-specific checks run."""
        # This test confirms that the viewer_factory creates a user actor
        # whose RBAC denial appears before project/capability checks in the pipeline.
        actor = _viewer()
        assert actor.actor_type == "user"
        result = can(actor, Action(name="project.create"), _obj(), CTX)
        _deny(result, DenyReason.user_role_forbidden)


# ── System actor ───────────────────────────────────────────────────────────────


class TestSystemActor:
    def test_system_actor_is_always_allowed(self):
        _allow(can(actor_system(), Action(name="any.verb"), _obj(), CTX))

    def test_system_actor_has_no_actor_id(self):
        assert actor_system().actor_id is None

    def test_system_actor_type_is_system(self):
        assert actor_system().actor_type == "system"


# ── Factory helpers ────────────────────────────────────────────────────────────


class TestActorFactories:
    def test_actor_from_user_session_sets_correct_fields(self):
        sid = uuid4()
        actor = actor_from_user_session(
            user_id=OWNER_ID,
            role="owner",
            status="active",
            step_up_verified=True,
            session_id=sid,
        )
        assert actor.actor_type == "user"
        assert actor.actor_id == OWNER_ID
        assert actor.role == "owner"
        assert actor.status == "active"
        assert actor.step_up_verified is True
        assert actor.auth_context_type == "user_session"
        assert actor.auth_context_id == sid

    def test_actor_from_agent_token_sets_correct_fields(self):
        tid = uuid4()
        actor = actor_from_agent_token(
            agent_id=AGENT_ID,
            status="active",
            sensitivity_ceiling="secret",
            project_scope=["proj-x"],
            capability_scope=["memory.read"],
            token_id=tid,
        )
        assert actor.actor_type == "agent"
        assert actor.actor_id == AGENT_ID
        assert actor.status == "active"
        assert actor.sensitivity_ceiling == "secret"
        assert actor.project_scope == ["proj-x"]
        assert actor.capability_scope == ["memory.read"]
        assert actor.auth_context_type == "agent_token"
        assert actor.auth_context_id == tid

    def test_actor_from_user_session_defaults_step_up_to_false(self):
        actor = actor_from_user_session(user_id=OWNER_ID, role="operator", status="active")
        assert actor.step_up_verified is False

    def test_actor_from_agent_token_defaults_scopes_to_none(self):
        actor = actor_from_agent_token(agent_id=AGENT_ID, status="active", sensitivity_ceiling="normal")
        assert actor.project_scope is None
        assert actor.capability_scope is None


# ── PolicyDecision helper methods ──────────────────────────────────────────────


class TestPolicyDecisionHelpers:
    def test_allow_factory(self):
        d = PolicyDecision.allow()
        assert d.decision == Decision.allow
        assert d.deny_reason is None
        assert d.message is None

    def test_deny_factory(self):
        d = PolicyDecision.deny(DenyReason.user_disabled, "disabled", status="disabled")
        assert d.decision == Decision.deny
        assert d.deny_reason == DenyReason.user_disabled
        assert d.message == "disabled"
        assert d.details == {"status": "disabled"}

    def test_review_factory(self):
        d = PolicyDecision.review(DenyReason.review_policy_triggered, "needs review", level="secret")
        assert d.decision == Decision.review_required
        assert d.deny_reason == DenyReason.review_policy_triggered

    def test_step_up_factory(self):
        d = PolicyDecision.step_up(DenyReason.user_step_up_required, "step up please")
        assert d.decision == Decision.step_up_required
        assert d.deny_reason == DenyReason.user_step_up_required


# ── Pydantic schema conversion ─────────────────────────────────────────────────


class TestPolicyDecisionSchema:
    def test_allow_to_schema(self):
        pd = PolicyDecision.allow()
        schema = PolicyDecisionRead.from_policy_decision(pd)
        assert schema.decision == SchemaDecision.allow
        assert schema.deny_reason is None
        assert schema.message is None

    def test_deny_to_schema(self):
        pd = PolicyDecision.deny(DenyReason.user_role_forbidden, "no access", role="viewer")
        schema = PolicyDecisionRead.from_policy_decision(pd)
        assert schema.decision == SchemaDecision.deny
        assert schema.deny_reason == SchemaDenyReason.user_role_forbidden
        assert "no access" in schema.message
        assert schema.details["role"] == "viewer"

    def test_schema_deny_reason_enum_matches_dataclass_enum(self):
        """Contract: Pydantic DenyReason enum must mirror dataclass DenyReason."""
        dataclass_values = {r.value for r in DenyReason}
        schema_values = {r.value for r in SchemaDenyReason}
        assert dataclass_values == schema_values

    def test_schema_decision_enum_matches_dataclass_enum(self):
        dataclass_values = {d.value for d in Decision}
        schema_values = {d.value for d in SchemaDecision}
        assert dataclass_values == schema_values

    def test_schema_json_serializable(self):
        pd = PolicyDecision.deny(DenyReason.user_disabled, "user is disabled", status="disabled")
        schema = PolicyDecisionRead.from_policy_decision(pd)
        json_str = schema.model_dump_json()
        assert "user_disabled" in json_str
        assert "disabled" in json_str

    def test_schema_generates_openapi_ref(self):
        """Ensure the schema can be referenced in OpenAPI generation."""
        ref = PolicyDecisionRead.model_json_schema()
        assert ref["type"] == "object"
        assert "decision" in ref["properties"]
        assert "deny_reason" in ref["properties"]


# ── Edge cases ─────────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_action_with_dotted_name_splits_correctly(self):
        """'project.create' -> verb='create'."""
        # Already tested implicitly via RBAC; explicit check just in case.
        _allow(can(_owner(), Action(name="project.create"), _obj(), CTX))
        _allow(can(_operator(), Action(name="project.create"), _obj(), CTX))

    def test_action_with_single_segment(self):
        """'read' (no dot) -> verb='read'."""
        _allow(can(_owner(), Action(name="read"), _obj(), CTX))

    def test_action_with_multiple_dots(self):
        """'a.b.c.d.e' -> verb='e'."""
        _allow(can(_owner(), Action(name="a.b.c.d.read"), _obj(), CTX))

    def test_object_with_owner_user_id_does_not_affect_policy(self):
        """owner_user_id on Object is metadata; policy engine ignores it."""
        _allow(can(_owner(), Action(name="project.read"), _obj(owner_user_id=VIEWER_ID), CTX))

    def test_context_is_optional(self):
        """can() must work with and without a PolicyContext."""
        assert can(_owner(), Action(name="read"), _obj()).decision == Decision.allow
        assert can(_owner(), Action(name="read"), _obj(), PolicyContext()).decision == Decision.allow

    def test_actor_extra_field_does_not_affect_policy(self):
        actor = Actor(actor_type="user", actor_id=OWNER_ID, role="owner", status="active", extra={"custom": "data"})
        _allow(can(actor, Action(name="read"), _obj(), CTX))

    def test_object_extra_field_does_not_affect_policy(self):
        _allow(can(_owner(), Action(name="read"), Object(object_type="project", extra={"custom": 123}), CTX))


# ── User / Agent differential contract ─────────────────────────────────────────


class TestUserAgentDifferential:
    """Phase 1 gate: user and agent deny reasons must be distinguishable."""

    def test_disabled_user_vs_disabled_agent_give_different_reasons(self):
        user_actor = actor_from_user_session(user_id=OWNER_ID, role="owner", status="disabled")
        agent_actor = actor_from_agent_token(agent_id=AGENT_ID, status="disabled", sensitivity_ceiling="normal")

        user_result = can(user_actor, Action(name="read"), _obj(), CTX)
        agent_result = can(agent_actor, Action(name="read"), _obj(), CTX)

        assert user_result.deny_reason != agent_result.deny_reason
        assert user_result.deny_reason == DenyReason.user_disabled
        assert agent_result.deny_reason == DenyReason.agent_disabled

    def test_user_locked_vs_agent_archived_give_different_reasons(self):
        user_actor = actor_from_user_session(user_id=OWNER_ID, role="owner", status="locked")
        agent_actor = actor_from_agent_token(agent_id=AGENT_ID, status="archived", sensitivity_ceiling="normal")

        user_result = can(user_actor, Action(name="read"), _obj(), CTX)
        agent_result = can(agent_actor, Action(name="read"), _obj(), CTX)

        assert user_result.deny_reason == DenyReason.user_locked
        assert agent_result.deny_reason == DenyReason.agent_archived

    def test_user_deny_reasons_never_start_with_agent_prefix(self):
        """Contract: user-originated deny reasons must not use 'agent_' prefix."""
        user_actor = actor_from_user_session(user_id=OWNER_ID, role="viewer", status="active")
        result = can(user_actor, Action(name="project.create"), _obj(), CTX)
        # RBAC deny for user
        assert not result.deny_reason.value.startswith("agent_")

    def test_agent_deny_reasons_never_start_with_user_prefix(self):
        """Contract: agent-originated deny reasons must not use 'user_' prefix."""
        agent_actor = actor_from_agent_token(agent_id=AGENT_ID, status="disabled", sensitivity_ceiling="normal")
        result = can(agent_actor, Action(name="read"), _obj(), CTX)
        # Status deny for agent
        assert not result.deny_reason.value.startswith("user_")
