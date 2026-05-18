from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.db.audit import AuditEvent, OutboxEvent, add_audit_event, add_outbox_event
from mneme.db.transactions import transaction
from mneme.schemas.agents import (
    AgentCreateRequest,
    AgentRead,
    AgentTokenCreateRequest,
    AgentTokenRead,
    AgentUpdateRequest,
)
from mneme.security import (
    compute_agent_token_fingerprint,
    generate_agent_token,
    hash_agent_token,
)


@dataclass(frozen=True)
class AuthenticatedAgent:
    agent: AgentRead
    token: AgentTokenRead
    token_hash: str


@dataclass(frozen=True)
class CreateAgentTokenResult:
    token: AgentTokenRead
    token_secret: str


# ── agents CRUD ──────────────────────────────────────────────────────────────

_INSERT_AGENT = text(
    """
    INSERT INTO agents (
      agent_id,
      project_id,
      agent_code,
      name,
      description,
      status,
      owner_user_id,
      store_id,
      model_id,
      sensitivity_ceiling,
      policy_json
    )
    VALUES (
      :agent_id,
      :project_id,
      :agent_code,
      :name,
      :description,
      'active',
      :owner_user_id,
      :store_id,
      :model_id,
      :sensitivity_ceiling,
      :policy_json
    )
    RETURNING
      agent_id,
      project_id,
      agent_code,
      name,
      description,
      status,
      owner_user_id,
      store_id,
      model_id,
      sensitivity_ceiling,
      policy_json,
      created_at,
      updated_at,
      disabled_at
    """
).bindparams(
    bindparam("agent_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("owner_user_id", type_=PG_UUID(as_uuid=True)),
    bindparam("store_id", type_=PG_UUID(as_uuid=True)),
    bindparam("model_id", type_=PG_UUID(as_uuid=True)),
    bindparam("policy_json", type_=JSONB),
)

_SELECT_AGENT_BY_ID = text(
    """
    SELECT
      agent_id,
      project_id,
      agent_code,
      name,
      description,
      status,
      owner_user_id,
      store_id,
      model_id,
      sensitivity_ceiling,
      policy_json,
      created_at,
      updated_at,
      disabled_at
    FROM agents
    WHERE agent_id = :agent_id
    """
).bindparams(bindparam("agent_id", type_=PG_UUID(as_uuid=True)))

_SELECT_AGENT_BY_CODE = text(
    """
    SELECT
      agent_id,
      project_id,
      agent_code,
      name,
      description,
      status,
      owner_user_id,
      store_id,
      model_id,
      sensitivity_ceiling,
      policy_json,
      created_at,
      updated_at,
      disabled_at
    FROM agents
    WHERE agent_code = :agent_code
    LIMIT 1
    """
)

_LIST_AGENTS_COUNT = text("SELECT count(*) FROM agents WHERE status != 'archived'")

_LIST_AGENTS = text(
    """
    SELECT
      agent_id,
      project_id,
      agent_code,
      name,
      description,
      status,
      owner_user_id,
      store_id,
      model_id,
      sensitivity_ceiling,
      policy_json,
      created_at,
      updated_at,
      disabled_at
    FROM agents
    WHERE status != 'archived'
    ORDER BY created_at DESC
    LIMIT :page_size OFFSET :offset
    """
)

_UPDATE_AGENT = text(
    """
    UPDATE agents
    SET
      project_id = CASE WHEN :project_id_set THEN :project_id ELSE project_id END,
      store_id = CASE WHEN :store_id_set THEN :store_id ELSE store_id END,
      model_id = CASE WHEN :model_id_set THEN :model_id ELSE model_id END,
      name = CASE WHEN :name_set THEN :name ELSE name END,
      description = CASE WHEN :description_set THEN :description ELSE description END,
      sensitivity_ceiling = CASE
        WHEN :sensitivity_ceiling_set THEN :sensitivity_ceiling
        ELSE sensitivity_ceiling
      END,
      policy_json = CASE WHEN :policy_json_set THEN :policy_json ELSE policy_json END,
      updated_at = CURRENT_TIMESTAMP
    WHERE agent_id = :agent_id
      AND status != 'archived'
    RETURNING
      agent_id,
      project_id,
      agent_code,
      name,
      description,
      status,
      owner_user_id,
      store_id,
      model_id,
      sensitivity_ceiling,
      policy_json,
      created_at,
      updated_at,
      disabled_at
    """
).bindparams(
    bindparam("agent_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("store_id", type_=PG_UUID(as_uuid=True)),
    bindparam("model_id", type_=PG_UUID(as_uuid=True)),
    bindparam("policy_json", type_=JSONB),
)

_DISABLE_AGENT = text(
    """
    UPDATE agents
    SET status = 'disabled',
        disabled_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP
    WHERE agent_id = :agent_id
      AND status = 'active'
    RETURNING
      agent_id,
      project_id,
      agent_code,
      name,
      description,
      status,
      owner_user_id,
      store_id,
      model_id,
      sensitivity_ceiling,
      policy_json,
      created_at,
      updated_at,
      disabled_at
    """
).bindparams(bindparam("agent_id", type_=PG_UUID(as_uuid=True)))

_ARCHIVE_AGENT = text(
    """
    UPDATE agents
    SET status = 'archived',
        disabled_at = COALESCE(disabled_at, CURRENT_TIMESTAMP),
        updated_at = CURRENT_TIMESTAMP
    WHERE agent_id = :agent_id
      AND status != 'archived'
    RETURNING
      agent_id,
      project_id,
      agent_code,
      name,
      description,
      status,
      owner_user_id,
      store_id,
      model_id,
      sensitivity_ceiling,
      policy_json,
      created_at,
      updated_at,
      disabled_at
    """
).bindparams(bindparam("agent_id", type_=PG_UUID(as_uuid=True)))


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _agent_from_row(row: Any) -> AgentRead:
    data = dict(row._mapping)
    data["policy_json"] = _json_value(data.get("policy_json"), {})
    return AgentRead.model_validate(data)


def create_agent(
    db: Session,
    context: RequestContext,
    *,
    payload: AgentCreateRequest,
    owner_user_id: UUID,
) -> AgentRead:
    agent_id = uuid4()

    existing = db.execute(_SELECT_AGENT_BY_CODE, {"agent_code": payload.agent_code}).first()
    if existing is not None:
        raise ValueError(f"agent_code '{payload.agent_code}' already exists")

    idempotency_key = context.idempotency_key or str(uuid4())

    with transaction(db):
        row = db.execute(
            _INSERT_AGENT,
            {
                "agent_id": agent_id,
                "project_id": payload.project_id,
                "agent_code": payload.agent_code,
                "name": payload.name,
                "description": payload.description,
                "owner_user_id": owner_user_id,
                "store_id": payload.store_id,
                "model_id": payload.model_id,
                "sensitivity_ceiling": payload.sensitivity_ceiling.value,
                "policy_json": payload.policy_json,
            },
        ).one()

        add_audit_event(
            db,
            context,
            AuditEvent(
                action="agent.create",
                result="success",
                object_type="agent",
                object_id=agent_id,
                project_id=payload.project_id,
            ),
        )

        add_outbox_event(
            db,
            context,
            OutboxEvent(
                event_type="agent.created",
                aggregate_type="agent",
                aggregate_id=agent_id,
                aggregate_version=1,
                idempotency_key=idempotency_key,
                payload_json={"agent_code": payload.agent_code},
            ),
        )

    return _agent_from_row(row)


def get_agent(db: Session, agent_id: UUID) -> AgentRead | None:
    row = db.execute(_SELECT_AGENT_BY_ID, {"agent_id": agent_id}).first()
    if row is None:
        return None
    return _agent_from_row(row)


def list_agents(db: Session, *, page: int = 1, page_size: int = 50) -> tuple[list[AgentRead], int]:
    total = db.execute(_LIST_AGENTS_COUNT).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(_LIST_AGENTS, {"page_size": page_size, "offset": offset}).all()
    agents = [_agent_from_row(row) for row in rows]
    return agents, total


def update_agent(
    db: Session,
    context: RequestContext,
    *,
    agent_id: UUID,
    payload: AgentUpdateRequest,
) -> AgentRead:
    fields = payload.model_fields_set
    if not fields:
        raise ValueError("agent update payload is empty")
    if "name" in fields and payload.name is None:
        raise ValueError("name cannot be null")
    if "sensitivity_ceiling" in fields and payload.sensitivity_ceiling is None:
        raise ValueError("sensitivity_ceiling cannot be null")
    if "policy_json" in fields and payload.policy_json is None:
        raise ValueError("policy_json cannot be null")

    existing = get_agent(db, agent_id)
    if existing is None:
        raise ValueError(f"agent {agent_id} not found")
    if existing.status.value == "archived":
        raise ValueError(f"agent {agent_id} is archived")

    idempotency_key = context.idempotency_key or str(uuid4())
    changed_fields = payload.model_dump(mode="json", exclude_unset=True)

    with transaction(db):
        row = db.execute(
            _UPDATE_AGENT,
            {
                "agent_id": agent_id,
                "project_id_set": "project_id" in fields,
                "project_id": payload.project_id,
                "store_id_set": "store_id" in fields,
                "store_id": payload.store_id,
                "model_id_set": "model_id" in fields,
                "model_id": payload.model_id,
                "name_set": "name" in fields,
                "name": payload.name,
                "description_set": "description" in fields,
                "description": payload.description,
                "sensitivity_ceiling_set": "sensitivity_ceiling" in fields,
                "sensitivity_ceiling": (
                    payload.sensitivity_ceiling.value if payload.sensitivity_ceiling else None
                ),
                "policy_json_set": "policy_json" in fields,
                "policy_json": payload.policy_json,
            },
        ).first()
        if row is None:
            raise ValueError(f"agent {agent_id} cannot be updated")
        agent = _agent_from_row(row)

        add_audit_event(
            db,
            context,
            AuditEvent(
                action="agent.update",
                result="success",
                object_type="agent",
                object_id=agent_id,
                project_id=agent.project_id,
                diff_summary={"changed_fields": sorted(changed_fields.keys())},
            ),
        )

        add_outbox_event(
            db,
            context,
            OutboxEvent(
                event_type="agent.updated",
                aggregate_type="agent",
                aggregate_id=agent_id,
                aggregate_version=1,
                idempotency_key=idempotency_key,
                payload_json={
                    "agent_id": str(agent_id),
                    "agent_code": agent.agent_code,
                    "changed_fields": changed_fields,
                },
            ),
        )

    return agent


def disable_agent(
    db: Session,
    context: RequestContext,
    *,
    agent_id: UUID,
) -> AgentRead:
    existing = get_agent(db, agent_id)
    if existing is None:
        raise ValueError(f"agent {agent_id} not found")
    if existing.status.value != "active":
        raise ValueError(f"agent {agent_id} is not active (status={existing.status.value})")

    idempotency_key = context.idempotency_key or str(uuid4())

    with transaction(db):
        row = db.execute(_DISABLE_AGENT, {"agent_id": agent_id}).first()
        if row is None:
            raise ValueError(f"agent {agent_id} cannot be disabled")
        agent = _agent_from_row(row)

        add_audit_event(
            db,
            context,
            AuditEvent(
                action="agent.disable",
                result="success",
                object_type="agent",
                object_id=agent_id,
                project_id=agent.project_id,
                metadata_json={"previous_status": existing.status.value},
            ),
        )

        add_outbox_event(
            db,
            context,
            OutboxEvent(
                event_type="agent.disabled",
                aggregate_type="agent",
                aggregate_id=agent_id,
                aggregate_version=1,
                idempotency_key=idempotency_key,
                payload_json={
                    "agent_id": str(agent_id),
                    "agent_code": agent.agent_code,
                    "previous_status": existing.status.value,
                    "status": agent.status.value,
                },
            ),
        )

    return agent


def archive_agent(
    db: Session,
    context: RequestContext,
    *,
    agent_id: UUID,
) -> AgentRead:
    existing = get_agent(db, agent_id)
    if existing is None:
        raise ValueError(f"agent {agent_id} not found")
    if existing.status.value == "archived":
        raise ValueError(f"agent {agent_id} is already archived")

    idempotency_key = context.idempotency_key or str(uuid4())

    with transaction(db):
        row = db.execute(_ARCHIVE_AGENT, {"agent_id": agent_id}).first()
        if row is None:
            raise ValueError(f"agent {agent_id} cannot be archived")
        agent = _agent_from_row(row)

        add_audit_event(
            db,
            context,
            AuditEvent(
                action="agent.archive",
                result="success",
                object_type="agent",
                object_id=agent_id,
                project_id=agent.project_id,
                metadata_json={"previous_status": existing.status.value},
            ),
        )

        add_outbox_event(
            db,
            context,
            OutboxEvent(
                event_type="agent.archived",
                aggregate_type="agent",
                aggregate_id=agent_id,
                aggregate_version=1,
                idempotency_key=idempotency_key,
                payload_json={
                    "agent_id": str(agent_id),
                    "agent_code": agent.agent_code,
                    "previous_status": existing.status.value,
                    "status": agent.status.value,
                },
            ),
        )

    return agent


# ── agent_tokens ─────────────────────────────────────────────────────────────

_INSERT_AGENT_TOKEN = text(
    """
    INSERT INTO agent_tokens (
      token_id,
      agent_id,
      issued_by_user_id,
      name,
      token_hash,
      token_prefix,
      token_fingerprint,
      project_scope,
      capability_scope,
      sensitivity_ceiling,
      budget_limit_daily,
      rate_limit_per_min,
      expires_at
    )
    VALUES (
      :token_id,
      :agent_id,
      :issued_by_user_id,
      :name,
      :token_hash,
      :token_prefix,
      :token_fingerprint,
      :project_scope,
      :capability_scope,
      :sensitivity_ceiling,
      :budget_limit_daily,
      :rate_limit_per_min,
      :expires_at
    )
    RETURNING
      token_id,
      agent_id,
      issued_by_user_id,
      name,
      token_prefix,
      token_fingerprint,
      project_scope,
      capability_scope,
      sensitivity_ceiling,
      budget_limit_daily,
      rate_limit_per_min,
      expires_at,
      revoked_at,
      last_used_at,
      created_at,
      updated_at
    """
).bindparams(
    bindparam("token_id", type_=PG_UUID(as_uuid=True)),
    bindparam("agent_id", type_=PG_UUID(as_uuid=True)),
    bindparam("issued_by_user_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_scope", type_=JSONB),
    bindparam("capability_scope", type_=JSONB),
)

_AUTH_AGENT_TOKEN = text(
    """
    UPDATE agent_tokens
    SET last_used_at = :now
    WHERE token_hash = :token_hash
      AND revoked_at IS NULL
      AND expires_at > :now
      AND EXISTS (
        SELECT 1
        FROM agents
        WHERE agents.agent_id = agent_tokens.agent_id
          AND agents.status = 'active'
      )
    RETURNING
      token_id,
      agent_id,
      issued_by_user_id,
      name,
      token_prefix,
      token_fingerprint,
      project_scope,
      capability_scope,
      sensitivity_ceiling,
      budget_limit_daily,
      rate_limit_per_min,
      expires_at,
      revoked_at,
      last_used_at,
      created_at,
      updated_at
    """
)

_SELECT_AGENT_TOKENS = text(
    """
    SELECT
      token_id,
      agent_id,
      issued_by_user_id,
      name,
      token_prefix,
      token_fingerprint,
      project_scope,
      capability_scope,
      sensitivity_ceiling,
      budget_limit_daily,
      rate_limit_per_min,
      expires_at,
      revoked_at,
      last_used_at,
      created_at,
      updated_at
    FROM agent_tokens
    WHERE agent_id = :agent_id
    ORDER BY created_at DESC
    """
).bindparams(bindparam("agent_id", type_=PG_UUID(as_uuid=True)))

_REVOKE_AGENT_TOKEN = text(
    """
    UPDATE agent_tokens
    SET revoked_at = :revoked_at
    WHERE token_id = :token_id
      AND agent_id = :agent_id
      AND revoked_at IS NULL
    RETURNING revoked_at
    """
).bindparams(
    bindparam("token_id", type_=PG_UUID(as_uuid=True)),
    bindparam("agent_id", type_=PG_UUID(as_uuid=True)),
)


def _token_from_row(row: Any) -> AgentTokenRead:
    data = dict(row._mapping)
    data.pop("token_hash", None)
    project_scope = _json_value(data.get("project_scope"), [])
    capability_scope = _json_value(data.get("capability_scope"), [])
    data["project_scope"] = project_scope
    data["capability_scope"] = capability_scope
    # Compute unified scopes = project_scope + capability_scope
    data["scopes"] = project_scope + capability_scope
    return AgentTokenRead.model_validate(data)


def create_agent_token(
    db: Session,
    context: RequestContext,
    *,
    agent_id: UUID,
    payload: AgentTokenCreateRequest,
    issued_by_user_id: UUID,
) -> CreateAgentTokenResult:
    now = datetime.now(timezone.utc)
    if payload.expires_at <= now:
        raise ValueError("expires_at must be in the future")

    token_secret = generate_agent_token()
    token_id = uuid4()
    token_hash = hash_agent_token(token_secret)
    token_prefix = token_secret[:24]
    token_fingerprint = compute_agent_token_fingerprint(
        token_secret, str(agent_id), str(issued_by_user_id)
    )
    idempotency_key = context.idempotency_key or str(uuid4())

    with transaction(db):
        row = db.execute(
            _INSERT_AGENT_TOKEN,
            {
                "token_id": token_id,
                "agent_id": agent_id,
                "issued_by_user_id": issued_by_user_id,
                "name": payload.name,
                "token_hash": token_hash,
                "token_prefix": token_prefix,
                "token_fingerprint": token_fingerprint,
                "project_scope": payload.project_scope,
                "capability_scope": payload.capability_scope,
                "sensitivity_ceiling": payload.sensitivity_ceiling.value,
                "budget_limit_daily": payload.budget_limit_daily,
                "rate_limit_per_min": payload.rate_limit_per_min,
                "expires_at": payload.expires_at,
            },
        ).one()

        add_audit_event(
            db,
            context,
            AuditEvent(
                action="agent_token.create",
                result="success",
                object_type="agent_token",
                object_id=token_id,
                metadata_json={
                    "agent_id": str(agent_id),
                    "token_fingerprint": token_fingerprint,
                },
            ),
        )

        add_outbox_event(
            db,
            context,
            OutboxEvent(
                event_type="agent_token.created",
                aggregate_type="agent_token",
                aggregate_id=token_id,
                aggregate_version=1,
                idempotency_key=idempotency_key,
                payload_json={
                    "agent_id": str(agent_id),
                    "token_fingerprint": token_fingerprint,
                },
            ),
        )

    return CreateAgentTokenResult(
        token=_token_from_row(row),
        token_secret=token_secret,
    )


def authenticate_agent_token(db: Session, token: str) -> AuthenticatedAgent | None:
    now = datetime.now(timezone.utc)
    token_hash = hash_agent_token(token)

    with transaction(db):
        token_row = db.execute(
            _AUTH_AGENT_TOKEN,
            {"token_hash": token_hash, "now": now},
        ).first()
        if token_row is None:
            return None

        agent_row = db.execute(
            _SELECT_AGENT_BY_ID,
            {"agent_id": _as_uuid(token_row._mapping["agent_id"])},
        ).first()
        if agent_row is None:
            return None

    return AuthenticatedAgent(
        agent=_agent_from_row(agent_row),
        token=_token_from_row(token_row),
        token_hash=token_hash,
    )


def revoke_agent_token(
    db: Session,
    context: RequestContext,
    *,
    agent_id: UUID,
    token_id: UUID,
    revoke_reason: str,
) -> datetime | None:
    now = datetime.now(timezone.utc)
    idempotency_key = context.idempotency_key or str(uuid4())

    with transaction(db):
        revoked_at = db.execute(
            _REVOKE_AGENT_TOKEN,
            {
                "token_id": token_id,
                "agent_id": agent_id,
                "revoked_at": now,
            },
        ).scalar_one_or_none()

        if revoked_at is not None:
            add_audit_event(
                db,
                context,
                AuditEvent(
                    action="agent_token.revoke",
                    result="success",
                    object_type="agent_token",
                    object_id=token_id,
                    reason_code=revoke_reason,
                ),
            )

            add_outbox_event(
                db,
                context,
                OutboxEvent(
                    event_type="agent_token.revoked",
                    aggregate_type="agent_token",
                    aggregate_id=token_id,
                    aggregate_version=1,
                    idempotency_key=idempotency_key,
                    payload_json={
                        "agent_id": str(agent_id),
                        "revoke_reason": revoke_reason,
                    },
                ),
            )

    return revoked_at


def list_agent_tokens(db: Session, agent_id: UUID) -> list[AgentTokenRead]:
    rows = db.execute(_SELECT_AGENT_TOKENS, {"agent_id": agent_id}).all()
    return [_token_from_row(row) for row in rows]


def _as_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))
