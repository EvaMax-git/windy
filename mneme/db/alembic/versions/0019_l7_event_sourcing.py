"""L7 Event Sourcing + Federation + Graph Triggers DDL.

Creates:
1. ``event_log`` — append-only domain event store for event sourcing.
2. ``federation_nodes`` — peer registry for multi-instance sync.
3. ``sync_queue`` — outbound/inbound sync queue for federation protocol.
4. ``graph_trigger_log`` — audit trail for automatic graph-trigger actions.
5. SQL triggers on ``memories`` that auto-maintain ``graph_nodes``.

Revision ID: 0019_l7_event_sourcing
Revises: 0018_sub_library_add_columns
Create Date: 2026-05-08

Rationale
---------
event_log
   Separate from ``events`` (which is an outbox for pub/sub dispatch).
   ``event_log`` is an **append-only domain event store** — every state-changing
   operation in the system writes a row here for replay, audit, and sync.

federation_nodes + sync_queue
   Pre-wire the federation / multi-instance sync protocol.
   ``federation_nodes`` registers peer instances; ``sync_queue`` holds
   pending outbound events and received inbound events for conflict resolution.

graph_trigger_log + memory graph triggers
   Automatic dependency-graph maintenance: when a memory is inserted, updated,
   or deleted, PostgreSQL triggers upsert/archive the corresponding
   ``graph_nodes`` row so the graph layer stays consistent without application
   code.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019_l7_event_sourcing"
down_revision: str | Sequence[str] | None = "0018_sub_library_add_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ═══════════════════════════════════════════════════════════════════════
# Upgrades
# ═══════════════════════════════════════════════════════════════════════

_EVENT_LOG_SQL = r"""
CREATE TABLE event_log (
    log_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    stream_type     varchar(80) NOT NULL,
    stream_id       uuid NOT NULL,
    stream_version  bigint NOT NULL,
    event_type      varchar(120) NOT NULL,
    correlation_id  uuid,
    causation_id    uuid,
    actor_type      varchar(40),
    actor_id        uuid,
    payload_json    jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata_json   jsonb NOT NULL DEFAULT '{}'::jsonb,
    committed_at    timestamptz NOT NULL DEFAULT now(),
    project_id      uuid,

    CHECK (stream_type IN (
        'memory', 'conversation', 'message', 'knowledge_document',
        'knowledge_chunk', 'asset', 'agent', 'project'
    )),

    UNIQUE (stream_type, stream_id, stream_version)
);

CREATE INDEX idx_event_log_stream
    ON event_log(stream_type, stream_id, stream_version);

CREATE INDEX idx_event_log_committed
    ON event_log(committed_at DESC);

CREATE INDEX idx_event_log_project
    ON event_log(project_id)
    WHERE project_id IS NOT NULL;

CREATE INDEX idx_event_log_correlation
    ON event_log(correlation_id)
    WHERE correlation_id IS NOT NULL;
"""

_FEDERATION_SQL = r"""
CREATE TABLE federation_nodes (
    node_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    node_code       varchar(64) NOT NULL UNIQUE,
    display_name    varchar(200) NOT NULL,
    instance_url    varchar(512) NOT NULL,
    public_key      text,
    api_version     varchar(24) NOT NULL DEFAULT '1.0',
    node_status     varchar(24) NOT NULL DEFAULT 'active',
    sync_role       varchar(24) NOT NULL DEFAULT 'peer',
    heartbeat_at    timestamptz,
    last_sync_at    timestamptz,
    config_json     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CHECK (node_status IN ('active', 'paused', 'inactive', 'revoked')),
    CHECK (sync_role IN ('leader', 'peer', 'readonly'))
);

CREATE TABLE sync_queue (
    sync_queue_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    direction       varchar(12) NOT NULL,
    node_id         uuid NOT NULL,
    stream_type     varchar(80) NOT NULL,
    stream_id       uuid NOT NULL,
    stream_version  bigint NOT NULL,
    event_type      varchar(120) NOT NULL,
    payload_json    jsonb NOT NULL DEFAULT '{}'::jsonb,
    sync_status     varchar(24) NOT NULL DEFAULT 'pending',
    attempt_count   integer NOT NULL DEFAULT 0,
    last_error      text,
    locked_until    timestamptz,
    enqueued_at     timestamptz NOT NULL DEFAULT now(),
    synced_at       timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CHECK (direction IN ('outbound', 'inbound')),
    CHECK (sync_status IN (
        'pending', 'syncing', 'confirmed', 'conflict',
        'failed', 'skipped', 'cancelled'
    ))
);

CREATE INDEX idx_sync_queue_pending
    ON sync_queue(direction, sync_status, enqueued_at)
    WHERE sync_status = 'pending';

CREATE INDEX idx_sync_queue_node
    ON sync_queue(node_id, direction, sync_status);

ALTER TABLE sync_queue
    ADD CONSTRAINT fk_sync_queue_node
    FOREIGN KEY (node_id) REFERENCES federation_nodes(node_id)
    ON DELETE CASCADE;
"""

_GRAPH_TRIGGER_SQL = r"""
-- Track auto-graph-trigger actions for observability
CREATE TABLE graph_trigger_log (
    trigger_log_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    trigger_event   varchar(24) NOT NULL,
    memory_id       uuid NOT NULL,
    node_id         uuid,
    edge_id         uuid,
    action          varchar(40) NOT NULL,
    details_json    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),

    CHECK (trigger_event IN ('insert', 'update', 'delete', 'restore')),
    CHECK (action IN (
        'node_created', 'node_updated', 'node_archived',
        'node_restored', 'node_deleted',
        'edge_created', 'edge_updated', 'edge_cancelled',
        'skipped_no_change', 'error'
    ))
);

CREATE INDEX idx_graph_trigger_log_memory
    ON graph_trigger_log(memory_id, created_at DESC);

CREATE INDEX idx_graph_trigger_log_event
    ON graph_trigger_log(trigger_event, created_at DESC);

-- Helper function: sync a memory row to graph_nodes
CREATE OR REPLACE FUNCTION fn_sync_memory_to_graph()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_node_id       uuid;
    v_content_hash  varchar(128);
    v_properties    jsonb;
    v_operation     varchar(40);
    v_event         varchar(24);
    v_sensitivity   varchar(24);
BEGIN
    -- Determine trigger event
    IF TG_OP = 'INSERT' THEN
        v_event := 'insert';
    ELSIF TG_OP = 'UPDATE' THEN
        v_event := 'update';
    ELSIF TG_OP = 'DELETE' THEN
        v_event := 'delete';
    ELSE
        v_event := 'update';
    END IF;

    -- Handle DELETE (OLD row) vs INSERT/UPDATE (NEW row)
    IF TG_OP = 'DELETE' THEN
        -- Archive the graph node rather than hard-delete
        UPDATE graph_nodes
        SET status = 'archived',
            properties_json = jsonb_set(
                properties_json,
                '{archived_via_trigger_at}',
                to_jsonb(now())
            )
        WHERE source_type = 'memory' AND source_id = OLD.memory_id
        RETURNING node_id INTO v_node_id;

        INSERT INTO graph_trigger_log (trigger_event, memory_id, node_id, action, details_json)
        VALUES (
            'delete',
            OLD.memory_id,
            v_node_id,
            'node_archived',
            jsonb_build_object('memory_canonical_key', OLD.canonical_key)
        );
        RETURN OLD;
    END IF;

    -- For INSERT and UPDATE — compute content_hash from canonical_key
    v_content_hash := encode(
        sha256(COALESCE(NEW.canonical_key, NEW.memory_id::text)::bytea),
        'hex'
    );

    -- Build properties payload
    v_properties := jsonb_build_object(
        'memory_title', NEW.title,
        'memory_status', NEW.status,
        'memory_decay_state', NEW.decay_state,
        'memory_decay_score', NEW.decay_score,
        'canonical_key', NEW.canonical_key
    );

    -- Resolve sensitivity
    v_sensitivity := COALESCE(NEW.sensitivity_level, 'normal');

    -- Upsert graph_nodes
    INSERT INTO graph_nodes (
        project_id, node_type, node_label, node_key,
        source_type, source_id, content_hash,
        properties_json, sensitivity_level, status
    )
    VALUES (
        NEW.project_id,
        'memory',
        COALESCE(NEW.title, 'Memory ' || substring(NEW.memory_id::text, 1, 8)),
        'memory_' || NEW.memory_id::text,
        'memory',
        NEW.memory_id,
        v_content_hash,
        v_properties,
        v_sensitivity,
        CASE WHEN NEW.status = 'active' THEN 'active'
             WHEN NEW.status = 'archived' THEN 'archived'
             ELSE 'active'
        END
    )
    ON CONFLICT (project_id, node_key)
    DO UPDATE SET
        node_label       = COALESCE(NEW.title, EXCLUDED.node_label),
        properties_json  = EXCLUDED.properties_json,
        sensitivity_level = EXCLUDED.sensitivity_level,
        status           = CASE
            WHEN NEW.status = 'active' THEN 'active'
            WHEN NEW.status = 'archived' THEN 'archived'
            ELSE EXCLUDED.status
        END,
        content_hash     = EXCLUDED.content_hash,
        updated_at       = now()
    RETURNING node_id INTO v_node_id;

    -- Determine action description
    IF TG_OP = 'INSERT' THEN
        v_operation := 'node_created';
    ELSIF TG_OP = 'UPDATE' AND OLD.status != NEW.status THEN
        v_operation := 'node_updated';
    ELSE
        v_operation := 'node_updated';
    END IF;

    -- Log trigger action
    INSERT INTO graph_trigger_log (trigger_event, memory_id, node_id, action, details_json)
    VALUES (
        v_event,
        NEW.memory_id,
        v_node_id,
        v_operation,
        jsonb_build_object(
            'memory_title', NEW.title,
            'memory_status', NEW.status,
            'node_status', 'active'
        )
    );

    RETURN NEW;
END;
$$;

-- Install the trigger on the memories table
DROP TRIGGER IF EXISTS trg_memory_sync_to_graph ON memories;
CREATE TRIGGER trg_memory_sync_to_graph
    AFTER INSERT OR UPDATE OF title, status, decay_state, decay_score, sensitivity_level
    ON memories
    FOR EACH ROW
    EXECUTE FUNCTION fn_sync_memory_to_graph();

DROP TRIGGER IF EXISTS trg_memory_delete_sync_to_graph ON memories;
CREATE TRIGGER trg_memory_delete_sync_to_graph
    BEFORE DELETE ON memories
    FOR EACH ROW
    EXECUTE FUNCTION fn_sync_memory_to_graph();
"""

# ═══════════════════════════════════════════════════════════════════════
# Downgrade SQL
# ═══════════════════════════════════════════════════════════════════════

_DOWNGRADE_SQL = r"""
DROP TRIGGER IF EXISTS trg_memory_delete_sync_to_graph ON memories;
DROP TRIGGER IF EXISTS trg_memory_sync_to_graph ON memories;
DROP FUNCTION IF EXISTS fn_sync_memory_to_graph();
DROP TABLE IF EXISTS graph_trigger_log CASCADE;
DROP TABLE IF EXISTS sync_queue CASCADE;
DROP TABLE IF EXISTS federation_nodes CASCADE;
DROP TABLE IF EXISTS event_log CASCADE;
"""


def upgrade() -> None:
    op.execute(_EVENT_LOG_SQL)
    op.execute(_FEDERATION_SQL)
    op.execute(_GRAPH_TRIGGER_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
