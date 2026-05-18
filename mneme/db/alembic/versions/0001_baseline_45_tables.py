"""baseline schema for Mneme data model

Revision ID: 0001_baseline_45_tables
Revises:
Create Date: 2026-05-02

"""

from collections.abc import Sequence

from alembic import context, op


revision: str = "0001_baseline_45_tables"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


BASELINE_SQL = r"""
-- Extensions must be pre-created by superuser (pgcrypto, uuid-ossp, vector)
-- CREATE EXTENSION IF NOT EXISTS pgcrypto;
-- CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
-- CREATE EXTENSION IF NOT EXISTS vector;

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

CREATE TABLE projects (
  project_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_code varchar(64) NOT NULL UNIQUE,
  name varchar(200) NOT NULL,
  description text,
  status varchar(24) NOT NULL DEFAULT 'active',
  sensitivity_default varchar(24) NOT NULL DEFAULT 'normal',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz,
  CHECK (status IN ('active', 'archived', 'disabled')),
  CHECK (sensitivity_default IN ('public', 'normal', 'private', 'sensitive', 'secret'))
);

CREATE TABLE users (
  user_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  username varchar(80) NOT NULL UNIQUE,
  email varchar(255) UNIQUE,
  display_name varchar(120) NOT NULL,
  role_code varchar(24) NOT NULL,
  status varchar(32) NOT NULL DEFAULT 'pending_bootstrap',
  password_hash varchar(255) NOT NULL,
  mfa_mode varchar(32) NOT NULL DEFAULT 'none',
  locale varchar(32) NOT NULL DEFAULT 'zh-CN',
  timezone varchar(64) NOT NULL DEFAULT 'Asia/Shanghai',
  last_login_at timestamptz,
  disabled_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (role_code IN ('owner', 'operator', 'viewer', 'auditor')),
  CHECK (status IN ('pending_bootstrap', 'active', 'disabled', 'locked')),
  CHECK (mfa_mode IN ('none', 'totp', 'passkey', 'required_but_unbound'))
);

CREATE TABLE user_sessions (
  session_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  session_token_hash varchar(255) NOT NULL UNIQUE,
  session_token_prefix varchar(24) NOT NULL,
  auth_method varchar(32) NOT NULL DEFAULT 'password',
  device_label varchar(200),
  device_fingerprint varchar(128),
  ip_hash varchar(128),
  user_agent text,
  step_up_verified_at timestamptz,
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  revoke_reason varchar(64),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (auth_method IN ('password', 'password_totp', 'passkey', 'bootstrap'))
);

CREATE TABLE agents (
  agent_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid,
  agent_code varchar(64) NOT NULL UNIQUE,
  name varchar(200) NOT NULL,
  description text,
  status varchar(24) NOT NULL DEFAULT 'active',
  owner_user_id uuid,
  sensitivity_ceiling varchar(24) NOT NULL DEFAULT 'normal',
  policy_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  disabled_at timestamptz,
  CHECK (status IN ('active', 'disabled', 'archived')),
  CHECK (sensitivity_ceiling IN ('public', 'normal', 'private', 'sensitive', 'secret'))
);

CREATE TABLE agent_tokens (
  token_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL,
  issued_by_user_id uuid NOT NULL,
  token_hash varchar(255) NOT NULL UNIQUE,
  token_prefix varchar(24) NOT NULL,
  token_fingerprint varchar(128) NOT NULL,
  project_scope jsonb NOT NULL DEFAULT '[]'::jsonb,
  capability_scope jsonb NOT NULL DEFAULT '[]'::jsonb,
  sensitivity_ceiling varchar(24) NOT NULL DEFAULT 'normal',
  budget_limit_daily numeric(18,6),
  rate_limit_per_min integer,
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  last_used_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (sensitivity_ceiling IN ('public', 'normal', 'private', 'sensitive', 'secret'))
);

CREATE TABLE audit_events (
  audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  occurred_at timestamptz NOT NULL DEFAULT now(),
  actor_type varchar(24) NOT NULL,
  actor_id uuid,
  auth_context_type varchar(24),
  auth_context_id uuid,
  action varchar(120) NOT NULL,
  object_type varchar(80),
  object_id uuid,
  project_id uuid,
  result varchar(24) NOT NULL,
  reason_code varchar(80),
  sensitivity_level varchar(24) NOT NULL DEFAULT 'normal',
  correlation_id uuid NOT NULL,
  request_id uuid NOT NULL,
  review_item_id uuid,
  diff_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  CHECK (actor_type IN ('user', 'agent', 'service', 'system')),
  CHECK (auth_context_type IN ('user_session', 'agent_token', 'service_identity', 'system_job') OR auth_context_type IS NULL),
  CHECK (result IN ('success', 'denied', 'failed')),
  CHECK (sensitivity_level IN ('public', 'normal', 'private', 'sensitive', 'secret'))
);

CREATE TABLE events (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_type varchar(120) NOT NULL,
  aggregate_type varchar(80) NOT NULL,
  aggregate_id uuid NOT NULL,
  aggregate_version bigint NOT NULL,
  correlation_id uuid NOT NULL,
  causation_id uuid,
  idempotency_key varchar(255) NOT NULL,
  producer varchar(80) NOT NULL,
  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  visibility varchar(24) NOT NULL DEFAULT 'internal',
  publish_state varchar(24) NOT NULL DEFAULT 'pending',
  occurred_at timestamptz NOT NULL,
  committed_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz,
  last_error text,
  CHECK (visibility IN ('internal', 'external', 'audit_only')),
  CHECK (publish_state IN ('pending', 'dispatched', 'delivered', 'failed', 'dead_letter')),
  UNIQUE (idempotency_key)
);

CREATE TABLE event_deliveries (
  delivery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id uuid NOT NULL,
  consumer_name varchar(120) NOT NULL,
  delivery_state varchar(24) NOT NULL DEFAULT 'pending',
  dispatch_attempts integer NOT NULL DEFAULT 0,
  last_dispatched_at timestamptz,
  acknowledged_at timestamptz,
  failed_at timestamptz,
  last_error text,
  lease_expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (delivery_state IN ('pending', 'dispatched', 'acknowledged', 'failed', 'dead_letter')),
  UNIQUE (event_id, consumer_name)
);

CREATE TABLE dead_letters (
  dead_letter_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_type varchar(24) NOT NULL,
  source_id uuid NOT NULL,
  related_event_id uuid,
  aggregate_type varchar(80),
  aggregate_id uuid,
  failure_class varchar(64) NOT NULL,
  error_code varchar(64),
  error_message text NOT NULL,
  retry_exhausted boolean NOT NULL DEFAULT false,
  external_effect_state varchar(32) NOT NULL DEFAULT 'none',
  replay_state varchar(24) NOT NULL DEFAULT 'pending',
  review_required boolean NOT NULL DEFAULT false,
  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  first_failed_at timestamptz NOT NULL DEFAULT now(),
  last_failed_at timestamptz NOT NULL DEFAULT now(),
  replayed_at timestamptz,
  resolved_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (source_type IN ('event_delivery', 'job', 'provider_call', 'importer')),
  CHECK (failure_class IN ('provider_transient_exhausted', 'policy_denied_terminal', 'payload_invalid', 'code_bug', 'external_side_effect_unknown')),
  CHECK (external_effect_state IN ('none', 'unknown', 'confirmed_done', 'confirmed_not_done')),
  CHECK (replay_state IN ('pending', 'under_review', 'replayed', 'cancelled', 'resolved'))
);

CREATE TABLE review_items (
  review_item_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid,
  review_type varchar(40) NOT NULL,
  target_type varchar(80) NOT NULL,
  target_id uuid NOT NULL,
  target_version bigint,
  status varchar(24) NOT NULL DEFAULT 'pending',
  priority integer NOT NULL DEFAULT 100,
  requester_actor_type varchar(24) NOT NULL,
  requester_actor_id uuid,
  reviewer_id uuid,
  decision varchar(24),
  reason text,
  decision_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  due_at timestamptz,
  decided_at timestamptz,
  expires_at timestamptz,
  correlation_id uuid NOT NULL,
  request_id uuid NOT NULL,
  idempotency_key varchar(255) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (review_type IN ('memory_candidate', 'sensitive_access', 'high_cost_call', 'import_confirm', 'restore_confirm', 'dlq_replay', 'manual')),
  CHECK (target_type IN ('memory_candidate', 'memory', 'asset', 'job', 'dead_letter', 'provider_call', 'credential', 'import_run', 'restore_run', 'project', 'user', 'agent', 'service', 'system', 'object_version', 'backup_run')),
  CHECK (status IN ('pending', 'in_review', 'approved', 'rejected', 'cancelled', 'expired')),
  CHECK (requester_actor_type IN ('user', 'agent', 'service', 'system')),
  CHECK (decision IN ('approved', 'rejected', 'cancelled', 'expired') OR decision IS NULL),
  CHECK (priority BETWEEN 0 AND 1000),
  UNIQUE (idempotency_key)
);

CREATE TABLE providers (
  provider_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider_code varchar(64) NOT NULL UNIQUE,
  name varchar(120) NOT NULL,
  provider_type varchar(32) NOT NULL,
  status varchar(24) NOT NULL DEFAULT 'active',
  endpoint_base text,
  config_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (provider_type IN ('llm', 'embedding', 'ocr', 'search', 'storage', 'webhook')),
  CHECK (status IN ('active', 'disabled', 'degraded'))
);

CREATE TABLE provider_models (
  provider_model_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider_id uuid NOT NULL,
  model_code varchar(120) NOT NULL,
  external_model_id varchar(160) NOT NULL,
  model_type varchar(32) NOT NULL,
  status varchar(24) NOT NULL DEFAULT 'active',
  display_name varchar(160),
  version_label varchar(80),
  context_window_tokens integer,
  max_input_tokens integer,
  max_output_tokens integer,
  input_price_per_1k numeric(18,8),
  output_price_per_1k numeric(18,8),
  currency_code varchar(8) NOT NULL DEFAULT 'USD',
  supports_streaming boolean NOT NULL DEFAULT false,
  supports_json_mode boolean NOT NULL DEFAULT false,
  supports_tools boolean NOT NULL DEFAULT false,
  supports_vision boolean NOT NULL DEFAULT false,
  sensitivity_ceiling varchar(24) NOT NULL DEFAULT 'private',
  config_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  deprecated_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (model_type IN ('chat', 'embedding', 'rerank', 'ocr', 'vision', 'audio', 'search', 'storage', 'custom_http')),
  CHECK (status IN ('active', 'disabled', 'degraded', 'deprecated')),
  CHECK (sensitivity_ceiling IN ('public', 'normal', 'private', 'sensitive', 'secret')),
  UNIQUE (provider_id, model_code),
  UNIQUE (provider_id, external_model_id)
);

CREATE TABLE capabilities (
  capability_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  capability_code varchar(64) NOT NULL UNIQUE,
  name varchar(120) NOT NULL,
  category varchar(32) NOT NULL,
  risk_level varchar(24) NOT NULL DEFAULT 'normal',
  default_budget_mode varchar(24) NOT NULL DEFAULT 'metered',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (category IN ('chat', 'embedding', 'ocr', 'rerank', 'search', 'export', 'admin')),
  CHECK (risk_level IN ('low', 'normal', 'high', 'critical')),
  CHECK (default_budget_mode IN ('free', 'metered', 'approval_required'))
);

CREATE TABLE credential_vault (
  credential_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider_id uuid NOT NULL,
  credential_name varchar(120) NOT NULL,
  credential_type varchar(24) NOT NULL,
  status varchar(24) NOT NULL DEFAULT 'active',
  ciphertext bytea NOT NULL,
  key_wrap bytea NOT NULL,
  key_version varchar(64) NOT NULL,
  fingerprint varchar(128) NOT NULL,
  scope_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  rotated_at timestamptz,
  last_used_at timestamptz,
  revoked_at timestamptz,
  created_by_user_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (credential_type IN ('api_key', 'oauth', 'cert', 'secret')),
  CHECK (status IN ('active', 'disabled', 'rotated', 'revoked')),
  UNIQUE (provider_id, credential_name)
);

CREATE TABLE capability_bindings (
  capability_binding_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  capability_id uuid NOT NULL,
  provider_id uuid NOT NULL,
  provider_model_id uuid,
  credential_id uuid,
  project_id uuid,
  binding_scope varchar(24) NOT NULL DEFAULT 'global',
  status varchar(24) NOT NULL DEFAULT 'active',
  priority integer NOT NULL DEFAULT 100,
  sensitivity_floor varchar(24) NOT NULL DEFAULT 'public',
  sensitivity_ceiling varchar(24) NOT NULL DEFAULT 'private',
  budget_mode varchar(24) NOT NULL DEFAULT 'metered',
  require_review boolean NOT NULL DEFAULT false,
  allow_streaming boolean NOT NULL DEFAULT true,
  timeout_seconds integer NOT NULL DEFAULT 120,
  rate_limit_key varchar(120),
  policy_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by_user_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (binding_scope IN ('global', 'project', 'sensitivity', 'project_sensitivity')),
  CHECK (status IN ('active', 'disabled', 'degraded', 'shadow')),
  CHECK (priority BETWEEN 0 AND 1000),
  CHECK (sensitivity_floor IN ('public', 'normal', 'private', 'sensitive', 'secret')),
  CHECK (sensitivity_ceiling IN ('public', 'normal', 'private', 'sensitive', 'secret')),
  CHECK (budget_mode IN ('free', 'metered', 'approval_required')),
  CHECK (timeout_seconds BETWEEN 1 AND 3600)
);

CREATE TABLE vault_access_logs (
  access_log_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  credential_id uuid,
  actor_type varchar(24) NOT NULL,
  actor_id uuid,
  auth_context_type varchar(24),
  auth_context_id uuid,
  action varchar(48) NOT NULL,
  result varchar(24) NOT NULL,
  capability_id uuid,
  provider_id uuid,
  request_id uuid,
  correlation_id uuid,
  reason_code varchar(80),
  target_scope jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  CHECK (actor_type IN ('user', 'agent', 'service', 'system')),
  CHECK (auth_context_type IN ('user_session', 'agent_token', 'service_identity', 'system_job') OR auth_context_type IS NULL),
  CHECK (action IN ('create', 'enable', 'disable', 'rotate', 'revoke', 'export', 'use', 'access_denied')),
  CHECK (result IN ('success', 'denied', 'failed'))
);

CREATE TABLE usage_limits (
  usage_limit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  subject_type varchar(24) NOT NULL,
  subject_id uuid NOT NULL,
  capability_id uuid,
  provider_id uuid,
  project_id uuid,
  limit_scope varchar(24) NOT NULL,
  window_unit varchar(16) NOT NULL,
  max_requests integer,
  max_input_tokens bigint,
  max_output_tokens bigint,
  max_total_tokens bigint,
  max_cost numeric(18,6),
  approval_threshold_cost numeric(18,6),
  block_threshold_cost numeric(18,6),
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (subject_type IN ('user', 'agent', 'project', 'capability', 'provider')),
  CHECK (limit_scope IN ('global', 'provider', 'capability', 'project')),
  CHECK (window_unit IN ('minute', 'hour', 'day', 'month'))
);

CREATE TABLE budget_tracking (
  budget_tracking_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id uuid NOT NULL,
  correlation_id uuid NOT NULL,
  subject_type varchar(24) NOT NULL,
  subject_id uuid NOT NULL,
  capability_id uuid,
  provider_id uuid,
  project_id uuid,
  reservation_state varchar(24) NOT NULL,
  currency_code varchar(8) NOT NULL DEFAULT 'USD',
  estimated_input_tokens bigint,
  estimated_output_tokens bigint,
  actual_input_tokens bigint,
  actual_output_tokens bigint,
  reserved_cost numeric(18,6) NOT NULL DEFAULT 0,
  committed_cost numeric(18,6) NOT NULL DEFAULT 0,
  released_cost numeric(18,6) NOT NULL DEFAULT 0,
  denied_reason varchar(80),
  provider_request_fingerprint varchar(128),
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (subject_type IN ('user', 'agent', 'project', 'system')),
  CHECK (reservation_state IN ('reserved', 'committed', 'released', 'denied', 'refunded'))
);

CREATE TABLE api_call_logs (
  api_call_log_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id uuid NOT NULL,
  correlation_id uuid NOT NULL,
  idempotency_key varchar(255) NOT NULL UNIQUE,
  project_id uuid,
  actor_type varchar(24) NOT NULL,
  actor_id uuid,
  auth_context_type varchar(24),
  auth_context_id uuid,
  capability_id uuid NOT NULL,
  capability_binding_id uuid,
  provider_id uuid NOT NULL,
  provider_model_id uuid,
  credential_id uuid,
  vault_access_log_id uuid,
  budget_tracking_id uuid,
  review_item_id uuid,
  event_id uuid,
  call_type varchar(32) NOT NULL,
  call_state varchar(24) NOT NULL DEFAULT 'planned',
  external_request_id varchar(255),
  provider_request_fingerprint varchar(128) NOT NULL,
  request_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
  response_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
  input_tokens bigint,
  output_tokens bigint,
  total_tokens bigint,
  estimated_cost numeric(18,6),
  actual_cost numeric(18,6),
  currency_code varchar(8) NOT NULL DEFAULT 'USD',
  latency_ms integer,
  retry_count integer NOT NULL DEFAULT 0,
  error_code varchar(80),
  error_message text,
  retention_until timestamptz NOT NULL DEFAULT (now() + interval '180 days'),
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (actor_type IN ('user', 'agent', 'service', 'system')),
  CHECK (auth_context_type IN ('user_session', 'agent_token', 'service_identity', 'system_job') OR auth_context_type IS NULL),
  CHECK (call_type IN ('chat', 'embedding', 'rerank', 'ocr', 'vision', 'audio', 'search', 'storage', 'custom_http')),
  CHECK (call_state IN ('planned', 'budget_reserved', 'credential_checked', 'in_flight', 'succeeded', 'failed', 'cancelled', 'denied', 'timeout', 'dead_letter'))
);

CREATE TABLE jobs (
  job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid,
  job_key varchar(255) NOT NULL UNIQUE,
  job_type varchar(80) NOT NULL,
  status varchar(24) NOT NULL DEFAULT 'pending',
  priority integer NOT NULL DEFAULT 100,
  queue_name varchar(80) NOT NULL DEFAULT 'default',
  scheduled_at timestamptz NOT NULL DEFAULT now(),
  available_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  lease_owner varchar(120),
  lease_expires_at timestamptz,
  idempotency_key varchar(255) NOT NULL UNIQUE,
  retry_count integer NOT NULL DEFAULT 0,
  max_retries integer NOT NULL DEFAULT 3,
  timeout_seconds integer NOT NULL DEFAULT 900,
  cause_event_id uuid,
  aggregate_type varchar(80),
  aggregate_id uuid,
  target_version bigint,
  input jsonb NOT NULL DEFAULT '{}'::jsonb,
  output jsonb NOT NULL DEFAULT '{}'::jsonb,
  error jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_error text,
  created_by_actor_type varchar(24) NOT NULL DEFAULT 'system',
  created_by_actor_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (job_type IN ('asset_import_pipeline', 'asset_extract_metadata', 'knowledge_index_refresh', 'memory_extract', 'memory_candidate_review', 'provider_call', 'backup', 'restore', 'importer', 'dlq_replay', 'maintenance')),
  CHECK (status IN ('pending', 'scheduled', 'running', 'succeeded', 'failed', 'retrying', 'cancelled', 'dead_letter', 'superseded')),
  CHECK (created_by_actor_type IN ('user', 'agent', 'service', 'system'))
);

CREATE TABLE job_logs (
  job_log_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id uuid NOT NULL,
  step varchar(120) NOT NULL,
  level varchar(16) NOT NULL DEFAULT 'info',
  message text NOT NULL,
  attempt_no integer NOT NULL DEFAULT 0,
  event_id uuid,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  CHECK (level IN ('debug', 'info', 'warning', 'error', 'critical'))
);

CREATE TABLE pipeline_defs (
  pipeline_def_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid,
  pipeline_code varchar(80) NOT NULL,
  pipeline_type varchar(40) NOT NULL,
  version bigint NOT NULL DEFAULT 1,
  name varchar(160) NOT NULL,
  description text,
  config_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  status varchar(24) NOT NULL DEFAULT 'active',
  created_by_user_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (pipeline_type IN ('asset_import', 'knowledge_index', 'memory_extract', 'backup', 'restore', 'importer', 'maintenance')),
  CHECK (status IN ('draft', 'active', 'disabled', 'archived')),
  UNIQUE (project_id, pipeline_code, version)
);

CREATE TABLE pipeline_runs (
  pipeline_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pipeline_def_id uuid NOT NULL,
  project_id uuid,
  root_job_id uuid,
  trigger_type varchar(32) NOT NULL,
  trigger_event_id uuid,
  target_type varchar(80),
  target_id uuid,
  target_version bigint,
  status varchar(24) NOT NULL DEFAULT 'pending',
  started_at timestamptz,
  finished_at timestamptz,
  input_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  output_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  idempotency_key varchar(255) NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (trigger_type IN ('manual', 'event', 'schedule', 'api', 'importer', 'system')),
  CHECK (target_type IN ('asset', 'document', 'memory', 'project', 'backup', 'import_run') OR target_type IS NULL),
  CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled', 'superseded'))
);

CREATE TABLE inbox_items (
  inbox_item_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid,
  inbox_type varchar(32) NOT NULL,
  source varchar(80) NOT NULL,
  source_uri text,
  source_ref varchar(255),
  status varchar(24) NOT NULL DEFAULT 'received',
  asset_id uuid,
  title varchar(300),
  content_hash varchar(128),
  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  received_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  created_by_actor_type varchar(24) NOT NULL DEFAULT 'user',
  created_by_actor_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (inbox_type IN ('file', 'url', 'text', 'email', 'message', 'api', 'importer')),
  CHECK (status IN ('received', 'staged', 'linked', 'processed', 'rejected', 'failed', 'archived')),
  CHECK (created_by_actor_type IN ('user', 'agent', 'service', 'system'))
);

CREATE TABLE assets (
  asset_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid,
  asset_uid varchar(128) NOT NULL,
  title varchar(300) NOT NULL,
  asset_type varchar(40) NOT NULL,
  media_type varchar(120),
  original_filename varchar(255),
  storage_backend varchar(32) NOT NULL DEFAULT 'mneme_data',
  storage_ref text NOT NULL,
  canonical_uri text,
  content_hash varchar(128) NOT NULL,
  size_bytes bigint,
  status varchar(24) NOT NULL DEFAULT 'active',
  ingest_state varchar(24) NOT NULL DEFAULT 'pending',
  knowledge_state varchar(24) NOT NULL DEFAULT 'not_started',
  current_version bigint NOT NULL DEFAULT 1,
  sensitivity_level varchar(24) NOT NULL DEFAULT 'normal',
  retention_policy varchar(40) NOT NULL DEFAULT 'default',
  source_inbox_item_id uuid,
  created_by_user_id uuid,
  imported_from varchar(80),
  imported_source_id varchar(255),
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz,
  CHECK (asset_type IN ('document', 'image', 'audio', 'video', 'archive', 'dataset', 'note', 'url', 'other')),
  CHECK (storage_backend IN ('mneme_data', 'local_path', 'external_uri', 's3_compatible')),
  CHECK (status IN ('active', 'archived', 'deleted', 'quarantined')),
  CHECK (ingest_state IN ('pending', 'staged', 'importing', 'ready', 'failed')),
  CHECK (knowledge_state IN ('not_started', 'pending', 'running', 'ready', 'stale', 'failed')),
  CHECK (sensitivity_level IN ('public', 'normal', 'private', 'sensitive', 'secret')),
  UNIQUE (project_id, asset_uid),
  UNIQUE (project_id, content_hash)
);

CREATE TABLE asset_metadata (
  asset_metadata_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id uuid NOT NULL,
  metadata_key varchar(120) NOT NULL,
  metadata_value text,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  value_type varchar(24) NOT NULL DEFAULT 'text',
  source varchar(80) NOT NULL DEFAULT 'system',
  confidence numeric(5,4),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (value_type IN ('text', 'number', 'boolean', 'date', 'json')),
  CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  UNIQUE (asset_id, metadata_key, source)
);

CREATE TABLE knowledge_documents (
  document_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid,
  title varchar(300) NOT NULL,
  canonical_uri text,
  document_status varchar(24) NOT NULL DEFAULT 'active',
  current_version bigint NOT NULL DEFAULT 1,
  sensitivity_level varchar(24) NOT NULL DEFAULT 'normal',
  summary text,
  created_by_user_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (document_status IN ('active', 'archived', 'deleted')),
  CHECK (sensitivity_level IN ('public', 'normal', 'private', 'sensitive', 'secret'))
);

CREATE TABLE knowledge_blocks (
  block_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL,
  block_key varchar(80) NOT NULL,
  block_order integer NOT NULL,
  current_version bigint NOT NULL DEFAULT 1,
  block_type varchar(32) NOT NULL DEFAULT 'paragraph',
  content_markdown text NOT NULL,
  content_text text NOT NULL,
  token_count integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, block_key),
  UNIQUE (document_id, block_order),
  CHECK (block_type IN ('title', 'paragraph', 'list', 'table', 'quote', 'code', 'image_caption', 'metadata'))
);

CREATE TABLE knowledge_chunks (
  chunk_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL,
  block_id uuid,
  chunk_order integer NOT NULL,
  document_version bigint NOT NULL,
  chunk_text text NOT NULL,
  token_count integer,
  embedding vector(1536),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, document_version, chunk_order)
);

CREATE TABLE index_states (
  index_state_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  object_type varchar(40) NOT NULL,
  object_id uuid NOT NULL,
  ready_version bigint NOT NULL DEFAULT 0,
  stale_version bigint NOT NULL DEFAULT 0,
  fts_state varchar(24) NOT NULL DEFAULT 'pending',
  vector_state varchar(24) NOT NULL DEFAULT 'pending',
  graph_state varchar(24) NOT NULL DEFAULT 'pending',
  citation_state varchar(24) NOT NULL DEFAULT 'pending',
  last_refreshed_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (object_type, object_id),
  CHECK (fts_state IN ('pending', 'ready', 'stale', 'failed')),
  CHECK (vector_state IN ('pending', 'ready', 'stale', 'failed')),
  CHECK (graph_state IN ('pending', 'ready', 'stale', 'failed')),
  CHECK (citation_state IN ('pending', 'ready', 'stale', 'failed'))
);

CREATE TABLE source_maps (
  source_map_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid,
  source_type varchar(40) NOT NULL,
  source_id uuid NOT NULL,
  target_type varchar(40) NOT NULL,
  target_id uuid NOT NULL,
  source_asset_id uuid,
  source_document_id uuid,
  source_block_id uuid,
  target_document_id uuid,
  target_block_id uuid,
  target_chunk_id uuid,
  span jsonb NOT NULL DEFAULT '{}'::jsonb,
  confidence numeric(5,4),
  mapping_role varchar(32) NOT NULL DEFAULT 'citation',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (source_type IN ('asset', 'document', 'block', 'chunk', 'message', 'raw_event', 'memory_candidate', 'external')),
  CHECK (target_type IN ('document', 'block', 'chunk', 'memory_candidate', 'memory', 'asset')),
  CHECK (mapping_role IN ('citation', 'derived_from', 'extracted_from', 'transformed_from', 'attachment'))
);

CREATE TABLE conversations (
  conversation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid,
  owner_user_id uuid,
  conversation_type varchar(32) NOT NULL DEFAULT 'chat',
  title varchar(300),
  source_platform varchar(48) NOT NULL,
  sensitivity_level varchar(24) NOT NULL DEFAULT 'private',
  retention_days integer,
  conversation_status varchar(24) NOT NULL DEFAULT 'active',
  started_at timestamptz,
  ended_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (conversation_type IN ('chat', 'meeting', 'email_thread', 'system_event', 'agent_run')),
  CHECK (conversation_status IN ('active', 'archived', 'deleted')),
  CHECK (sensitivity_level IN ('public', 'normal', 'private', 'sensitive', 'secret'))
);

CREATE TABLE event_source (
  event_source_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL,
  source_platform varchar(48) NOT NULL,
  external_conversation_id varchar(255),
  source_account_id varchar(255),
  source_uri text,
  participants_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  time_range_start timestamptz,
  time_range_end timestamptz,
  import_run_id uuid,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE messages (
  message_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL,
  event_source_id uuid,
  parent_message_id uuid,
  role_code varchar(24) NOT NULL,
  sender_label varchar(120),
  content_text text NOT NULL,
  content_markdown text,
  content_hash varchar(128) NOT NULL,
  sensitivity_level varchar(24) NOT NULL DEFAULT 'private',
  pii_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
  message_time timestamptz NOT NULL,
  ingested_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (role_code IN ('user', 'assistant', 'agent', 'system', 'tool', 'other')),
  CHECK (sensitivity_level IN ('public', 'normal', 'private', 'sensitive', 'secret')),
  UNIQUE (event_source_id, content_hash, message_time)
);

CREATE TABLE raw_events (
  raw_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid,
  event_source_id uuid,
  conversation_id uuid,
  message_id uuid,
  raw_event_type varchar(40) NOT NULL,
  source_platform varchar(48) NOT NULL,
  external_event_id varchar(255),
  event_time timestamptz NOT NULL,
  payload_hash varchar(128) NOT NULL,
  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  text_preview text,
  sensitivity_level varchar(24) NOT NULL DEFAULT 'private',
  pii_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
  retention_until timestamptz,
  import_run_id uuid,
  idempotency_key varchar(255) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (raw_event_type IN ('message', 'tool_call', 'tool_result', 'reaction', 'attachment', 'system_event', 'import_record')),
  CHECK (sensitivity_level IN ('public', 'normal', 'private', 'sensitive', 'secret')),
  UNIQUE (idempotency_key),
  UNIQUE (event_source_id, payload_hash, event_time)
);

CREATE TABLE memory_candidates (
  candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid,
  source_type varchar(32) NOT NULL,
  source_id uuid,
  submitted_by_actor_type varchar(24) NOT NULL,
  submitted_by_actor_id uuid,
  title varchar(240),
  candidate_text text NOT NULL,
  candidate_hash varchar(128) NOT NULL,
  sensitivity_level varchar(24) NOT NULL DEFAULT 'private',
  candidate_status varchar(24) NOT NULL DEFAULT 'pending_review',
  confidence_score numeric(5,4),
  review_required boolean NOT NULL DEFAULT true,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, candidate_hash),
  CHECK (source_type IN ('message', 'raw_event', 'manual', 'importer', 'agent_submission')),
  CHECK (submitted_by_actor_type IN ('user', 'agent', 'service', 'system')),
  CHECK (sensitivity_level IN ('public', 'normal', 'private', 'sensitive', 'secret')),
  CHECK (candidate_status IN ('pending_review', 'approved', 'rejected', 'superseded', 'conflict'))
);

CREATE TABLE memories (
  memory_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid,
  canonical_key varchar(160) NOT NULL,
  title varchar(240),
  memory_text text NOT NULL,
  current_version bigint NOT NULL DEFAULT 1,
  sensitivity_level varchar(24) NOT NULL DEFAULT 'private',
  status varchar(24) NOT NULL DEFAULT 'active',
  activated_from_candidate_id uuid,
  activated_by_review_item_id uuid,
  activated_at timestamptz,
  expired_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, canonical_key),
  CHECK (sensitivity_level IN ('public', 'normal', 'private', 'sensitive', 'secret')),
  CHECK (status IN ('draft', 'active', 'expired', 'merged', 'deleted')),
  CHECK (status <> 'active' OR activated_by_review_item_id IS NOT NULL)
);

CREATE TABLE memory_versions (
  memory_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  memory_id uuid NOT NULL,
  version bigint NOT NULL,
  action varchar(32) NOT NULL,
  before_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  after_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  actor_type varchar(24) NOT NULL,
  actor_id uuid,
  review_item_id uuid,
  candidate_id uuid,
  event_id uuid,
  reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (version >= 1),
  CHECK (action IN ('create', 'update', 'merge', 'expire', 'delete', 'restore')),
  CHECK (actor_type IN ('user', 'agent', 'service', 'system')),
  UNIQUE (memory_id, version)
);

CREATE TABLE memory_sources (
  memory_source_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  memory_id uuid NOT NULL,
  memory_version bigint NOT NULL,
  candidate_id uuid,
  raw_event_id uuid,
  asset_id uuid,
  document_id uuid,
  block_id uuid,
  message_id uuid,
  source_span jsonb NOT NULL DEFAULT '{}'::jsonb,
  confidence numeric(5,4),
  source_role varchar(32) NOT NULL DEFAULT 'evidence',
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  CHECK (source_role IN ('evidence', 'origin', 'supporting', 'conflict', 'supersedes'))
);

CREATE TABLE memory_index_entries (
  memory_index_entry_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  memory_id uuid NOT NULL,
  memory_version bigint NOT NULL,
  project_id uuid,
  index_profile varchar(80) NOT NULL DEFAULT 'default',
  embedding_model_id uuid,
  content_hash varchar(128) NOT NULL,
  index_text text NOT NULL,
  fts_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(index_text, ''))) STORED,
  embedding vector(1536),
  fts_state varchar(24) NOT NULL DEFAULT 'pending',
  vector_state varchar(24) NOT NULL DEFAULT 'pending',
  ready_at timestamptz,
  stale_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (fts_state IN ('pending', 'ready', 'stale', 'failed')),
  CHECK (vector_state IN ('pending', 'ready', 'stale', 'failed')),
  UNIQUE (memory_id, memory_version, index_profile)
);

CREATE TABLE memory_relations (
  memory_relation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid,
  from_memory_id uuid NOT NULL,
  from_memory_version bigint,
  to_memory_id uuid NOT NULL,
  to_memory_version bigint,
  relation_type varchar(32) NOT NULL,
  relation_status varchar(24) NOT NULL DEFAULT 'active',
  created_by_review_item_id uuid,
  reason text,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (relation_type IN ('conflicts_with', 'supersedes', 'merged_into', 'duplicates', 'supports')),
  CHECK (relation_status IN ('active', 'resolved', 'cancelled')),
  CHECK (from_memory_id <> to_memory_id),
  UNIQUE (from_memory_id, to_memory_id, relation_type)
);

CREATE TABLE context_packs (
  context_pack_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id uuid NOT NULL,
  correlation_id uuid NOT NULL,
  agent_id uuid,
  project_id uuid,
  actor_type varchar(24) NOT NULL,
  actor_id uuid,
  compile_mode varchar(24) NOT NULL,
  status varchar(24) NOT NULL DEFAULT 'created',
  knowledge_version_set jsonb NOT NULL DEFAULT '[]'::jsonb,
  memory_version_set jsonb NOT NULL DEFAULT '[]'::jsonb,
  token_budget jsonb NOT NULL DEFAULT '{}'::jsonb,
  exclusion_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
  api_call_log_id uuid,
  retention_until timestamptz NOT NULL DEFAULT (now() + interval '180 days'),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (actor_type IN ('user', 'agent', 'service', 'system')),
  CHECK (compile_mode IN ('full', 'search_fallback')),
  CHECK (status IN ('created', 'used', 'failed', 'expired'))
);

CREATE TABLE context_pack_items (
  context_pack_item_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  context_pack_id uuid NOT NULL,
  item_order integer NOT NULL,
  item_type varchar(32) NOT NULL,
  object_id uuid,
  object_version bigint,
  source_ref jsonb NOT NULL DEFAULT '{}'::jsonb,
  included boolean NOT NULL DEFAULT true,
  exclusion_reason varchar(80),
  score numeric(8,6),
  token_count integer,
  reason text,
  content_digest varchar(128),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (item_type IN ('knowledge_document', 'knowledge_block', 'knowledge_chunk', 'memory', 'raw_event', 'fallback_query')),
  UNIQUE (context_pack_id, item_order)
);

CREATE TABLE object_registry (
  object_id uuid PRIMARY KEY,
  project_id uuid,
  object_type varchar(80) NOT NULL,
  object_key varchar(255),
  owner_actor_type varchar(24) NOT NULL DEFAULT 'system',
  owner_actor_id uuid,
  status varchar(24) NOT NULL DEFAULT 'active',
  current_version bigint NOT NULL DEFAULT 1,
  sensitivity_level varchar(24) NOT NULL DEFAULT 'normal',
  source_type varchar(80),
  source_id uuid,
  canonical_uri text,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz,
  CHECK (object_type IN ('asset', 'document', 'block', 'chunk', 'conversation', 'message', 'raw_event', 'memory_candidate', 'memory', 'context_pack', 'job', 'pipeline_def', 'pipeline_run', 'project', 'provider_model', 'credential', 'review_item', 'import_run', 'backup', 'restore', 'external', 'inbox_item')),
  CHECK (owner_actor_type IN ('user', 'agent', 'service', 'system')),
  CHECK (status IN ('active', 'archived', 'deleted', 'quarantined', 'superseded')),
  CHECK (current_version >= 1),
  CHECK (sensitivity_level IN ('public', 'normal', 'private', 'sensitive', 'secret')),
  UNIQUE (object_type, object_id)
);

CREATE TABLE object_versions (
  object_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  object_id uuid NOT NULL,
  object_type varchar(80) NOT NULL,
  version bigint NOT NULL,
  action varchar(32) NOT NULL,
  actor_type varchar(24) NOT NULL,
  actor_id uuid,
  event_id uuid,
  audit_id uuid,
  source_map_id uuid,
  previous_version bigint,
  checksum varchar(128),
  snapshot_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  diff_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (object_type IN ('asset', 'document', 'block', 'chunk', 'conversation', 'message', 'raw_event', 'memory_candidate', 'memory', 'context_pack', 'job', 'pipeline_def', 'pipeline_run', 'project', 'provider_model', 'credential', 'review_item', 'import_run', 'backup', 'restore', 'external', 'inbox_item')),
  CHECK (version >= 1),
  CHECK (previous_version IS NULL OR previous_version >= 1),
  CHECK (action IN ('create', 'update', 'merge', 'expire', 'archive', 'delete', 'restore', 'supersede', 'import')),
  CHECK (actor_type IN ('user', 'agent', 'service', 'system')),
  UNIQUE (object_id, version)
);

ALTER TABLE user_sessions ADD CONSTRAINT fk_user_sessions_user FOREIGN KEY (user_id) REFERENCES users(user_id);
ALTER TABLE agents ADD CONSTRAINT fk_agents_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE agents ADD CONSTRAINT fk_agents_owner_user FOREIGN KEY (owner_user_id) REFERENCES users(user_id) ON DELETE SET NULL;
ALTER TABLE agent_tokens ADD CONSTRAINT fk_agent_tokens_agent FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE;
ALTER TABLE agent_tokens ADD CONSTRAINT fk_agent_tokens_issued_by FOREIGN KEY (issued_by_user_id) REFERENCES users(user_id);

ALTER TABLE audit_events ADD CONSTRAINT fk_audit_events_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE audit_events ADD CONSTRAINT fk_audit_events_review_item FOREIGN KEY (review_item_id) REFERENCES review_items(review_item_id) ON DELETE SET NULL;
ALTER TABLE event_deliveries ADD CONSTRAINT fk_event_deliveries_event FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE;
ALTER TABLE dead_letters ADD CONSTRAINT fk_dead_letters_related_event FOREIGN KEY (related_event_id) REFERENCES events(event_id) ON DELETE SET NULL;
ALTER TABLE review_items ADD CONSTRAINT fk_review_items_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE review_items ADD CONSTRAINT fk_review_items_reviewer FOREIGN KEY (reviewer_id) REFERENCES users(user_id) ON DELETE SET NULL;

ALTER TABLE provider_models ADD CONSTRAINT fk_provider_models_provider FOREIGN KEY (provider_id) REFERENCES providers(provider_id) ON DELETE CASCADE;
ALTER TABLE credential_vault ADD CONSTRAINT fk_credential_vault_provider FOREIGN KEY (provider_id) REFERENCES providers(provider_id);
ALTER TABLE credential_vault ADD CONSTRAINT fk_credential_vault_created_by FOREIGN KEY (created_by_user_id) REFERENCES users(user_id) ON DELETE SET NULL;
ALTER TABLE capability_bindings ADD CONSTRAINT fk_capability_bindings_capability FOREIGN KEY (capability_id) REFERENCES capabilities(capability_id) ON DELETE CASCADE;
ALTER TABLE capability_bindings ADD CONSTRAINT fk_capability_bindings_provider FOREIGN KEY (provider_id) REFERENCES providers(provider_id) ON DELETE CASCADE;
ALTER TABLE capability_bindings ADD CONSTRAINT fk_capability_bindings_provider_model FOREIGN KEY (provider_model_id) REFERENCES provider_models(provider_model_id) ON DELETE SET NULL;
ALTER TABLE capability_bindings ADD CONSTRAINT fk_capability_bindings_credential FOREIGN KEY (credential_id) REFERENCES credential_vault(credential_id) ON DELETE SET NULL;
ALTER TABLE capability_bindings ADD CONSTRAINT fk_capability_bindings_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE;
ALTER TABLE capability_bindings ADD CONSTRAINT fk_capability_bindings_created_by FOREIGN KEY (created_by_user_id) REFERENCES users(user_id) ON DELETE SET NULL;
ALTER TABLE vault_access_logs ADD CONSTRAINT fk_vault_access_logs_credential FOREIGN KEY (credential_id) REFERENCES credential_vault(credential_id) ON DELETE SET NULL;
ALTER TABLE vault_access_logs ADD CONSTRAINT fk_vault_access_logs_capability FOREIGN KEY (capability_id) REFERENCES capabilities(capability_id) ON DELETE SET NULL;
ALTER TABLE vault_access_logs ADD CONSTRAINT fk_vault_access_logs_provider FOREIGN KEY (provider_id) REFERENCES providers(provider_id) ON DELETE SET NULL;
ALTER TABLE usage_limits ADD CONSTRAINT fk_usage_limits_capability FOREIGN KEY (capability_id) REFERENCES capabilities(capability_id) ON DELETE SET NULL;
ALTER TABLE usage_limits ADD CONSTRAINT fk_usage_limits_provider FOREIGN KEY (provider_id) REFERENCES providers(provider_id) ON DELETE SET NULL;
ALTER TABLE usage_limits ADD CONSTRAINT fk_usage_limits_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE budget_tracking ADD CONSTRAINT fk_budget_tracking_capability FOREIGN KEY (capability_id) REFERENCES capabilities(capability_id) ON DELETE SET NULL;
ALTER TABLE budget_tracking ADD CONSTRAINT fk_budget_tracking_provider FOREIGN KEY (provider_id) REFERENCES providers(provider_id) ON DELETE SET NULL;
ALTER TABLE budget_tracking ADD CONSTRAINT fk_budget_tracking_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE api_call_logs ADD CONSTRAINT fk_api_call_logs_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE api_call_logs ADD CONSTRAINT fk_api_call_logs_capability FOREIGN KEY (capability_id) REFERENCES capabilities(capability_id);
ALTER TABLE api_call_logs ADD CONSTRAINT fk_api_call_logs_binding FOREIGN KEY (capability_binding_id) REFERENCES capability_bindings(capability_binding_id) ON DELETE SET NULL;
ALTER TABLE api_call_logs ADD CONSTRAINT fk_api_call_logs_provider FOREIGN KEY (provider_id) REFERENCES providers(provider_id);
ALTER TABLE api_call_logs ADD CONSTRAINT fk_api_call_logs_provider_model FOREIGN KEY (provider_model_id) REFERENCES provider_models(provider_model_id) ON DELETE SET NULL;
ALTER TABLE api_call_logs ADD CONSTRAINT fk_api_call_logs_credential FOREIGN KEY (credential_id) REFERENCES credential_vault(credential_id) ON DELETE SET NULL;
ALTER TABLE api_call_logs ADD CONSTRAINT fk_api_call_logs_vault_access FOREIGN KEY (vault_access_log_id) REFERENCES vault_access_logs(access_log_id) ON DELETE SET NULL;
ALTER TABLE api_call_logs ADD CONSTRAINT fk_api_call_logs_budget FOREIGN KEY (budget_tracking_id) REFERENCES budget_tracking(budget_tracking_id) ON DELETE SET NULL;
ALTER TABLE api_call_logs ADD CONSTRAINT fk_api_call_logs_review FOREIGN KEY (review_item_id) REFERENCES review_items(review_item_id) ON DELETE SET NULL;
ALTER TABLE api_call_logs ADD CONSTRAINT fk_api_call_logs_event FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE SET NULL;

ALTER TABLE jobs ADD CONSTRAINT fk_jobs_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE jobs ADD CONSTRAINT fk_jobs_cause_event FOREIGN KEY (cause_event_id) REFERENCES events(event_id) ON DELETE SET NULL;
ALTER TABLE job_logs ADD CONSTRAINT fk_job_logs_job FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE;
ALTER TABLE job_logs ADD CONSTRAINT fk_job_logs_event FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE SET NULL;
ALTER TABLE pipeline_defs ADD CONSTRAINT fk_pipeline_defs_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE pipeline_defs ADD CONSTRAINT fk_pipeline_defs_created_by FOREIGN KEY (created_by_user_id) REFERENCES users(user_id) ON DELETE SET NULL;
ALTER TABLE pipeline_runs ADD CONSTRAINT fk_pipeline_runs_def FOREIGN KEY (pipeline_def_id) REFERENCES pipeline_defs(pipeline_def_id);
ALTER TABLE pipeline_runs ADD CONSTRAINT fk_pipeline_runs_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE pipeline_runs ADD CONSTRAINT fk_pipeline_runs_root_job FOREIGN KEY (root_job_id) REFERENCES jobs(job_id) ON DELETE SET NULL;
ALTER TABLE pipeline_runs ADD CONSTRAINT fk_pipeline_runs_trigger_event FOREIGN KEY (trigger_event_id) REFERENCES events(event_id) ON DELETE SET NULL;

ALTER TABLE inbox_items ADD CONSTRAINT fk_inbox_items_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE assets ADD CONSTRAINT fk_assets_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE assets ADD CONSTRAINT fk_assets_source_inbox FOREIGN KEY (source_inbox_item_id) REFERENCES inbox_items(inbox_item_id) ON DELETE SET NULL;
ALTER TABLE assets ADD CONSTRAINT fk_assets_created_by FOREIGN KEY (created_by_user_id) REFERENCES users(user_id) ON DELETE SET NULL;
ALTER TABLE inbox_items ADD CONSTRAINT fk_inbox_items_asset FOREIGN KEY (asset_id) REFERENCES assets(asset_id) ON DELETE SET NULL;
ALTER TABLE asset_metadata ADD CONSTRAINT fk_asset_metadata_asset FOREIGN KEY (asset_id) REFERENCES assets(asset_id) ON DELETE CASCADE;

ALTER TABLE knowledge_documents ADD CONSTRAINT fk_knowledge_documents_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE knowledge_documents ADD CONSTRAINT fk_knowledge_documents_created_by FOREIGN KEY (created_by_user_id) REFERENCES users(user_id) ON DELETE SET NULL;
ALTER TABLE knowledge_blocks ADD CONSTRAINT fk_knowledge_blocks_document FOREIGN KEY (document_id) REFERENCES knowledge_documents(document_id) ON DELETE CASCADE;
ALTER TABLE knowledge_chunks ADD CONSTRAINT fk_knowledge_chunks_document FOREIGN KEY (document_id) REFERENCES knowledge_documents(document_id) ON DELETE CASCADE;
ALTER TABLE knowledge_chunks ADD CONSTRAINT fk_knowledge_chunks_block FOREIGN KEY (block_id) REFERENCES knowledge_blocks(block_id) ON DELETE SET NULL;
ALTER TABLE source_maps ADD CONSTRAINT fk_source_maps_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE source_maps ADD CONSTRAINT fk_source_maps_asset FOREIGN KEY (source_asset_id) REFERENCES assets(asset_id) ON DELETE SET NULL;
ALTER TABLE source_maps ADD CONSTRAINT fk_source_maps_source_document FOREIGN KEY (source_document_id) REFERENCES knowledge_documents(document_id) ON DELETE SET NULL;
ALTER TABLE source_maps ADD CONSTRAINT fk_source_maps_source_block FOREIGN KEY (source_block_id) REFERENCES knowledge_blocks(block_id) ON DELETE SET NULL;
ALTER TABLE source_maps ADD CONSTRAINT fk_source_maps_target_document FOREIGN KEY (target_document_id) REFERENCES knowledge_documents(document_id) ON DELETE SET NULL;
ALTER TABLE source_maps ADD CONSTRAINT fk_source_maps_target_block FOREIGN KEY (target_block_id) REFERENCES knowledge_blocks(block_id) ON DELETE SET NULL;
ALTER TABLE source_maps ADD CONSTRAINT fk_source_maps_target_chunk FOREIGN KEY (target_chunk_id) REFERENCES knowledge_chunks(chunk_id) ON DELETE SET NULL;

ALTER TABLE conversations ADD CONSTRAINT fk_conversations_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE conversations ADD CONSTRAINT fk_conversations_owner FOREIGN KEY (owner_user_id) REFERENCES users(user_id) ON DELETE SET NULL;
ALTER TABLE event_source ADD CONSTRAINT fk_event_source_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE;
ALTER TABLE messages ADD CONSTRAINT fk_messages_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE;
ALTER TABLE messages ADD CONSTRAINT fk_messages_event_source FOREIGN KEY (event_source_id) REFERENCES event_source(event_source_id) ON DELETE SET NULL;
ALTER TABLE messages ADD CONSTRAINT fk_messages_parent FOREIGN KEY (parent_message_id) REFERENCES messages(message_id) ON DELETE SET NULL;
ALTER TABLE raw_events ADD CONSTRAINT fk_raw_events_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE raw_events ADD CONSTRAINT fk_raw_events_event_source FOREIGN KEY (event_source_id) REFERENCES event_source(event_source_id) ON DELETE SET NULL;
ALTER TABLE raw_events ADD CONSTRAINT fk_raw_events_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id) ON DELETE SET NULL;
ALTER TABLE raw_events ADD CONSTRAINT fk_raw_events_message FOREIGN KEY (message_id) REFERENCES messages(message_id) ON DELETE SET NULL;
ALTER TABLE memory_candidates ADD CONSTRAINT fk_memory_candidates_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE memories ADD CONSTRAINT fk_memories_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE memories ADD CONSTRAINT fk_memories_candidate FOREIGN KEY (activated_from_candidate_id) REFERENCES memory_candidates(candidate_id) ON DELETE SET NULL;
ALTER TABLE memories ADD CONSTRAINT fk_memories_review FOREIGN KEY (activated_by_review_item_id) REFERENCES review_items(review_item_id) ON DELETE RESTRICT;
ALTER TABLE memory_versions ADD CONSTRAINT fk_memory_versions_memory FOREIGN KEY (memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE;
ALTER TABLE memory_versions ADD CONSTRAINT fk_memory_versions_review FOREIGN KEY (review_item_id) REFERENCES review_items(review_item_id) ON DELETE SET NULL;
ALTER TABLE memory_versions ADD CONSTRAINT fk_memory_versions_candidate FOREIGN KEY (candidate_id) REFERENCES memory_candidates(candidate_id) ON DELETE SET NULL;
ALTER TABLE memory_versions ADD CONSTRAINT fk_memory_versions_event FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE SET NULL;
ALTER TABLE memory_sources ADD CONSTRAINT fk_memory_sources_version FOREIGN KEY (memory_id, memory_version) REFERENCES memory_versions(memory_id, version) ON DELETE CASCADE;
ALTER TABLE memory_sources ADD CONSTRAINT fk_memory_sources_candidate FOREIGN KEY (candidate_id) REFERENCES memory_candidates(candidate_id) ON DELETE SET NULL;
ALTER TABLE memory_sources ADD CONSTRAINT fk_memory_sources_raw_event FOREIGN KEY (raw_event_id) REFERENCES raw_events(raw_event_id) ON DELETE SET NULL;
ALTER TABLE memory_sources ADD CONSTRAINT fk_memory_sources_asset FOREIGN KEY (asset_id) REFERENCES assets(asset_id) ON DELETE SET NULL;
ALTER TABLE memory_sources ADD CONSTRAINT fk_memory_sources_document FOREIGN KEY (document_id) REFERENCES knowledge_documents(document_id) ON DELETE SET NULL;
ALTER TABLE memory_sources ADD CONSTRAINT fk_memory_sources_block FOREIGN KEY (block_id) REFERENCES knowledge_blocks(block_id) ON DELETE SET NULL;
ALTER TABLE memory_sources ADD CONSTRAINT fk_memory_sources_message FOREIGN KEY (message_id) REFERENCES messages(message_id) ON DELETE SET NULL;
ALTER TABLE memory_index_entries ADD CONSTRAINT fk_memory_index_entries_version FOREIGN KEY (memory_id, memory_version) REFERENCES memory_versions(memory_id, version) ON DELETE CASCADE;
ALTER TABLE memory_index_entries ADD CONSTRAINT fk_memory_index_entries_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE memory_index_entries ADD CONSTRAINT fk_memory_index_entries_embedding_model FOREIGN KEY (embedding_model_id) REFERENCES provider_models(provider_model_id) ON DELETE SET NULL;
ALTER TABLE memory_relations ADD CONSTRAINT fk_memory_relations_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE memory_relations ADD CONSTRAINT fk_memory_relations_from_memory FOREIGN KEY (from_memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE;
ALTER TABLE memory_relations ADD CONSTRAINT fk_memory_relations_to_memory FOREIGN KEY (to_memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE;
ALTER TABLE memory_relations ADD CONSTRAINT fk_memory_relations_review FOREIGN KEY (created_by_review_item_id) REFERENCES review_items(review_item_id) ON DELETE SET NULL;

ALTER TABLE context_packs ADD CONSTRAINT fk_context_packs_agent FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE SET NULL;
ALTER TABLE context_packs ADD CONSTRAINT fk_context_packs_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE context_packs ADD CONSTRAINT fk_context_packs_api_call FOREIGN KEY (api_call_log_id) REFERENCES api_call_logs(api_call_log_id) ON DELETE SET NULL;
ALTER TABLE context_pack_items ADD CONSTRAINT fk_context_pack_items_pack FOREIGN KEY (context_pack_id) REFERENCES context_packs(context_pack_id) ON DELETE CASCADE;

ALTER TABLE object_registry ADD CONSTRAINT fk_object_registry_project FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE SET NULL;
ALTER TABLE object_versions ADD CONSTRAINT fk_object_versions_registry FOREIGN KEY (object_type, object_id) REFERENCES object_registry(object_type, object_id) ON DELETE CASCADE;
ALTER TABLE object_versions ADD CONSTRAINT fk_object_versions_event FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE SET NULL;
ALTER TABLE object_versions ADD CONSTRAINT fk_object_versions_audit FOREIGN KEY (audit_id) REFERENCES audit_events(audit_id) ON DELETE SET NULL;
ALTER TABLE object_versions ADD CONSTRAINT fk_object_versions_source_map FOREIGN KEY (source_map_id) REFERENCES source_maps(source_map_id) ON DELETE SET NULL;

CREATE INDEX idx_user_sessions_user_id ON user_sessions(user_id);
CREATE INDEX idx_user_sessions_expires_at ON user_sessions(expires_at);
CREATE INDEX idx_audit_events_occurred_at ON audit_events(occurred_at DESC);
CREATE INDEX idx_audit_events_actor ON audit_events(actor_type, actor_id);
CREATE INDEX idx_audit_events_object ON audit_events(object_type, object_id);
CREATE INDEX idx_audit_events_correlation ON audit_events(correlation_id);
CREATE INDEX idx_events_publish_state ON events(publish_state, committed_at);
CREATE INDEX idx_events_aggregate ON events(aggregate_type, aggregate_id, aggregate_version DESC);
CREATE INDEX idx_event_deliveries_state ON event_deliveries(delivery_state, updated_at);
CREATE INDEX idx_dead_letters_replay_state ON dead_letters(replay_state, created_at);
CREATE INDEX idx_review_items_status_priority ON review_items(status, priority, created_at);
CREATE INDEX idx_review_items_target ON review_items(target_type, target_id, target_version DESC);
CREATE INDEX idx_provider_models_provider_status ON provider_models(provider_id, status);
CREATE INDEX idx_provider_models_type_status ON provider_models(model_type, status);
CREATE INDEX idx_capability_bindings_lookup ON capability_bindings(capability_id, project_id, status, priority);
CREATE UNIQUE INDEX uq_capability_bindings_scope ON capability_bindings(capability_id, provider_id, COALESCE(provider_model_id, '00000000-0000-0000-0000-000000000000'::uuid), COALESCE(project_id, '00000000-0000-0000-0000-000000000000'::uuid), sensitivity_floor, sensitivity_ceiling);
CREATE INDEX idx_credential_vault_provider_status ON credential_vault(provider_id, status);
CREATE INDEX idx_vault_access_logs_occurred_at ON vault_access_logs(occurred_at DESC);
CREATE INDEX idx_usage_limits_subject ON usage_limits(subject_type, subject_id, enabled);
CREATE INDEX idx_budget_tracking_correlation ON budget_tracking(correlation_id);
CREATE INDEX idx_budget_tracking_state ON budget_tracking(reservation_state, created_at DESC);
CREATE INDEX idx_api_call_logs_request ON api_call_logs(request_id);
CREATE INDEX idx_api_call_logs_correlation ON api_call_logs(correlation_id, created_at DESC);
CREATE INDEX idx_api_call_logs_provider_state ON api_call_logs(provider_id, provider_model_id, call_state, created_at DESC);
CREATE INDEX idx_api_call_logs_capability_project ON api_call_logs(capability_id, project_id, created_at DESC);
CREATE INDEX idx_api_call_logs_retention ON api_call_logs(retention_until);
CREATE INDEX idx_jobs_runnable ON jobs(queue_name, status, priority, available_at);
CREATE INDEX idx_jobs_aggregate ON jobs(aggregate_type, aggregate_id, target_version DESC);
CREATE INDEX idx_job_logs_job_time ON job_logs(job_id, occurred_at);
CREATE INDEX idx_pipeline_defs_type_status ON pipeline_defs(pipeline_type, status);
CREATE INDEX idx_pipeline_runs_target ON pipeline_runs(target_type, target_id, target_version DESC);
CREATE INDEX idx_inbox_items_project_status ON inbox_items(project_id, status, received_at DESC);
CREATE INDEX idx_assets_project_status ON assets(project_id, status, created_at DESC);
CREATE INDEX idx_assets_ingest_state ON assets(ingest_state, updated_at);
CREATE INDEX idx_asset_metadata_asset_id ON asset_metadata(asset_id);
CREATE INDEX idx_knowledge_chunks_document_version ON knowledge_chunks(document_id, document_version DESC);
CREATE INDEX idx_source_maps_source ON source_maps(source_type, source_id);
CREATE INDEX idx_source_maps_target ON source_maps(target_type, target_id);
CREATE INDEX idx_conversations_project_id ON conversations(project_id);
CREATE INDEX idx_event_source_conversation_id ON event_source(conversation_id);
CREATE INDEX idx_messages_conversation_time ON messages(conversation_id, message_time);
CREATE INDEX idx_raw_events_source_time ON raw_events(event_source_id, event_time);
CREATE INDEX idx_raw_events_message ON raw_events(message_id);
CREATE INDEX idx_raw_events_retention ON raw_events(retention_until);
CREATE INDEX idx_memory_candidates_project_status ON memory_candidates(project_id, candidate_status, created_at DESC);
CREATE INDEX idx_memories_project_status ON memories(project_id, status, updated_at DESC);
CREATE INDEX idx_memory_versions_memory ON memory_versions(memory_id, version DESC);
CREATE INDEX idx_memory_versions_review_item ON memory_versions(review_item_id);
CREATE INDEX idx_memory_sources_memory ON memory_sources(memory_id, memory_version);
CREATE INDEX idx_memory_sources_raw_event ON memory_sources(raw_event_id);
CREATE INDEX idx_memory_index_entries_ready ON memory_index_entries(project_id, fts_state, vector_state, ready_at DESC);
CREATE INDEX idx_memory_index_entries_fts ON memory_index_entries USING gin(fts_vector);
CREATE INDEX idx_memory_relations_from ON memory_relations(from_memory_id, relation_type, relation_status);
CREATE INDEX idx_memory_relations_to ON memory_relations(to_memory_id, relation_type, relation_status);
CREATE INDEX idx_context_packs_request ON context_packs(request_id);
CREATE INDEX idx_context_packs_agent_project ON context_packs(agent_id, project_id, created_at DESC);
CREATE INDEX idx_context_pack_items_pack ON context_pack_items(context_pack_id, item_order);
CREATE UNIQUE INDEX uq_object_registry_project_key ON object_registry(project_id, object_type, object_key) WHERE object_key IS NOT NULL;
CREATE INDEX idx_object_registry_project_status ON object_registry(project_id, object_type, status, updated_at DESC);
CREATE INDEX idx_object_versions_object ON object_versions(object_id, version DESC);

CREATE TRIGGER trg_projects_updated_at BEFORE UPDATE ON projects FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_user_sessions_updated_at BEFORE UPDATE ON user_sessions FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_agents_updated_at BEFORE UPDATE ON agents FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_agent_tokens_updated_at BEFORE UPDATE ON agent_tokens FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_event_deliveries_updated_at BEFORE UPDATE ON event_deliveries FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_dead_letters_updated_at BEFORE UPDATE ON dead_letters FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_review_items_updated_at BEFORE UPDATE ON review_items FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_providers_updated_at BEFORE UPDATE ON providers FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_provider_models_updated_at BEFORE UPDATE ON provider_models FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_capabilities_updated_at BEFORE UPDATE ON capabilities FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_capability_bindings_updated_at BEFORE UPDATE ON capability_bindings FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_credential_vault_updated_at BEFORE UPDATE ON credential_vault FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_api_call_logs_updated_at BEFORE UPDATE ON api_call_logs FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_usage_limits_updated_at BEFORE UPDATE ON usage_limits FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_budget_tracking_updated_at BEFORE UPDATE ON budget_tracking FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_jobs_updated_at BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_pipeline_defs_updated_at BEFORE UPDATE ON pipeline_defs FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_pipeline_runs_updated_at BEFORE UPDATE ON pipeline_runs FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_inbox_items_updated_at BEFORE UPDATE ON inbox_items FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_assets_updated_at BEFORE UPDATE ON assets FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_asset_metadata_updated_at BEFORE UPDATE ON asset_metadata FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_knowledge_documents_updated_at BEFORE UPDATE ON knowledge_documents FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_knowledge_blocks_updated_at BEFORE UPDATE ON knowledge_blocks FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_knowledge_chunks_updated_at BEFORE UPDATE ON knowledge_chunks FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_index_states_updated_at BEFORE UPDATE ON index_states FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_source_maps_updated_at BEFORE UPDATE ON source_maps FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_conversations_updated_at BEFORE UPDATE ON conversations FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_event_source_updated_at BEFORE UPDATE ON event_source FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_messages_updated_at BEFORE UPDATE ON messages FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_memory_candidates_updated_at BEFORE UPDATE ON memory_candidates FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_memories_updated_at BEFORE UPDATE ON memories FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_memory_index_entries_updated_at BEFORE UPDATE ON memory_index_entries FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_object_registry_updated_at BEFORE UPDATE ON object_registry FOR EACH ROW EXECUTE FUNCTION set_updated_at();
"""


DOWNGRADE_SQL = r"""
DROP TRIGGER IF EXISTS trg_object_registry_updated_at ON object_registry;
DROP TRIGGER IF EXISTS trg_memory_index_entries_updated_at ON memory_index_entries;
DROP TRIGGER IF EXISTS trg_memories_updated_at ON memories;
DROP TRIGGER IF EXISTS trg_memory_candidates_updated_at ON memory_candidates;
DROP TRIGGER IF EXISTS trg_messages_updated_at ON messages;
DROP TRIGGER IF EXISTS trg_event_source_updated_at ON event_source;
DROP TRIGGER IF EXISTS trg_conversations_updated_at ON conversations;
DROP TRIGGER IF EXISTS trg_source_maps_updated_at ON source_maps;
DROP TRIGGER IF EXISTS trg_index_states_updated_at ON index_states;
DROP TRIGGER IF EXISTS trg_knowledge_chunks_updated_at ON knowledge_chunks;
DROP TRIGGER IF EXISTS trg_knowledge_blocks_updated_at ON knowledge_blocks;
DROP TRIGGER IF EXISTS trg_knowledge_documents_updated_at ON knowledge_documents;
DROP TRIGGER IF EXISTS trg_asset_metadata_updated_at ON asset_metadata;
DROP TRIGGER IF EXISTS trg_assets_updated_at ON assets;
DROP TRIGGER IF EXISTS trg_inbox_items_updated_at ON inbox_items;
DROP TRIGGER IF EXISTS trg_pipeline_runs_updated_at ON pipeline_runs;
DROP TRIGGER IF EXISTS trg_pipeline_defs_updated_at ON pipeline_defs;
DROP TRIGGER IF EXISTS trg_jobs_updated_at ON jobs;
DROP TRIGGER IF EXISTS trg_budget_tracking_updated_at ON budget_tracking;
DROP TRIGGER IF EXISTS trg_usage_limits_updated_at ON usage_limits;
DROP TRIGGER IF EXISTS trg_api_call_logs_updated_at ON api_call_logs;
DROP TRIGGER IF EXISTS trg_credential_vault_updated_at ON credential_vault;
DROP TRIGGER IF EXISTS trg_capability_bindings_updated_at ON capability_bindings;
DROP TRIGGER IF EXISTS trg_capabilities_updated_at ON capabilities;
DROP TRIGGER IF EXISTS trg_provider_models_updated_at ON provider_models;
DROP TRIGGER IF EXISTS trg_providers_updated_at ON providers;
DROP TRIGGER IF EXISTS trg_review_items_updated_at ON review_items;
DROP TRIGGER IF EXISTS trg_dead_letters_updated_at ON dead_letters;
DROP TRIGGER IF EXISTS trg_event_deliveries_updated_at ON event_deliveries;
DROP TRIGGER IF EXISTS trg_agent_tokens_updated_at ON agent_tokens;
DROP TRIGGER IF EXISTS trg_agents_updated_at ON agents;
DROP TRIGGER IF EXISTS trg_user_sessions_updated_at ON user_sessions;
DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
DROP TRIGGER IF EXISTS trg_projects_updated_at ON projects;
DROP TABLE IF EXISTS object_versions CASCADE;
DROP TABLE IF EXISTS object_registry CASCADE;
DROP TABLE IF EXISTS context_pack_items CASCADE;
DROP TABLE IF EXISTS context_packs CASCADE;
DROP TABLE IF EXISTS memory_relations CASCADE;
DROP TABLE IF EXISTS memory_index_entries CASCADE;
DROP TABLE IF EXISTS memory_sources CASCADE;
DROP TABLE IF EXISTS memory_versions CASCADE;
DROP TABLE IF EXISTS memories CASCADE;
DROP TABLE IF EXISTS memory_candidates CASCADE;
DROP TABLE IF EXISTS raw_events CASCADE;
DROP TABLE IF EXISTS messages CASCADE;
DROP TABLE IF EXISTS event_source CASCADE;
DROP TABLE IF EXISTS conversations CASCADE;
DROP TABLE IF EXISTS source_maps CASCADE;
DROP TABLE IF EXISTS index_states CASCADE;
DROP TABLE IF EXISTS knowledge_chunks CASCADE;
DROP TABLE IF EXISTS knowledge_blocks CASCADE;
DROP TABLE IF EXISTS knowledge_documents CASCADE;
DROP TABLE IF EXISTS asset_metadata CASCADE;
DROP TABLE IF EXISTS assets CASCADE;
DROP TABLE IF EXISTS inbox_items CASCADE;
DROP TABLE IF EXISTS pipeline_runs CASCADE;
DROP TABLE IF EXISTS pipeline_defs CASCADE;
DROP TABLE IF EXISTS job_logs CASCADE;
DROP TABLE IF EXISTS jobs CASCADE;
DROP TABLE IF EXISTS api_call_logs CASCADE;
DROP TABLE IF EXISTS budget_tracking CASCADE;
DROP TABLE IF EXISTS usage_limits CASCADE;
DROP TABLE IF EXISTS vault_access_logs CASCADE;
DROP TABLE IF EXISTS capability_bindings CASCADE;
DROP TABLE IF EXISTS credential_vault CASCADE;
DROP TABLE IF EXISTS capabilities CASCADE;
DROP TABLE IF EXISTS provider_models CASCADE;
DROP TABLE IF EXISTS providers CASCADE;
DROP TABLE IF EXISTS review_items CASCADE;
DROP TABLE IF EXISTS dead_letters CASCADE;
DROP TABLE IF EXISTS event_deliveries CASCADE;
DROP TABLE IF EXISTS events CASCADE;
DROP TABLE IF EXISTS audit_events CASCADE;
DROP TABLE IF EXISTS agent_tokens CASCADE;
DROP TABLE IF EXISTS agents CASCADE;
DROP TABLE IF EXISTS user_sessions CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS projects CASCADE;
DROP FUNCTION IF EXISTS set_updated_at();
"""


def _execute_sql(sql: str) -> None:
    if context.is_offline_mode():
        context.get_context().impl.static_output(sql)
        return

    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    _execute_sql(BASELINE_SQL)


def downgrade() -> None:
    _execute_sql(DOWNGRADE_SQL)
