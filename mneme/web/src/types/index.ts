// ── Auth ──
export interface UserRead {
  user_id: string;
  username: string;
  email: string | null;
  display_name: string;
  role_code: "owner" | "operator" | "viewer" | "auditor";
  status: "pending_bootstrap" | "active" | "disabled" | "locked";
  mfa_mode: string;
  locale: string;
  timezone: string;
  last_login_at: string | null;
  disabled_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface UserSessionRead {
  session_id: string;
  user_id: string;
  session_token_prefix: string;
  auth_method: string;
  device_label: string | null;
  step_up_verified_at: string | null;
  last_seen_at: string;
  expires_at: string;
  revoked_at: string | null;
  revoke_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface LoginRequest {
  username: string;
  password: string;
  device_label?: string;
}

export interface LoginResponse {
  user: UserRead;
  session: UserSessionRead;
}

export interface LogoutResponse {
  session_id: string;
  revoked_at: string;
}

export interface MeResponse {
  user: UserRead;
  session: UserSessionRead;
}

// ── Envelope ──
export interface ApiEnvelope<T = unknown> {
  request_id: string;
  correlation_id: string;
  data: T;
  meta: Record<string, unknown>;
}

export interface ApiErrorEnvelope {
  request_id: string;
  correlation_id: string;
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
  };
}

export interface PageInfo {
  page: number;
  page_size: number;
  total_items: number;
  total_pages: number;
  has_next: boolean;
  has_previous: boolean;
}

export interface PaginatedData<T> {
  items: T[];
  page_info: PageInfo;
}

// ── Health ──
export interface HealthReadyData {
  status: "ok" | "degraded" | "unavailable";
  database: "ok" | "degraded" | "unavailable";
  redis: "ok" | "degraded" | "unavailable";
  outbox_pending: number;
  worker_status?: "lease_holder" | "standby" | "stopped" | "unknown";
}

// ── Audit ──
export interface AuditEvent {
  audit_id: string;
  occurred_at: string;
  actor: {
    actor_type: "user" | "agent" | "service" | "system";
    actor_id: string | null;
    auth_context_type: string | null;
    auth_context_id: string | null;
  };
  action: string;
  object_type: string | null;
  object_id: string | null;
  project_id: string | null;
  result: "success" | "denied" | "failed";
  reason_code: string | null;
  sensitivity_level: "public" | "normal" | "private" | "sensitive" | "secret";
  correlation_id: string;
  request_id: string;
  review_item_id: string | null;
  diff_summary: Record<string, unknown>;
  metadata_json: Record<string, unknown>;
}

// ── Events / Outbox ──
export interface EventRead {
  event_id: string;
  event_type: string;
  aggregate_type: string;
  aggregate_id: string;
  aggregate_version: number;
  correlation_id: string;
  causation_id: string | null;
  idempotency_key: string;
  producer: string;
  payload_json: Record<string, unknown>;
  visibility: "internal" | "external" | "audit_only";
  publish_state: "pending" | "dispatched" | "delivered" | "failed" | "dead_letter";
  occurred_at: string;
  committed_at: string;
  published_at: string | null;
  last_error: string | null;
}

export interface EventDelivery {
  delivery_id: string;
  event_id: string;
  consumer_name: string;
  delivery_state: string;
  dispatch_attempts: number;
  last_dispatched_at: string | null;
  acknowledged_at: string | null;
  failed_at: string | null;
  last_error: string | null;
  lease_expires_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface EventDetail extends EventRead {
  deliveries: EventDelivery[];
}

// ── Review ──
export interface ReviewItem {
  review_item_id: string;
  review_type: string;
  target_type: string;
  target_id: string;
  target_version: number | null;
  status: "pending" | "in_review" | "approved" | "rejected" | "cancelled" | "expired";
  priority: number;
  requester_actor_type: string;
  requester_actor_id: string | null;
  reviewer_id: string | null;
  decision: "approved" | "rejected" | "cancelled" | "expired" | null;
  reason: string | null;
  decided_at: string | null;
  created_at: string;
  correlation_id: string;
  request_id: string;
  expires_at: string | null;
  decision_payload: Record<string, unknown>;
}

// ── Dead Letters ──
export interface DeadLetter {
  dead_letter_id: string;
  source_type: string;
  source_id: string;
  related_event_id: string | null;
  failure_class: string;
  error_message: string;
  replay_state: "pending" | "under_review" | "replayed" | "cancelled" | "resolved";
  external_effect_state: "none" | "unknown" | "confirmed_done" | "confirmed_not_done";
  review_required: boolean;
  created_at: string;
  last_retry_at: string | null;
  resolved_at: string | null;
}

// ── Jobs ──
export interface JobSummary {
  job_id: string;
  job_key: string;
  job_type: string;
  status: "pending" | "scheduled" | "running" | "succeeded" | "failed" | "retrying" | "cancelled" | "dead_letter";
  priority: number;
  queue_name: string;
  scheduled_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  retry_count: number;
  max_retries: number;
  created_by_actor_type: string;
  created_by_actor_id: string | null;
  last_error: string | null;
  created_at: string | null;
}

export interface JobLogEntry {
  job_log_id: string;
  job_id: string;
  step: string;
  level: string;
  message: string;
  attempt_no: number;
  metadata_json: Record<string, unknown>;
  occurred_at: string | null;
}

export interface JobDetail extends JobSummary {
  available_at: string | null;
  input_payload: Record<string, unknown>;
  output: Record<string, unknown>;
  error: Record<string, unknown>;
  logs: JobLogEntry[];
}

// ── Backup / Restore ──
export interface BackupSummary {
  backup_id: string;
  created_at: string;
  pg_version: string;
  status: string;
  file_size_bytes: number;
  alembic_revision: string;
  tables: number;
  table_count_summary: Record<string, number>;
  checksum_sha256: string;
  backup_directory: string;
}

export interface BackupDetail {
  backup_id: string;
  created_at: string;
  pg_version: string;
  format: string;
  tables: number;
  table_row_counts: Record<string, number>;
  file_path: string;
  file_size_bytes: number;
  checksum_sha256: string;
  alembic_revision: string;
  status: string;
  error_message: string | null;
  completed_at: string | null;
  dump_command: string | null;
  env_info: Record<string, string>;
}

export interface BackupTriggerResponse {
  backup_id: string;
  job_id: string;
  status: string;
  message: string;
}

export interface RestoreSubmitResponse {
  backup_id: string;
  review_item_id: string;
  status: string;
  message: string;
}

export interface TableComparisonItem {
  table_name: string;
  backup_rows: number;
  live_rows: number;
  difference: number;
  exists_in_live: boolean;
  will_be: string;
}

export interface RestoreDetailedPreview {
  backup_id: string;
  backup_created_at: string;
  backup_tables: number;
  live_tables: number;
  table_comparisons: TableComparisonItem[];
  total_rows_backup: number;
  total_rows_live: number;
  will_overwrite_tables: number;
  will_create_tables: number;
  will_drop_tables: number;
  warnings: string[];
  error: string | null;
}

export interface RestoreSummary {
  restore_id: string;
  backup_id: string;
  restore_type: string;
  status: string;
  started_at: string;
  completed_at: string;
  target_database: string;
  report_directory: string;
}

export interface RestoreDrillResponse {
  restore_id: string;
  success: boolean;
  status: string;
  verification_summary: Record<string, boolean>;
  report_path: string;
  error_message: string | null;
}

// ── Knowledge (P3-05/P3-06/P3-07/P3-08) ──
export interface KnowledgeDocument {
  document_id: string;
  project_id: string | null;
  sub_library_id: string | null;
  title: string;
  canonical_uri: string | null;
  document_status: "active" | "archived" | "deleted";
  current_version: number;
  sensitivity_level: string;
  summary: string | null;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeBlock {
  block_id: string;
  document_id: string;
  block_key: string;
  block_order: number;
  current_version: number;
  block_type: "title" | "paragraph" | "list" | "table" | "quote" | "code" | "image_caption" | "metadata";
  content_markdown: string;
  content_text: string;
  token_count: number | null;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeChunk {
  chunk_id: string;
  document_id: string;
  block_id: string | null;
  chunk_order: number;
  document_version: number;
  chunk_text: string;
  token_count: number | null;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeFtsSearchResult {
  chunk_id: string;
  document_id: string;
  block_id: string | null;
  chunk_order: number;
  chunk_text: string;
  rank: number;
  document_title: string;
  document_uri: string | null;
  document_sensitivity: string;
  block_key: string | null;
  block_type: string | null;
  block_order: number | null;
  is_stale: boolean;
  stale_reason: string | null;
}

export interface CitationNode {
  type: string;
  id: string;
  label: string;
  uri: string | null;
}

export interface Citation {
  chunk_id: string;
  chunk_text: string;
  chunk_order: number;
  chain: CitationNode[];
  document_id: string | null;
  document_title: string | null;
  document_version: number | null;
  is_stale: boolean;
  stale_reason: string | null;
  created_at: string | null;
}

export interface IndexState {
  index_state_id: string;
  object_type: string;
  object_id: string;
  ready_version: number;
  stale_version: number;
  fts_state: string;
  vector_state: string;
  graph_state: string;
  citation_state: string;
  last_refreshed_at: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

// ── Asset (P3-11) ──
export type AssetType = "document" | "image" | "audio" | "video" | "archive" | "dataset" | "note" | "url" | "other";
export type AssetStatus = "active" | "archived" | "deleted" | "quarantined";
export type IngestState = "pending" | "staged" | "importing" | "ready" | "failed";
export type KnowledgeState = "not_started" | "pending" | "running" | "ready" | "stale" | "failed";
export type SensitivityLevel = "public" | "normal" | "private" | "sensitive" | "secret";
export type RetentionPolicy = "default" | "short_term" | "long_term" | "permanent";

export interface Asset {
  asset_id: string;
  project_id: string | null;
  asset_uid: string;
  title: string;
  asset_type: AssetType;
  media_type: string | null;
  original_filename: string | null;
  storage_backend: string;
  storage_ref: string;
  canonical_uri: string | null;
  content_hash: string;
  size_bytes: number | null;
  status: AssetStatus;
  ingest_state: IngestState;
  knowledge_state: KnowledgeState;
  current_version: number;
  sensitivity_level: SensitivityLevel;
  retention_policy: RetentionPolicy;
  source_inbox_item_id: string | null;
  created_by_user_id: string | null;
  imported_from: string | null;
  imported_source_id: string | null;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

export type MetadataValueType = "text" | "number" | "boolean" | "date" | "json";
export type MetadataSource = "manual" | "system" | "ai" | "importer" | "pipeline";

export interface AssetMetadata {
  asset_metadata_id: string;
  asset_id: string;
  metadata_key: string;
  metadata_value: string | null;
  value_type: MetadataValueType;
  source: MetadataSource | string;
  confidence: number | null;
  created_at: string;
  updated_at: string;
}

// ── Status helpers ──
export type StatusLevel = "ok" | "degraded" | "unavailable" | "unknown";
export type StatusColor = "green" | "yellow" | "red" | "gray";

// ── Memory (P4-05 / P4-06 / P4-10) ──
export interface MemoryRead {
  memory_id: string;
  project_id: string | null;
  canonical_key: string;
  title: string | null;
  memory_text: string;
  current_version: number;
  sensitivity_level: string;
  status: string;
  activated_from_candidate_id: string | null;
  activated_by_review_item_id: string | null;
  activated_at: string | null;
  expired_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface MemoryVersionRead {
  memory_version_id: string;
  memory_id: string;
  version: number;
  action: string;
  before_json: Record<string, unknown>;
  after_json: Record<string, unknown>;
  actor_type: string;
  actor_id: string | null;
  review_item_id: string | null;
  candidate_id: string | null;
  event_id: string | null;
  reason: string | null;
  created_at: string | null;
}

export interface MemorySourceRead {
  memory_source_id: string;
  memory_id: string;
  memory_version: number;
  candidate_id: string | null;
  raw_event_id: string | null;
  asset_id: string | null;
  document_id: string | null;
  block_id: string | null;
  message_id: string | null;
  source_span: Record<string, unknown>;
  confidence: number | null;
  source_role: string;
  created_at: string | null;
}

export interface MemoryIndexEntryRead {
  memory_index_entry_id: string;
  memory_id: string;
  memory_version: number;
  project_id: string | null;
  index_profile: string;
  embedding_model_id: string | null;
  content_hash: string;
  index_text: string;
  fts_state: string;
  vector_state: string;
  ready_at: string | null;
  stale_at: string | null;
  last_error: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface MemoryIndexStateSummary {
  total_entries: number;
  fts_ready: number;
  fts_stale: number;
  fts_pending: number;
  fts_failed: number;
  vector_ready: number;
  vector_pending: number;
  vector_stale: number;
  vector_failed: number;
}

export interface MemorySearchResult {
  memory_index_entry_id: string;
  memory_id: string;
  memory_version: number;
  index_text: string;
  fts_state: string;
  vector_state: string;
  rank: number;
  title: string | null;
  memory_text: string;
  sensitivity_level: string;
  canonical_key: string;
  status: string;
  current_version: number;
}

export function statusToColor(status: string): StatusColor {
  switch (status) {
    case "ok":
    case "connected":
    case "approved":
    case "delivered":
    case "succeeded":
    case "replayed":
    case "resolved":
    case "success":
    case "completed":
    case "ready":
      return "green";
    case "degraded":
    case "standby":
    case "pending":
    case "in_review":
    case "dispatched":
    case "under_review":
    case "scheduled":
    case "in_progress":
    case "stale":
    case "staged":
    case "importing":
    case "not_started":
      return "yellow";
    case "unavailable":
    case "disconnected":
    case "stopped":
    case "rejected":
    case "cancelled":
    case "failed":
    case "denied":
    case "dead_letter":
    case "expired":
    case "retrying":
    case "archived":
    case "deleted":
    case "quarantined":
      return "red";
    default:
      return "gray";
  }
}

// ── Memory Candidate (P4-04) ──
export interface MemoryCandidate {
  candidate_id: string;
  project_id: string | null;
  source_type: string;
  source_id: string | null;
  submitted_by_actor_type: string;
  submitted_by_actor_id: string | null;
  title: string | null;
  candidate_text: string;
  candidate_hash: string;
  sensitivity_level: string;
  candidate_status: string;
  confidence_score: number | null;
  review_required: boolean;
  metadata_json: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

// ── Memory Relation (P4-08) ──
export interface MemoryRelation {
  memory_relation_id: string;
  project_id: string | null;
  from_memory_id: string;
  from_memory_version: number | null;
  to_memory_id: string;
  to_memory_version: number | null;
  relation_type: string;
  relation_status: string;
  created_by_review_item_id: string | null;
  reason: string | null;
  metadata_json: Record<string, unknown>;
  created_at: string | null;
}

// ── Conversation / Message ──
export type ConversationType = "chat" | "meeting" | "email_thread" | "system_event" | "agent_run";
export type ConversationStatus = "active" | "archived" | "deleted";
export type MessageRoleCode = "user" | "assistant" | "agent" | "system" | "tool" | "other";

export interface ConversationRead {
  conversation_id: string;
  project_id: string | null;
  owner_user_id: string | null;
  conversation_type: ConversationType | string;
  title: string | null;
  source_platform: string;
  sensitivity_level: string;
  retention_days: number | null;
  conversation_status: ConversationStatus | string;
  started_at: string | null;
  ended_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface MessageRead {
  message_id: string;
  conversation_id: string;
  event_source_id: string | null;
  parent_message_id: string | null;
  role_code: MessageRoleCode | string;
  sender_label: string | null;
  content_text: string;
  content_markdown: string | null;
  content_hash: string;
  sensitivity_level: string;
  pii_flags: Record<string, unknown>[];
  message_time: string;
  ingested_at: string;
  created_at: string;
  updated_at: string;
}

// ── Agent ──
export type AgentStatus = "active" | "disabled" | "archived" | "inactive" | "suspended" | "revoked";

export interface AgentRead {
  agent_id: string;
  project_id?: string | null;
  agent_code?: string;
  name: string;
  description: string | null;
  status: AgentStatus;
  owner_user_id?: string | null;
  store_id?: string | null;
  sensitivity_ceiling?: SensitivityLevel;
  policy_json?: Record<string, unknown>;
  disabled_at?: string | null;
  scopes?: string[];
  token_count?: number;
  created_by_user_id?: string | null;
  last_seen_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentCreateRequest {
  agent_code: string;
  name: string;
  description?: string | null;
  project_id?: string | null;
  store_id?: string | null;
  sensitivity_ceiling?: SensitivityLevel;
  policy_json?: Record<string, unknown>;
}

export interface AgentUpdatePayload {
  name?: string;
  description?: string | null;
  project_id?: string | null;
  store_id?: string | null;
  sensitivity_ceiling?: SensitivityLevel;
  policy_json?: Record<string, unknown>;
}

export interface AgentTokenRead {
  token_id: string;
  agent_id: string;
  issued_by_user_id?: string;
  name?: string;
  token_prefix: string;
  token_fingerprint?: string;
  token_hash_display?: string;
  project_scope?: string[];
  capability_scope?: string[];
  scopes?: string[];
  sensitivity_ceiling?: SensitivityLevel;
  budget_limit_daily?: string | number | null;
  rate_limit_per_min?: number | null;
  expires_at: string;
  revoked_at?: string | null;
  last_used_at: string | null;
  created_at: string;
  updated_at?: string;
}

export interface AgentTokenCreated {
  token_id: string;
  agent_id: string;
  name: string;
  token_raw: string;
  token_prefix: string;
  scopes: string[];
  expires_at: string | null;
  created_at: string;
}

// ── Context Pack ──
export interface ContextPackSummary {
  pack_id: string;
  name: string;
  description: string | null;
  memory_count: number;
  document_count: number;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ContextPackDetail extends ContextPackSummary {
  memory_ids: string[];
  document_ids: string[];
  metadata_json: Record<string, unknown>;
}

// ── Graph (P7) ──
export type GraphNodeType = "memory" | "document" | "concept" | "entity" | "agent";

export interface GraphNode {
  node_id: string;
  node_type: GraphNodeType;
  label: string;
  description: string | null;
  project_id: string | null;
  source_id: string | null;
  properties: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface GraphEdge {
  edge_id: string;
  from_node_id: string;
  to_node_id: string;
  relation_type: string;
  weight: number;
  label: string | null;
  properties: Record<string, unknown>;
  created_at: string;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  total_nodes: number;
  total_edges: number;
}

// ── Eval (P7) ──
export type EvalTaskStatus = "pending" | "running" | "completed" | "failed" | "cancelled";
export type EvalMetricAggregation = "mean" | "median" | "min" | "max" | "p50" | "p90" | "p95" | "p99";

export interface EvalTask {
  task_id: string;
  task_name: string;
  task_type: string;
  description: string | null;
  status: EvalTaskStatus;
  progress: number;
  config: Record<string, unknown>;
  total_items: number;
  processed_items: number;
  created_by_user_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface EvalResultItem {
  result_id: string;
  task_id: string;
  item_index: number;
  input: string | null;
  expected_output: string | null;
  actual_output: string | null;
  metrics: Record<string, number>;
  metadata_json: Record<string, unknown>;
  created_at: string;
}

export interface EvalMetricSummary {
  metric_name: string;
  aggregation: EvalMetricAggregation;
  value: number;
  min_value: number;
  max_value: number;
  std_dev: number;
  sample_count: number;
}

export interface EvalTaskDetail extends EvalTask {
  metrics_summary: EvalMetricSummary[];
  recent_results: EvalResultItem[];
}

// ── Memory Store (P9) ──
export type MemoryStoreType = "memory_card" | "identity" | "skill" | "rule" | "tool";

export interface MemoryStoreRead {
  store_id: string;
  agent_id: string | null;
  name: string;
  type: MemoryStoreType;
  description: string | null;
  created_at: string;
  updated_at: string;
}

export const MEMORY_STORE_TYPE_LABELS: Record<MemoryStoreType, string> = {
  memory_card: "知识卡片",
  identity: "身份",
  skill: "技能",
  rule: "规则",
  tool: "工具",
};

// ── Global Search ──
export type GlobalSearchSource = "agent" | "knowledge" | "memory";

export interface GlobalSearchResult {
  source: GlobalSearchSource;
  result_id: string;
  title: string;
  snippet: string;
  rank: number;
  icon: string;
  link: string;
  meta: Record<string, unknown>;
}

export interface GlobalSearchResponse {
  items: GlobalSearchResult[];
  total: number;
  query_time_ms: number;
  source_counts: Record<string, number>;
}

// ── Gateway / API 管理 (P2-11) ──
export interface GateProviderRead {
  provider_id: string;
  provider_code: string;
  name: string;
  provider_type: string;
  status: string;
  endpoint_base: string | null;
  config_json: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface GateProviderListResponse {
  items: GateProviderRead[];
  page_info: PageInfo;
}

export interface GateProviderModelRead {
  provider_model_id: string;
  provider_id: string;
  model_code: string;
  external_model_id: string;
  model_type: string;
  status: string;
  display_name: string | null;
  version_label: string | null;
  context_window_tokens: number | null;
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  input_price_per_1k: number | null;
  output_price_per_1k: number | null;
  currency_code: string;
  supports_streaming: boolean;
  supports_json_mode: boolean;
  supports_tools: boolean;
  supports_vision: boolean;
  sensitivity_ceiling: string;
  deprecated_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface GateProviderModelListResponse {
  items: GateProviderModelRead[];
  page_info: PageInfo;
}

export interface GateCapabilityRead {
  capability_id: string;
  capability_code: string;
  name: string;
  category: string;
  risk_level: string;
  default_budget_mode: string;
  created_at: string;
  updated_at: string;
}

export interface GateCapabilityListResponse {
  items: GateCapabilityRead[];
  page_info: PageInfo;
}

export interface GateCapabilityBindingRead {
  capability_binding_id: string;
  capability_id: string;
  provider_id: string;
  provider_model_id: string | null;
  credential_id: string | null;
  project_id: string | null;
  binding_scope: string;
  status: string;
  priority: number;
  sensitivity_floor: string;
  sensitivity_ceiling: string;
  budget_mode: string;
  require_review: boolean;
  allow_streaming: boolean;
  timeout_seconds: number | null;
  rate_limit_key: string | null;
  policy_json: Record<string, unknown> | null;
  metadata_json: Record<string, unknown> | null;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface GateCapabilityBindingListResponse {
  items: GateCapabilityBindingRead[];
  page_info: PageInfo;
}

export interface GateUsageLimitRead {
  usage_limit_id: string;
  subject_type: string;
  subject_id: string | null;
  capability_id: string | null;
  provider_id: string | null;
  project_id: string | null;
  limit_scope: string;
  window_unit: string;
  max_requests: number | null;
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  max_total_tokens: number | null;
  max_cost: number | null;
  approval_threshold_cost: number | null;
  block_threshold_cost: number | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface GateUsageLimitListResponse {
  items: GateUsageLimitRead[];
  page_info: PageInfo;
}

export interface GateLimitUsageRead {
  usage_limit_id: string;
  current_requests: number;
  current_input_tokens: number;
  current_output_tokens: number;
  current_total_tokens: number;
  current_cost: number;
  window_start: string;
  window_end: string;
}

// ── Gateway Request Types (for type safety) ──
export interface GateProviderCreate {
  provider_code: string;
  name: string;
  provider_type: string;
  endpoint_base?: string | null;
  config_json?: Record<string, unknown> | null;
}

export interface GateProviderUpdate {
  name?: string;
  provider_type?: string;
  status?: string;
  endpoint_base?: string | null;
  config_json?: Record<string, unknown> | null;
}

export interface GateProviderModelCreate {
  model_code: string;
  external_model_id: string;
  model_type: string;
  display_name?: string;
  context_window_tokens?: number;
  max_input_tokens?: number;
  max_output_tokens?: number;
  input_price_per_1k?: number;
  output_price_per_1k?: number;
  currency_code?: string;
  supports_streaming?: boolean;
  supports_json_mode?: boolean;
  supports_tools?: boolean;
  supports_vision?: boolean;
  sensitivity_ceiling?: string;
}

export interface GateCapabilityCreate {
  capability_code: string;
  name: string;
  category: string;
  risk_level: string;
  default_budget_mode: string;
}

export interface GateCapabilityBindingCreate {
  capability_id: string;
  provider_id: string;
  provider_model_id?: string;
  credential_id?: string;
  project_id?: string;
  binding_scope?: string;
  status?: string;
  priority?: number;
  sensitivity_floor?: string;
  sensitivity_ceiling?: string;
  budget_mode?: string;
  require_review?: boolean;
  allow_streaming?: boolean;
  timeout_seconds?: number;
}

export interface GateCapabilityBindingUpdate {
  status?: string;
  priority?: number;
  require_review?: boolean;
  allow_streaming?: boolean;
  timeout_seconds?: number;
  budget_mode?: string;
}

export interface GateUsageLimitCreate {
  subject_type: string;
  subject_id?: string;
  capability_id?: string;
  provider_id?: string;
  project_id?: string;
  limit_scope: string;
  window_unit: string;
  max_requests?: number;
  max_input_tokens?: number;
  max_output_tokens?: number;
  max_total_tokens?: number;
  max_cost?: number;
  enabled?: boolean;
}

export interface GateUsageLimitUpdate {
  max_requests?: number;
  max_input_tokens?: number;
  max_output_tokens?: number;
  max_total_tokens?: number;
  max_cost?: number;
  enabled?: boolean;
  window_unit?: string;
}

// ── Projects ──
export interface ProjectRead {
  project_id: string;
  project_code: string;
  name: string;
  description: string | null;
  status: "active" | "archived" | "disabled";
  sensitivity_default: string;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

export interface ProjectCreateRequest {
  project_code: string;
  name: string;
  description?: string | null;
  sensitivity_default?: string;
}

export interface ProjectUpdateRequest {
  name?: string;
  description?: string | null;
  sensitivity_default?: string;
}

export interface ProjectListResponse {
  items: ProjectRead[];
  page_info: PageInfo;
}

// ── Pipeline Defs (from /api/v4/pipelines/defs) ──
export type PipelineDefStatus = "draft" | "active" | "disabled" | "archived";

export type PipelineType =
  | "asset_import"
  | "knowledge_index"
  | "memory_extract"
  | "backup"
  | "restore"
  | "importer"
  | "maintenance";

export interface PipelineDefRead {
  pipeline_def_id: string;
  project_id: string | null;
  pipeline_code: string;
  pipeline_type: PipelineType;
  version: number;
  name: string;
  description: string | null;
  config_json: Record<string, unknown>;
  status: PipelineDefStatus;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
}

export const PIPELINE_TYPE_LABELS: Record<PipelineType, string> = {
  asset_import: "资产导入",
  knowledge_index: "知识索引",
  memory_extract: "记忆提取",
  backup: "备份",
  restore: "恢复",
  importer: "导入器",
  maintenance: "维护",
};

export const PIPELINE_STATUS_LABELS: Record<PipelineDefStatus, string> = {
  draft: "草稿",
  active: "活跃",
  disabled: "已禁用",
  archived: "已归档",
};

export const INDEX_STATE_LABELS: Record<string, string> = {
  ready: "已就绪",
  pending: "等待生成",
  stale: "已过期",
  failed: "生成失败",
  not_started: "未生成",
};

export const INGEST_STATE_LABELS: Record<string, string> = {
  pending: "等待上传",
  staged: "已上传",
  importing: "导入中",
  ready: "已就绪",
  failed: "导入失败",
};

// ── Sub-Library (knowledge store backends — from /api/v4/sub-libraries) ──
export interface SubLibraryRead {
  id: string;
  name: string;
  type: string; // "vector" | "graph" | "fulltext" | "custom"
  key: string;
  capability_json: Record<string, unknown>;
  metadata_json: Record<string, unknown>;
  created_at: string | null;
}

export interface SubLibraryListResponse {
  items: SubLibraryRead[];
  page_info: PageInfo;
}

export const SUB_LIBRARY_TYPE_LABELS: Record<string, string> = {
  vector: "向量库",
  graph: "知识图谱",
  fulltext: "全文索引",
  custom: "自定义",
};

export const SUB_LIBRARY_TYPE_ICONS: Record<string, string> = {
  vector: "🧠",
  graph: "🔗",
  fulltext: "🔍",
  custom: "⚙️",
};

// ── Processing Job (from /api/v4/import) ──
export interface ProcessingJobRead {
  id: string;
  asset_id: string;
  pipeline_id: string;
  target_stores: string[];
  status: string;
  chunks_produced: number;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string | null;
}

export interface ProcessingJobStatus {
  job_id: string;
  asset_id: string;
  status: string;
  chunks_produced: number;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string | null;
}

// ── Agent Cards (P8-02) ──
export type AgentCardType = "identity" | "soul" | "tool" | "user_profile";
export type AgentCardStatus = "active" | "disabled" | "archived";
export type ToolItemStatus = "active" | "disabled" | "archived";

export interface AgentCardRead {
  card_id: string;
  agent_id: string | null;
  card_type: AgentCardType;
  name: string;
  description: string | null;
  content_json: Record<string, unknown>;
  status: AgentCardStatus;
  display_order: number;
  tool_count: number;
  created_at: string;
  updated_at: string;
}

export interface AgentCardCreateRequest {
  agent_id?: string | null;
  card_type: AgentCardType;
  name: string;
  description?: string | null;
  content_json?: Record<string, unknown>;
  display_order?: number;
}

export interface AgentCardUpdateRequest {
  agent_id?: string | null;
  name?: string | null;
  description?: string | null;
  content_json?: Record<string, unknown> | null;
  status?: AgentCardStatus | null;
  display_order?: number | null;
}

export interface AgentToolItemRead {
  item_id: string;
  card_id: string;
  name: string;
  description: string | null;
  tool_type: string | null;
  config_json: Record<string, unknown>;
  input_schema: Record<string, unknown> | null;
  output_schema: Record<string, unknown> | null;
  status: ToolItemStatus;
  display_order: number;
  created_at: string;
  updated_at: string;
}

export interface AgentToolItemCreateRequest {
  card_id: string;
  name: string;
  description?: string | null;
  tool_type?: string | null;
  config_json?: Record<string, unknown>;
  input_schema?: Record<string, unknown> | null;
  output_schema?: Record<string, unknown> | null;
  display_order?: number;
}

export interface AgentToolItemUpdateRequest {
  name?: string | null;
  description?: string | null;
  tool_type?: string | null;
  config_json?: Record<string, unknown> | null;
  input_schema?: Record<string, unknown> | null;
  output_schema?: Record<string, unknown> | null;
  status?: ToolItemStatus | null;
  display_order?: number | null;
}

export const CARD_TYPE_LABELS: Record<AgentCardType, string> = {
  identity: "🪪 身份卡",
  soul: "💫 灵魂卡",
  tool: "🔧 工具卡",
  user_profile: "👤 用户画像",
};

export const TOOL_TYPE_LABELS: Record<string, string> = {
  api: "API 接口",
  function: "函数调用",
  script: "脚本执行",
  builtin: "内置能力",
  mcp: "MCP 协议",
};

// ── Admin Logs ──
export interface AdminLogEntry {
  api_call_log_id: string;
  request_id: string | null;
  correlation_id: string | null;
  actor_type: string;
  provider_id: string | null;
  call_type: string;
  call_state: string;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  latency_ms: number | null;
  error_code: string | null;
  error_message: string | null;
  retry_count: number;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
}

// ── Context Assembly (P8-01) ──
export type InjectionStrategy = "always" | "moderate" | "on_demand";

export interface AssembleRequest {
  agent_id: string;
  query_text: string;
  project_id?: string | null;
  conversation_history?: string | null;
  max_tokens?: number | null;
  strategy_overrides?: Record<string, InjectionStrategy> | null;
  expand_cards?: string[] | null;
}

export interface CardSection {
  card_type: string;
  store_id: string | null;
  store_name: string | null;
  strategy: InjectionStrategy;
  content: string;
  token_count: number;
  memory_ids: string[];
  truncated: boolean;
}

export interface BudgetBreakdown {
  total_available: number;
  system_overhead: number;
  output_reserve: number;
  usable: number;
  always_used: number;
  moderate_used: number;
  on_demand_used: number;
  remaining: number;
}

export interface AssembleResponse {
  agent_id: string;
  assembled_text: string;
  sections: CardSection[];
  budget: BudgetBreakdown;
  total_tokens: number;
  strategy_summary: Record<string, string>;
  degradation_reason: string | null;
}

// ── Old Ingest types (for AssetTab.vue backward compat) ──
export interface IngestPipeline {
  pipeline_key: string;
  name: string;
  icon: string;
  description: string;
  supported_formats: string[];
}

export interface IngestStore {
  store_key: string;
  name: string;
  icon: string;
  description: string;
  type: string;
}

export interface IngestProcessItem {
  asset_id: string;
  status: string;
  chunks_produced: number;
  error: string | null;
  title?: string;
  original_filename?: string;
  progress_percent?: number;
  started_at?: string | null;
  completed_at?: string | null;
}
