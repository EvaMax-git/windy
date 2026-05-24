import type { ApiEnvelope, ApiErrorEnvelope } from "@/types";

const BASE_URL = "/api/v4";
const HEALTH_BASE = "";

/**
 * Generate a simple UUID v4 for request-id when none is provided.
 */
function generateRequestId(): string {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

export interface ApiRequestOptions {
  method?: string;
  body?: unknown;
  params?: Record<string, string | number | boolean | undefined>;
  headers?: Record<string, string>;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details: Record<string, unknown> = {},
    public requestId?: string,
    public correlationId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(
  path: string,
  options: ApiRequestOptions = {},
  useBaseUrl = true,
): Promise<T> {
  const { method = "GET", body, params, headers: extraHeaders } = options;

  const requestId = generateRequestId();
  // Concatenate base + path before URL resolution to preserve the /api/v4 prefix.
  // new URL("/auth/login", "/api/v4") would strip the prefix and produce
  // http://host/auth/login instead of http://host/api/v4/auth/login.
  const fullPath = (useBaseUrl ? BASE_URL : "") + path;
  const url = new URL(fullPath, window.location.origin);

  if (params) {
    Object.entries(params).forEach(([key, val]) => {
      if (val !== undefined && val !== "") {
        url.searchParams.set(key, String(val));
      }
    });
  }

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Request-Id": requestId,
    ...extraHeaders,
  };

  const fetchOptions: RequestInit = {
    method,
    headers,
    credentials: "include",
  };

  if (body && method !== "GET") {
    fetchOptions.body = JSON.stringify(body);
  }

  const response = await fetch(url.toString(), fetchOptions);

  if (!response.ok) {
    let errorData: ApiErrorEnvelope | null = null;
    try {
      errorData = await response.json();
    } catch {
      // non-JSON error response
    }

    if (errorData?.error) {
      throw new ApiError(
        response.status,
        errorData.error.code,
        errorData.error.message,
        errorData.error.details,
        errorData.request_id,
        errorData.correlation_id,
      );
    }

    throw new ApiError(
      response.status,
      "unknown",
      `请求失败，状态码 ${response.status}`,
    );
  }

  const json = await response.json();
  return json as T;
}

/**
 * API v4 request — returns the full envelope.
 */
export async function apiRequest<T>(path: string, options?: ApiRequestOptions): Promise<ApiEnvelope<T>> {
  return request<ApiEnvelope<T>>(path, options);
}

/**
 * API v4 request — extracts and returns just the `data` field from the envelope.
 */
export async function apiData<T>(path: string, options?: ApiRequestOptions): Promise<T> {
  const envelope = await apiRequest<T>(path, options);
  return envelope.data;
}

/**
 * Health check request — does NOT use `/api/v4` prefix.
 */
export async function healthRequest<T>(path: string): Promise<T> {
  return request<T>(path, {}, false);
}

// ── Specific API helpers ──────────────────────────────────────────────────────

import type {
  HealthReadyData,
  AuditEvent,
  EventRead,
  ReviewItem,
  DeadLetter,
  PaginatedData,
  PageInfo,
  LoginRequest,
  LoginResponse,
  MeResponse,
  LogoutResponse,
} from "@/types";

// ── Auth ──
export async function login(payload: LoginRequest): Promise<LoginResponse> {
  return apiData<LoginResponse>("/auth/login", {
    method: "POST",
    body: payload,
  });
}

export async function me(): Promise<MeResponse> {
  return apiData<MeResponse>("/auth/me");
}

export async function logoutApi(reason?: string): Promise<LogoutResponse> {
  return apiData<LogoutResponse>("/auth/logout", {
    method: "POST",
    body: { revoke_reason: reason },
  });
}

// ── Health ──
export function fetchHealthReady(): Promise<HealthReadyData> {
  return healthRequest<HealthReadyData>("/health/ready");
}

// ── Dashboard Stats ──
export interface DashboardStats {
  agents: AgentRead[];
  total_memories: number;
  pending_candidates: number;
  pending_reviews: number;
  total_documents: number;
  sensitivity_distribution: Record<string, number>;
  recent_activity: AuditEvent[];
}

export async function fetchDashboardStats(): Promise<DashboardStats> {
  return apiData<DashboardStats>("/dashboard/stats");
}

export function fetchDashboardHealthSummary(): Promise<HealthReadyData> {
  return apiData<HealthReadyData>("/dashboard/health-summary");
}

// ── Audit ──
export interface AuditFilterParams {
  page?: number;
  page_size?: number;
  actor_type?: string;
  action?: string;
  result?: string;
  occurred_after?: string;
  occurred_before?: string;
}

export async function fetchAuditEvents(
  params: AuditFilterParams = {},
): Promise<PaginatedData<AuditEvent>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    actor_type: params.actor_type,
    action: params.action,
    result: params.result,
    occurred_after: params.occurred_after,
    occurred_before: params.occurred_before,
  };
  return apiData<PaginatedData<AuditEvent>>("/admin/audit-events", {
    params: queryParams,
  });
}

// ── Events / Outbox ──
export interface EventFilterParams {
  page?: number;
  page_size?: number;
  event_type?: string;
  publish_state?: string;
  aggregate_type?: string;
  occurred_after?: string;
  occurred_before?: string;
}

export async function fetchEvents(
  params: EventFilterParams = {},
): Promise<PaginatedData<EventRead>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    event_type: params.event_type,
    publish_state: params.publish_state,
    aggregate_type: params.aggregate_type,
    occurred_after: params.occurred_after,
    occurred_before: params.occurred_before,
  };
  return apiData<PaginatedData<EventRead>>("/admin/events", {
    params: queryParams,
  });
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

export async function fetchEventDetail(eventId: string): Promise<EventDetail> {
  return apiData<EventDetail>(`/admin/events/${eventId}`);
}

// ── Review Items ──
export interface ReviewFilterParams {
  page?: number;
  page_size?: number;
  review_type?: string;
  status?: string;
  created_after?: string;
  created_before?: string;
}

export async function fetchReviewItems(
  params: ReviewFilterParams = {},
): Promise<PaginatedData<ReviewItem>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    review_type: params.review_type,
    status: params.status,
    created_after: params.created_after,
    created_before: params.created_before,
  };
  return apiData<PaginatedData<ReviewItem>>("/review/items", {
    params: queryParams,
  });
}

export async function fetchReviewItem(id: string): Promise<ReviewItem> {
  return apiData<ReviewItem>(`/review/items/${id}`);
}

export async function approveReviewItem(
  id: string,
  reason?: string,
): Promise<ReviewItem> {
  return apiData<ReviewItem>(`/review/items/${id}/approve`, {
    method: "POST",
    body: { reason: reason || "Approved" },
  });
}

export async function rejectReviewItem(
  id: string,
  reason?: string,
): Promise<ReviewItem> {
  return apiData<ReviewItem>(`/review/items/${id}/reject`, {
    method: "POST",
    body: { reason: reason || "Rejected" },
  });
}

// ── Dead Letters ──
export interface DeadLetterFilterParams {
  page?: number;
  page_size?: number;
  failure_class?: string;
  replay_state?: string;
  source_type?: string;
  created_after?: string;
  created_before?: string;
}

export async function fetchDeadLetters(
  params: DeadLetterFilterParams = {},
): Promise<PaginatedData<DeadLetter>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    failure_class: params.failure_class,
    replay_state: params.replay_state,
    source_type: params.source_type,
    created_after: params.created_after,
    created_before: params.created_before,
  };
  return apiData<PaginatedData<DeadLetter>>("/admin/dead-letters", {
    params: queryParams,
  });
}

export async function replayDeadLetter(id: string): Promise<{ review_item_id: string }> {
  return apiData<{ review_item_id: string }>(`/admin/dead-letters/${id}/replay`, {
    method: "POST",
  });
}

// ── Jobs ──
import type {
  JobSummary,
  JobDetail,
  BackupSummary,
  BackupDetail,
  BackupTriggerResponse,
  RestoreSubmitResponse,
  RestoreDetailedPreview,
  RestoreSummary,
  RestoreDrillResponse,
} from "@/types";

export interface JobFilterParams {
  page?: number;
  page_size?: number;
  status?: string;
}

export async function fetchJobs(
  params: JobFilterParams = {},
): Promise<PaginatedData<JobSummary>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    status: params.status,
  };
  return apiData<PaginatedData<JobSummary>>("/admin/jobs", {
    params: queryParams,
  });
}

export async function fetchJobDetail(id: string): Promise<JobDetail> {
  return apiData<JobDetail>(`/admin/jobs/${id}`);
}

// ── Backup / Restore ──
export async function fetchBackups(
  params: { page?: number; page_size?: number } = {},
): Promise<PaginatedData<BackupSummary>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
  };
  return apiData<PaginatedData<BackupSummary>>("/admin/backups", {
    params: queryParams,
  });
}

export async function fetchBackupDetail(id: string): Promise<BackupDetail> {
  return apiData<BackupDetail>(`/admin/backups/${id}`);
}

export async function triggerBackup(): Promise<BackupTriggerResponse> {
  return apiData<BackupTriggerResponse>("/admin/backup", {
    method: "POST",
  });
}

export async function previewRestore(
  backupId: string,
): Promise<RestoreDetailedPreview> {
  return apiData<RestoreDetailedPreview>(
    `/admin/restore/${backupId}/preview`,
  );
}

export async function submitRestore(
  backupId: string,
  reason?: string,
): Promise<RestoreSubmitResponse> {
  return apiData<RestoreSubmitResponse>("/admin/restore", {
    method: "POST",
    body: { backup_id: backupId, reason: reason || "Restore requested via governance UI" },
  });
}

export async function fetchRestores(
  params: { page?: number; page_size?: number } = {},
): Promise<PaginatedData<RestoreSummary>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
  };
  return apiData<PaginatedData<RestoreSummary>>("/admin/restores", {
    params: queryParams,
  });
}

export async function executeRestoreDrill(
  backupId: string,
  keepTempDb = false,
): Promise<RestoreDrillResponse> {
  return apiData<RestoreDrillResponse>("/admin/restores/drill", {
    method: "POST",
    body: { backup_id: backupId, keep_temp_db: keepTempDb },
  });
}

// ── Knowledge (P3-10) ──
import type {
  KnowledgeDocument,
  KnowledgeBlock,
  KnowledgeChunk,
  KnowledgeFtsSearchResult,
  Citation,
  IndexState,
} from "@/types";

export interface KnowledgeDocumentFilterParams {
  page?: number;
  page_size?: number;
  project_id?: string;
  status?: string;
  sub_library_id?: string;
}

export async function fetchKnowledgeDocuments(
  params: KnowledgeDocumentFilterParams = {},
): Promise<PaginatedData<KnowledgeDocument>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    project_id: params.project_id,
    status: params.status,
    sub_library_id: params.sub_library_id,
  };
  return apiData<PaginatedData<KnowledgeDocument>>("/knowledge/documents", {
    params: queryParams,
  });
}

export async function fetchKnowledgeDocument(
  documentId: string,
): Promise<KnowledgeDocument> {
  return apiData<KnowledgeDocument>(`/knowledge/documents/${documentId}`);
}

export async function fetchDocumentBlocks(
  documentId: string,
): Promise<KnowledgeBlock[]> {
  return apiData<KnowledgeBlock[]>(`/knowledge/documents/${documentId}/blocks`);
}

export async function fetchDocumentChunks(
  documentId: string,
  documentVersion?: number,
): Promise<KnowledgeChunk[]> {
  const queryParams: Record<string, string | number | boolean | undefined> = {};
  if (documentVersion !== undefined) queryParams.document_version = documentVersion;
  return apiData<KnowledgeChunk[]>(`/knowledge/documents/${documentId}/chunks`, {
    params: queryParams,
  });
}

export interface KnowledgeSearchParams {
  q: string;
  project_id?: string;
  sensitivity_floor?: string;
  page?: number;
  page_size?: number;
}

export async function searchKnowledge(
  params: KnowledgeSearchParams,
): Promise<PaginatedData<KnowledgeFtsSearchResult>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    q: params.q,
    project_id: params.project_id,
    sensitivity_floor: params.sensitivity_floor,
    page: params.page || 1,
    page_size: params.page_size || 20,
  };
  return apiData<PaginatedData<KnowledgeFtsSearchResult>>("/knowledge/search", {
    params: queryParams,
  });
}

export async function fetchCitation(chunkId: string): Promise<Citation> {
  return apiData<Citation>(`/knowledge/citations/${chunkId}`);
}

export async function fetchDocumentCitations(
  documentId: string,
): Promise<{ document_id: string; citations: Citation[]; total: number }> {
  return apiData<{ document_id: string; citations: Citation[]; total: number }>(
    `/knowledge/documents/${documentId}/citations`,
  );
}

export async function fetchDocumentIndexState(
  documentId: string,
): Promise<IndexState> {
  return apiData<IndexState>(`/knowledge/documents/${documentId}/index-state`);
}

export async function updateKnowledgeDocument(
  documentId: string,
  payload: { title?: string; summary?: string; sensitivity_level?: string; sub_library_id?: string },
): Promise<KnowledgeDocument> {
  return apiData<KnowledgeDocument>(`/knowledge/documents/${documentId}`, {
    method: "PATCH",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function archiveKnowledgeDocument(
  documentId: string,
): Promise<KnowledgeDocument> {
  return apiData<KnowledgeDocument>(`/knowledge/documents/${documentId}/archive`, {
    method: "POST",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function rechunkDocument(
  documentId: string,
  payload?: { strategy?: string; max_tokens_per_chunk?: number },
): Promise<KnowledgeChunk[]> {
  return apiData<KnowledgeChunk[]>(`/knowledge/documents/${documentId}/rechunk`, {
    method: "POST",
    body: payload || {},
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function refreshKnowledgeIndexes(): Promise<{ refreshed: number }> {
  return apiData<{ refreshed: number }>("/knowledge/indexes/refresh", {
    method: "POST",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function addDocumentBlock(
  documentId: string,
  payload: { block_order: number; block_type: string; content_markdown: string },
): Promise<KnowledgeBlock> {
  return apiData<KnowledgeBlock>(`/knowledge/documents/${documentId}/blocks`, {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function deleteDocumentBlock(
  blockId: string,
): Promise<{ deleted: boolean }> {
  return apiData<{ deleted: boolean }>(`/knowledge/blocks/${blockId}`, {
    method: "DELETE",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

// ── Assets (P3-11) ──
import type {
  Asset,
  AssetMetadata,
} from "@/types";

export interface AssetFilterParams {
  page?: number;
  page_size?: number;
  project_id?: string;
  asset_type?: string;
  knowledge_state?: string;
  sensitivity_level?: string;
  status?: string;
  ingest_state?: string;
}

export async function fetchAssets(
  params: AssetFilterParams = {},
): Promise<PaginatedData<Asset>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    project_id: params.project_id,
    asset_type: params.asset_type,
    knowledge_state: params.knowledge_state,
    sensitivity_level: params.sensitivity_level,
    status: params.status,
    ingest_state: params.ingest_state,
  };
  return apiData<PaginatedData<Asset>>("/assets", { params: queryParams });
}

export async function fetchAsset(assetId: string): Promise<Asset> {
  return apiData<Asset>(`/assets/${assetId}`);
}

export async function updateAsset(
  assetId: string,
  payload: { title?: string; sensitivity_level?: string; retention_policy?: string },
): Promise<Asset> {
  return apiData<Asset>(`/assets/${assetId}`, {
    method: "PATCH",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function deleteAsset(assetId: string): Promise<Asset> {
  return apiData<Asset>(`/assets/${assetId}`, {
    method: "DELETE",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function restoreAsset(assetId: string): Promise<Asset> {
  return apiData<Asset>(`/assets/${assetId}/restore`, {
    method: "POST",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function uploadAsset(
  file: File,
  projectId?: string,
  options?: {
    title?: string;
    asset_type?: string;
    sensitivity_level?: string;
    retention_policy?: string;
    onProgress?: (percent: number) => void;
  },
): Promise<Asset> {
  const formData = new FormData();
  formData.append("file", file);
  if (projectId) formData.append("project_id", projectId);
  if (options?.title) formData.append("title", options.title);
  if (options?.asset_type) formData.append("asset_type", options.asset_type);
  if (options?.sensitivity_level) formData.append("sensitivity_level", options.sensitivity_level);
  if (options?.retention_policy) formData.append("retention_policy", options.retention_policy);

  const requestId = generateRequestId();

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE_URL}/assets/ingest`);
    xhr.setRequestHeader("X-Request-Id", requestId);
    xhr.setRequestHeader("Idempotency-Key", generateRequestId());
    xhr.withCredentials = true;

    if (options?.onProgress) {
      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) {
          options.onProgress!(Math.round((e.loaded / e.total) * 100));
        }
      });
    }

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const envelope = JSON.parse(xhr.responseText);
          resolve(envelope.data as Asset);
        } catch {
          reject(new Error("解析响应失败"));
        }
      } else {
        let message = `上传失败，状态码 ${xhr.status}`;
        try {
          const errorData = JSON.parse(xhr.responseText);
          if (errorData?.error?.message) {
            message = errorData.error.message;
          }
        } catch { /* ignore */ }
        reject(new Error(message));
      }
    });

    xhr.addEventListener("error", () => {
      reject(new Error("网络连接失败"));
    });

    xhr.addEventListener("abort", () => {
      reject(new Error("上传已取消"));
    });

    xhr.send(formData);
  });
}

/** Upload a file via /import endpoint to trigger auto-parse pipeline (A1 MVP). */
export async function uploadAndImport(
  file: File,
  options?: {
    projectId?: string;
    pipelineKey?: string;
    onProgress?: (percent: number) => void;
  },
): Promise<{ job_id: string; asset_uid?: string }> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("pipeline_key", options?.pipelineKey || "standard_chunk");
  if (options?.projectId) formData.append("project_id", options.projectId);

  const requestId = generateRequestId();

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE_URL}/import`);
    xhr.setRequestHeader("X-Request-Id", requestId);
    xhr.setRequestHeader("Idempotency-Key", generateRequestId());
    xhr.withCredentials = true;

    if (options?.onProgress) {
      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) {
          options.onProgress!(Math.round((e.loaded / e.total) * 100));
        }
      });
    }

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        const resp = JSON.parse(xhr.responseText);
        resolve(resp.data);
      } else {
        let message = `导入失败 (${xhr.status})`;
        try {
          const err = JSON.parse(xhr.responseText);
          message = err.message || err.detail || message;
        } catch {}
        reject(new Error(message));
      }
    });

    xhr.addEventListener("error", () => reject(new Error("网络连接失败")));
    xhr.addEventListener("abort", () => reject(new Error("上传已取消")));

    xhr.send(formData);
  });
}

/** Poll import job status. */
export async function getImportJobStatus(
  jobId: string,
): Promise<{ status: string; progress?: number; error?: string }> {
  return apiData(`/import/${jobId}/status`);
}

/** Ask a question (A3 MVP). */
export async function askQuestion(params: {
  question: string;
  project_id?: string;
  max_citations?: number;
  sensitivity_floor?: string;
}): Promise<{
  answer: string;
  citations: Array<{ chunk_id: string; document_title: string; snippet: string; rank: number }>;
  model: string | null;
  degraded: boolean;
  degradation_reason: string | null;
}> {
  return apiData("/ask", {
    method: "POST",
    body: params,
  });
}

/** Multi-turn chat (A4 MVP). */
export async function chat(params: {
  message: string;
  conversation_id?: string;
  project_id?: string;
  max_context_chunks?: number;
}): Promise<{
  conversation_id: string;
  message_id: string;
  answer: string;
  citations: Array<{ chunk_id: string; document_title: string; snippet: string }>;
  model: string | null;
  degraded: boolean;
}> {
  return apiData("/chat", {
    method: "POST",
    body: params,
  });
}

export async function fetchAssetMetadata(assetId: string): Promise<AssetMetadata[]> {
  return apiData<AssetMetadata[]>(`/assets/${assetId}/metadata`);
}

export async function addAssetMetadata(
  assetId: string,
  payload: {
    metadata_key: string;
    metadata_value?: string;
    value_type?: string;
    source?: string;
    confidence?: number;
  },
): Promise<AssetMetadata> {
  return apiData<AssetMetadata>(`/assets/${assetId}/metadata`, {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function updateAssetMetadata(
  assetId: string,
  metadataId: string,
  payload: {
    metadata_value?: string;
    value_type?: string;
    confidence?: number;
  },
): Promise<AssetMetadata> {
  return apiData<AssetMetadata>(`/assets/${assetId}/metadata/${metadataId}`, {
    method: "PATCH",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function deleteAssetMetadata(
  assetId: string,
  metadataId: string,
): Promise<AssetMetadata> {
  return apiData<AssetMetadata>(`/assets/${assetId}/metadata/${metadataId}`, {
    method: "DELETE",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

// Re-export types for convenience
export type {
  JobSummary,
  JobDetail,
  BackupSummary,
  BackupDetail,
  BackupTriggerResponse,
  RestoreSubmitResponse,
  RestoreDetailedPreview,
  RestoreSummary,
  RestoreDrillResponse,
} from "@/types";

export type {
  KnowledgeDocument,
  KnowledgeBlock,
  KnowledgeChunk,
  KnowledgeFtsSearchResult,
  Citation,
  IndexState,
} from "@/types";

// ── Memory (P4-05 / P4-06 / P4-10) ──
import type {
  MemoryRead,
  MemoryVersionRead,
  MemorySourceRead,
  MemoryIndexEntryRead,
  MemoryIndexStateSummary,
  MemorySearchResult,
  MemoryCandidate,
  MemoryRelation,
} from "@/types";

export interface MemoryFilterParams {
  page?: number;
  page_size?: number;
  project_id?: string;
  status?: string;
  sensitivity_level?: string;
  search?: string;
}

export async function fetchMemories(
  params: MemoryFilterParams = {},
): Promise<PaginatedData<MemoryRead>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    project_id: params.project_id,
    status: params.status,
    sensitivity_level: params.sensitivity_level,
    search: params.search,
  };
  return apiData<PaginatedData<MemoryRead>>("/memory", { params: queryParams });
}

export async function fetchMemory(memoryId: string): Promise<MemoryRead> {
  return apiData<MemoryRead>(`/memory/${memoryId}`);
}

export async function fetchMemoryVersions(
  memoryId: string,
  params: { page?: number; page_size?: number } = {},
): Promise<PaginatedData<MemoryVersionRead>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
  };
  return apiData<PaginatedData<MemoryVersionRead>>(`/memory/${memoryId}/versions`, {
    params: queryParams,
  });
}

export async function fetchMemorySpecificVersion(
  memoryId: string,
  version: number,
): Promise<MemoryVersionRead> {
  return apiData<MemoryVersionRead>(`/memory/${memoryId}/versions/${version}`);
}

export async function fetchMemorySources(
  memoryId: string,
): Promise<{ items: MemorySourceRead[] }> {
  return apiData<{ items: MemorySourceRead[] }>(`/memory/${memoryId}/sources`);
}

export async function fetchMemoryIndexEntries(
  params: { page?: number; page_size?: number; project_id?: string; fts_state?: string; memory_id?: string } = {},
): Promise<PaginatedData<MemoryIndexEntryRead>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    project_id: params.project_id,
    fts_state: params.fts_state,
    memory_id: params.memory_id,
  };
  return apiData<PaginatedData<MemoryIndexEntryRead>>("/memory/index/entries", {
    params: queryParams,
  });
}

export async function fetchMemoryIndexStatus(
  projectId?: string,
): Promise<MemoryIndexStateSummary> {
  const queryParams: Record<string, string | number | boolean | undefined> = {};
  if (projectId) queryParams.project_id = projectId;
  return apiData<MemoryIndexStateSummary>("/memory/index/status", {
    params: queryParams,
  });
}

export async function searchMemories(
  params: { q: string; project_id?: string; page?: number; page_size?: number },
): Promise<PaginatedData<MemorySearchResult>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    q: params.q,
    project_id: params.project_id,
    page: params.page || 1,
    page_size: params.page_size || 20,
  };
  return apiData<PaginatedData<MemorySearchResult>>("/memory/search", {
    params: queryParams,
  });
}

export async function expireMemory(memoryId: string, reason?: string): Promise<MemoryRead> {
  return apiData<MemoryRead>(`/memory/${memoryId}/expire`, {
    method: "POST",
    body: { reason },
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function restoreMemory(memoryId: string, reason?: string): Promise<MemoryRead> {
  return apiData<MemoryRead>(`/memory/${memoryId}/restore`, {
    method: "POST",
    body: { reason },
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function deleteMemory(memoryId: string): Promise<{ deleted: boolean; memory_id: string; status: string }> {
  return apiData<{ deleted: boolean; memory_id: string; status: string }>(`/memory/${memoryId}`, {
    method: "DELETE",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function createMemory(payload: {
  project_id: string;
  title?: string;
  memory_text: string;
  sensitivity_level?: string;
}): Promise<MemoryRead> {
  return apiData<MemoryRead>("/memory", {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function updateMemory(
  memoryId: string,
  payload: { title?: string; memory_text?: string; sensitivity_level?: string },
): Promise<MemoryRead> {
  return apiData<MemoryRead>(`/memory/${memoryId}`, {
    method: "PATCH",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function mergeMemory(
  memoryId: string,
  targetMemoryId: string,
): Promise<MemoryRead> {
  return apiData<MemoryRead>(`/memory/${memoryId}/merge`, {
    method: "POST",
    body: { target_memory_id: targetMemoryId },
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

// ── Memory Relations (P4-08) ──
export async function fetchMemoryRelations(
  memoryId: string,
): Promise<PaginatedData<MemoryRelation>> {
  return apiData<PaginatedData<MemoryRelation>>(`/memory/${memoryId}/relations`);
}

// ── Memory Candidates (P4-04) ──
export interface CandidateFilterParams {
  page?: number;
  page_size?: number;
  project_id?: string;
  source_type?: string;
  candidate_status?: string;
}

export async function fetchCandidates(
  params: CandidateFilterParams = {},
): Promise<PaginatedData<MemoryCandidate>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    project_id: params.project_id,
    source_type: params.source_type,
    candidate_status: params.candidate_status,
  };
  return apiData<PaginatedData<MemoryCandidate>>("/memory/candidates", {
    params: queryParams,
  });
}

export async function fetchCandidate(candidateId: string): Promise<MemoryCandidate> {
  return apiData<MemoryCandidate>(`/memory/candidates/${candidateId}`);
}

export async function approveCandidate(
  candidateId: string,
  reason?: string,
): Promise<MemoryCandidate> {
  return apiData<MemoryCandidate>(`/memory/candidates/${candidateId}/approve`, {
    method: "POST",
    body: { reason: reason || "Approved" },
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function rejectCandidate(
  candidateId: string,
  reason?: string,
): Promise<MemoryCandidate> {
  return apiData<MemoryCandidate>(`/memory/candidates/${candidateId}/reject`, {
    method: "POST",
    body: { reason: reason || "Rejected" },
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

// ── Conversations / Messages ──
import type {
  ConversationRead,
  MessageRead,
} from "@/types";

export interface ConversationFilterParams {
  page?: number;
  page_size?: number;
  project_id?: string;
  conversation_type?: string;
  conversation_status?: string;
}

export async function fetchConversations(
  params: ConversationFilterParams = {},
): Promise<PaginatedData<ConversationRead>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    project_id: params.project_id,
    conversation_type: params.conversation_type,
    conversation_status: params.conversation_status,
  };
  return apiData<PaginatedData<ConversationRead>>("/conversations", {
    params: queryParams,
  });
}

export async function fetchConversation(
  conversationId: string,
): Promise<ConversationRead> {
  return apiData<ConversationRead>(`/conversations/${conversationId}`);
}

export interface MessageFilterParams {
  page?: number;
  page_size?: number;
}

export async function fetchConversationMessages(
  conversationId: string,
  params: MessageFilterParams = {},
): Promise<PaginatedData<MessageRead>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
  };
  return apiData<PaginatedData<MessageRead>>(
    `/conversations/${conversationId}/messages`,
    { params: queryParams },
  );
}

export async function fetchConversationMessage(
  conversationId: string,
  messageId: string,
): Promise<MessageRead> {
  return apiData<MessageRead>(
    `/conversations/${conversationId}/messages/${messageId}`,
  );
}

export type {
  Asset,
  AssetMetadata,
  ConversationRead,
  MessageRead,
} from "@/types";

// ── Agent ──
import type {
  AgentRead,
  AgentTokenRead,
  AgentTokenCreated,
  AgentUpdatePayload,
} from "@/types";

export interface AgentFilterParams {
  page?: number;
  page_size?: number;
  status?: string;
}

export async function fetchAgents(
  params: AgentFilterParams = {},
): Promise<PaginatedData<AgentRead>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    status: params.status,
  };
  return apiData<PaginatedData<AgentRead>>("/agents", { params: queryParams });
}

export async function fetchAgent(agentId: string): Promise<AgentRead> {
  return apiData<AgentRead>(`/agents/${agentId}`);
}

export async function updateAgent(
  agentId: string,
  payload: AgentUpdatePayload,
): Promise<AgentRead> {
  return apiData<AgentRead>(`/agents/${agentId}`, {
    method: "PATCH",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function disableAgent(agentId: string): Promise<AgentRead> {
  return apiData<AgentRead>(`/agents/${agentId}/disable`, {
    method: "POST",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function archiveAgent(agentId: string): Promise<AgentRead> {
  return apiData<AgentRead>(`/agents/${agentId}/archive`, {
    method: "POST",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function fetchAgentTokens(
  agentId: string,
): Promise<PaginatedData<AgentTokenRead>> {
  return apiData<PaginatedData<AgentTokenRead>>(`/agents/${agentId}/tokens`);
}

export async function createAgentToken(
  agentId: string,
  payload: { name: string; scopes?: string[]; expires_in_days?: number },
): Promise<AgentTokenCreated> {
  return apiData<AgentTokenCreated>(`/agents/${agentId}/tokens`, {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function revokeAgentToken(
  agentId: string,
  tokenId: string,
): Promise<{ token_id: string; revoked_at: string }> {
  return apiData<{ token_id: string; revoked_at: string }>(
    `/agents/${agentId}/tokens/${tokenId}/revoke`,
    {
      method: "POST",
      body: { revoke_reason: "manual_revoke" },
      headers: { "Idempotency-Key": generateRequestId() },
    },
  );
}

// ── Context Pack ──
import type {
  ContextPackSummary,
  ContextPackDetail,
} from "@/types";

export interface ContextPackFilterParams {
  page?: number;
  page_size?: number;
}

export async function fetchContextPacks(
  params: ContextPackFilterParams = {},
): Promise<PaginatedData<ContextPackSummary>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
  };
  return apiData<PaginatedData<ContextPackSummary>>("/context/packs", {
    params: queryParams,
  });
}

export async function fetchContextPack(
  packId: string,
): Promise<ContextPackDetail> {
  return apiData<ContextPackDetail>(`/context/packs/${packId}`);
}

export type {
  AgentRead,
  AgentTokenRead,
  AgentTokenCreated,
  AgentUpdatePayload,
  ContextPackSummary,
  ContextPackDetail,
} from "@/types";

// ── Graph (P7) ──
import type {
  GraphNode,
  GraphEdge,
  GraphData,
  GraphNodeType,
} from "@/types";

export interface GraphFilterParams {
  node_type?: string;
  search?: string;
  project_id?: string;
  limit?: number;
  depth?: number;
}

export async function fetchGraphData(
  params: GraphFilterParams = {},
): Promise<GraphData> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    node_type: params.node_type,
    search: params.search,
    project_id: params.project_id,
    limit: params.limit || 200,
    depth: params.depth || 2,
  };
  return apiData<GraphData>("/graph", { params: queryParams });
}

export async function fetchGraphNode(nodeId: string): Promise<GraphNode> {
  return apiData<GraphNode>(`/graph/nodes/${nodeId}`);
}

export async function fetchGraphNodeNeighbors(
  nodeId: string,
  depth = 1,
): Promise<GraphData> {
  return apiData<GraphData>(`/graph/nodes/${nodeId}/neighbors`, {
    params: { depth },
  });
}

export type { GraphNode, GraphEdge, GraphData, GraphNodeType };

// ── Eval (P7) ──
import type {
  EvalTask,
  EvalTaskDetail,
  EvalResultItem,
  EvalMetricSummary,
  EvalTaskStatus,
} from "@/types";

export interface EvalTaskFilterParams {
  page?: number;
  page_size?: number;
  status?: string;
  task_type?: string;
}

export async function fetchEvalTasks(
  params: EvalTaskFilterParams = {},
): Promise<PaginatedData<EvalTask>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    status: params.status,
    task_type: params.task_type,
  };
  return apiData<PaginatedData<EvalTask>>("/eval/tasks", { params: queryParams });
}

export async function fetchEvalTask(taskId: string): Promise<EvalTaskDetail> {
  return apiData<EvalTaskDetail>(`/eval/tasks/${taskId}`);
}

export async function createEvalTask(payload: {
  task_name: string;
  task_type: string;
  description?: string;
  config?: Record<string, unknown>;
}): Promise<EvalTask> {
  return apiData<EvalTask>("/eval/tasks", {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function runEvalTask(taskId: string): Promise<EvalTask> {
  return apiData<EvalTask>(`/eval/tasks/${taskId}/run`, {
    method: "POST",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function cancelEvalTask(taskId: string): Promise<EvalTask> {
  return apiData<EvalTask>(`/eval/tasks/${taskId}/cancel`, {
    method: "POST",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function fetchEvalTaskResults(
  taskId: string,
  params: { page?: number; page_size?: number } = {},
): Promise<PaginatedData<EvalResultItem>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
  };
  return apiData<PaginatedData<EvalResultItem>>(
    `/eval/tasks/${taskId}/results`,
    { params: queryParams },
  );
}

// ── Global Search ──
import type {
  GlobalSearchResponse,
  GlobalSearchResult,
  GlobalSearchSource,
} from "@/types";

export interface GlobalSearchParams {
  q: string;
  project_id?: string;
  page?: number;
  page_size?: number;
}

export async function searchGlobal(
  params: GlobalSearchParams,
): Promise<GlobalSearchResponse> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    q: params.q,
    project_id: params.project_id,
    page: params.page || 1,
    page_size: params.page_size || 20,
  };
  return apiData<GlobalSearchResponse>("/search/global", {
    params: queryParams,
  });
}

export type { GlobalSearchResponse, GlobalSearchResult, GlobalSearchSource };

export type { EvalTask, EvalTaskDetail, EvalResultItem, EvalMetricSummary, EvalTaskStatus };

// ── Gateway / API 管理 (P2-11) ──
import type {
  GateProviderRead,
  GateProviderListResponse,
  GateProviderCreate,
  GateProviderUpdate,
  GateProviderModelRead,
  GateProviderModelListResponse,
  GateProviderModelCreate,
  GateCapabilityRead,
  GateCapabilityListResponse,
  GateCapabilityCreate,
  GateCapabilityBindingRead,
  GateCapabilityBindingListResponse,
  GateCapabilityBindingCreate,
  GateCapabilityBindingUpdate,
  GateUsageLimitRead,
  GateUsageLimitListResponse,
  GateUsageLimitCreate,
  GateUsageLimitUpdate,
  GateLimitUsageRead,
} from "@/types";

export interface GateProviderFilterParams {
  page?: number;
  page_size?: number;
  provider_type?: string;
  status?: string;
  search?: string;
}

export async function fetchGateProviders(
  params: GateProviderFilterParams = {},
): Promise<GateProviderListResponse> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    provider_type: params.provider_type,
    status: params.status,
    search: params.search,
  };
  return apiData<GateProviderListResponse>("/gateway/providers", { params: queryParams });
}

export async function fetchGateProvider(
  providerId: string,
): Promise<GateProviderRead> {
  return apiData<GateProviderRead>(`/gateway/providers/${providerId}`);
}

export async function createGateProvider(
  payload: {
    provider_code: string;
    name: string;
    provider_type: string;
    endpoint_base?: string | null;
    config_json?: Record<string, unknown> | null;
  },
): Promise<GateProviderRead> {
  return apiData<GateProviderRead>("/gateway/providers", {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function updateGateProvider(
  providerId: string,
  payload: {
    name?: string;
    provider_type?: string;
    status?: string;
    endpoint_base?: string | null;
    config_json?: Record<string, unknown> | null;
  },
): Promise<GateProviderRead> {
  return apiData<GateProviderRead>(`/gateway/providers/${providerId}`, {
    method: "PUT",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export interface GateModelFilterParams {
  page?: number;
  page_size?: number;
  model_type?: string;
  status?: string;
  search?: string;
}

export async function fetchGateProviderModels(
  providerId: string,
  params: GateModelFilterParams = {},
): Promise<GateProviderModelListResponse> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    model_type: params.model_type,
    status: params.status,
    search: params.search,
  };
  return apiData<GateProviderModelListResponse>(
    `/gateway/providers/${providerId}/models`,
    { params: queryParams },
  );
}

export async function fetchGateProviderModel(
  providerId: string,
  modelId: string,
): Promise<GateProviderModelRead> {
  return apiData<GateProviderModelRead>(
    `/gateway/providers/${providerId}/models/${modelId}`,
  );
}

export async function createGateProviderModel(
  providerId: string,
  payload: {
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
  },
): Promise<GateProviderModelRead> {
  return apiData<GateProviderModelRead>(
    `/gateway/providers/${providerId}/models`,
    {
      method: "POST",
      body: payload,
      headers: { "Idempotency-Key": generateRequestId() },
    },
  );
}

export async function updateGateProviderModel(
  providerId: string,
  modelId: string,
  payload: {
    status?: string;
    display_name?: string;
    context_window_tokens?: number;
    max_input_tokens?: number;
    max_output_tokens?: number;
    input_price_per_1k?: number;
    output_price_per_1k?: number;
    supports_streaming?: boolean;
    supports_json_mode?: boolean;
    supports_tools?: boolean;
    supports_vision?: boolean;
    sensitivity_ceiling?: string;
  },
): Promise<GateProviderModelRead> {
  return apiData<GateProviderModelRead>(
    `/gateway/providers/${providerId}/models/${modelId}`,
    {
      method: "PUT",
      body: payload,
      headers: { "Idempotency-Key": generateRequestId() },
    },
  );
}

export interface GateCapFilterParams {
  page?: number;
  page_size?: number;
  category?: string;
  risk_level?: string;
  search?: string;
}

export async function fetchGateCapabilities(
  params: GateCapFilterParams = {},
): Promise<GateCapabilityListResponse> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    category: params.category,
    risk_level: params.risk_level,
    search: params.search,
  };
  return apiData<GateCapabilityListResponse>("/gateway/capabilities", { params: queryParams });
}

export async function createGateCapability(
  payload: {
    capability_code: string;
    name: string;
    category: string;
    risk_level: string;
    default_budget_mode: string;
  },
): Promise<GateCapabilityRead> {
  return apiData<GateCapabilityRead>("/gateway/capabilities", {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function seedGateCapabilities(): Promise<GateCapabilityListResponse> {
  return apiData<GateCapabilityListResponse>("/gateway/seed/capabilities", {
    method: "POST",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export interface GateBindingFilterParams {
  page?: number;
  page_size?: number;
  capability_id?: string;
  provider_id?: string;
  project_id?: string;
  status?: string;
  binding_scope?: string;
}

export async function fetchGateBindings(
  params: GateBindingFilterParams = {},
): Promise<GateCapabilityBindingListResponse> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    capability_id: params.capability_id,
    provider_id: params.provider_id,
    project_id: params.project_id,
    status: params.status,
    binding_scope: params.binding_scope,
  };
  return apiData<GateCapabilityBindingListResponse>("/gateway/bindings", { params: queryParams });
}

export async function fetchGateBinding(
  bindingId: string,
): Promise<GateCapabilityBindingRead> {
  return apiData<GateCapabilityBindingRead>(`/gateway/bindings/${bindingId}`);
}

export async function createGateBinding(
  payload: {
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
  },
): Promise<GateCapabilityBindingRead> {
  return apiData<GateCapabilityBindingRead>("/gateway/bindings", {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function updateGateBinding(
  bindingId: string,
  payload: {
    status?: string;
    priority?: number;
    require_review?: boolean;
    allow_streaming?: boolean;
    timeout_seconds?: number;
    budget_mode?: string;
  },
): Promise<GateCapabilityBindingRead> {
  return apiData<GateCapabilityBindingRead>(`/gateway/bindings/${bindingId}`, {
    method: "PUT",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export interface GateLimitFilterParams {
  page?: number;
  page_size?: number;
  subject_type?: string;
  subject_id?: string;
  capability_id?: string;
  provider_id?: string;
  project_id?: string;
  limit_scope?: string;
  enabled?: boolean;
}

export async function fetchGateLimits(
  params: GateLimitFilterParams = {},
): Promise<GateUsageLimitListResponse> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    subject_type: params.subject_type,
    subject_id: params.subject_id,
    capability_id: params.capability_id,
    provider_id: params.provider_id,
    project_id: params.project_id,
    limit_scope: params.limit_scope,
    enabled: params.enabled,
  };
  return apiData<GateUsageLimitListResponse>("/gateway/limits", { params: queryParams });
}

export async function fetchGateLimit(
  limitId: string,
): Promise<GateUsageLimitRead> {
  return apiData<GateUsageLimitRead>(`/gateway/limits/${limitId}`);
}

export async function createGateLimit(
  payload: {
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
  },
): Promise<GateUsageLimitRead> {
  return apiData<GateUsageLimitRead>("/gateway/limits", {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function updateGateLimit(
  limitId: string,
  payload: {
    max_requests?: number;
    max_input_tokens?: number;
    max_output_tokens?: number;
    max_total_tokens?: number;
    max_cost?: number;
    enabled?: boolean;
    window_unit?: string;
  },
): Promise<GateUsageLimitRead> {
  return apiData<GateUsageLimitRead>(`/gateway/limits/${limitId}`, {
    method: "PUT",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function deleteGateLimit(
  limitId: string,
): Promise<{ deleted: boolean; usage_limit_id: string }> {
  return apiData<{ deleted: boolean; usage_limit_id: string }>(
    `/gateway/limits/${limitId}`,
    { method: "DELETE", headers: { "Idempotency-Key": generateRequestId() } },
  );
}

export async function fetchGateLimitUsage(
  limitId: string,
): Promise<GateLimitUsageRead> {
  return apiData<GateLimitUsageRead>(`/gateway/limits/${limitId}/usage`);
}

// ── Projects ──
import type {
  ProjectRead,
  ProjectCreateRequest as ProjectCreateReq,
  ProjectUpdateRequest as ProjectUpdateReq,
  ProjectListResponse,
} from "@/types";

export interface ProjectFilterParams {
  page?: number;
  page_size?: number;
  status?: string;
}

export async function fetchProjects(
  params: ProjectFilterParams = {},
): Promise<ProjectListResponse> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    status: params.status,
  };
  return apiData<ProjectListResponse>("/projects", { params: queryParams });
}

export async function fetchProject(projectId: string): Promise<ProjectRead> {
  return apiData<ProjectRead>(`/projects/${projectId}`);
}

export async function createProject(
  payload: ProjectCreateReq,
): Promise<ProjectRead> {
  return apiData<ProjectRead>("/projects", {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function updateProject(
  projectId: string,
  payload: ProjectUpdateReq,
): Promise<ProjectRead> {
  return apiData<ProjectRead>(`/projects/${projectId}`, {
    method: "PUT",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function deleteProject(
  projectId: string,
): Promise<ProjectRead> {
  return apiData<ProjectRead>(`/projects/${projectId}`, {
    method: "DELETE",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export type { ProjectRead, ProjectListResponse };

// ── Pipeline Defs (CRUD against /api/v4/pipelines/defs) ──
import type {
  PipelineDefRead,
  PipelineDefStatus,
  PipelineType,
} from "@/types";

export interface PipelineDefFilterParams {
  page?: number;
  page_size?: number;
  pipeline_type?: PipelineType;
  status?: PipelineDefStatus;
  project_id?: string;
}

export async function fetchPipelineDefs(
  params: PipelineDefFilterParams = {},
): Promise<PaginatedData<PipelineDefRead>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    pipeline_type: params.pipeline_type,
    status: params.status,
    project_id: params.project_id,
  };
  return apiData<PaginatedData<PipelineDefRead>>("/pipelines/defs", {
    params: queryParams,
  });
}

export async function fetchPipelineDef(
  pipelineDefId: string,
): Promise<PipelineDefRead> {
  return apiData<PipelineDefRead>(`/pipelines/defs/${pipelineDefId}`);
}

export interface PipelineDefCreatePayload {
  pipeline_code: string;
  pipeline_type: PipelineType;
  name: string;
  description?: string;
  config_json?: Record<string, unknown>;
  project_id?: string;
  status?: PipelineDefStatus;
}

export async function createPipelineDef(
  payload: PipelineDefCreatePayload,
): Promise<PipelineDefRead> {
  return apiData<PipelineDefRead>("/pipelines/defs", {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function updatePipelineDef(
  pipelineDefId: string,
  payload: { status?: PipelineDefStatus; name?: string; description?: string; config_json?: Record<string, unknown> },
): Promise<PipelineDefRead> {
  const body: Record<string, unknown> = {};
  if (payload.status !== undefined) body.status = payload.status;
  if (payload.name !== undefined) body.name = payload.name;
  if (payload.description !== undefined) body.description = payload.description;
  if (payload.config_json !== undefined) body.config_json = payload.config_json;
  return apiData<PipelineDefRead>(`/pipelines/defs/${pipelineDefId}`, {
    method: "PATCH",
    body,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

// ── Sub-Libraries (from /api/v4/sub-libraries) ──
import type { SubLibraryRead, SubLibraryListResponse } from "@/types";

export interface SubLibraryCreatePayload {
  name: string;
  type: string;
  key?: string;
  capability_json?: Record<string, unknown>;
  metadata_json?: Record<string, unknown>;
}

export interface SubLibraryUpdatePayload {
  name?: string;
  type?: string;
  key?: string;
  capability_json?: Record<string, unknown>;
  metadata_json?: Record<string, unknown>;
}

export async function fetchSubLibraries(
  params: { type?: string; page?: number; page_size?: number } = {},
): Promise<SubLibraryListResponse> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    type: params.type,
    page: params.page || 1,
    page_size: params.page_size || 50,
  };
  return apiData<SubLibraryListResponse>("/sub-libraries", { params: queryParams });
}

export async function fetchSubLibrary(libId: string): Promise<SubLibraryRead> {
  return apiData<SubLibraryRead>(`/sub-libraries/${libId}`);
}

export async function createSubLibrary(
  payload: SubLibraryCreatePayload,
): Promise<SubLibraryRead> {
  return apiData<SubLibraryRead>("/sub-libraries", {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function updateSubLibrary(
  libId: string,
  payload: SubLibraryUpdatePayload,
): Promise<SubLibraryRead> {
  return apiData<SubLibraryRead>(`/sub-libraries/${libId}`, {
    method: "PUT",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function deleteSubLibrary(
  libId: string,
): Promise<{ deleted: boolean; id: string }> {
  return apiData<{ deleted: boolean; id: string }>(`/sub-libraries/${libId}`, {
    method: "DELETE",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export type { SubLibraryRead, SubLibraryListResponse };

// ── Import Jobs (from /api/v4/import — replaces old knowledge_ingest bridge) ──
import type {
  ProcessingJobRead,
  ProcessingJobStatus,
} from "@/types";

export interface ImportJobFormPayload {
  file: File;
  pipeline_key: string;
  target_stores: string[];
  project_id: string;
  title?: string;
  asset_type?: string;
  sensitivity_level?: string;
  retention_policy?: string;
  onProgress?: (percent: number) => void;
}

/**
 * Upload a file and create a processing job in one call.
 * Replaces the old two-step uploadAsset + submitIngestProcess flow.
 */
export async function submitImportJob(
  payload: ImportJobFormPayload,
): Promise<ProcessingJobRead> {
  const formData = new FormData();
  formData.append("file", payload.file);
  formData.append("pipeline_key", payload.pipeline_key);
  formData.append("target_stores", payload.target_stores.join(","));
  formData.append("project_id", payload.project_id);
  if (payload.title) formData.append("title", payload.title);
  if (payload.asset_type) formData.append("asset_type", payload.asset_type);
  if (payload.sensitivity_level) formData.append("sensitivity_level", payload.sensitivity_level);
  if (payload.retention_policy) formData.append("retention_policy", payload.retention_policy);

  const requestId = generateRequestId();

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE_URL}/import`);
    xhr.setRequestHeader("X-Request-Id", requestId);
    xhr.setRequestHeader("Idempotency-Key", generateRequestId());
    xhr.withCredentials = true;

    if (payload.onProgress) {
      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) {
          payload.onProgress!(Math.round((e.loaded / e.total) * 100));
        }
      });
    }

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const envelope = JSON.parse(xhr.responseText);
          resolve(envelope.data as ProcessingJobRead);
        } catch {
          reject(new Error("解析响应失败"));
        }
      } else {
        let message = `导入失败，状态码 ${xhr.status}`;
        try {
          const errorData = JSON.parse(xhr.responseText);
          if (errorData?.error?.message) {
            message = errorData.error.message;
          }
        } catch { /* ignore */ }
        reject(new Error(message));
      }
    });

    xhr.addEventListener("error", () => {
      reject(new Error("网络连接失败"));
    });

    xhr.addEventListener("abort", () => {
      reject(new Error("导入已取消"));
    });

    xhr.send(formData);
  });
}

export async function fetchImportJobStatus(
  jobId: string,
): Promise<ProcessingJobStatus> {
  return apiData<ProcessingJobStatus>(`/import/${jobId}/status`);
}

export async function fetchImportJobs(
  params: { asset_id?: string; page?: number; page_size?: number } = {},
): Promise<ProcessingJobRead[]> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
  };
  if (params.asset_id) {
    queryParams.asset_id = params.asset_id;
  }
  return apiData<ProcessingJobRead[]>("/import", { params: queryParams });
}

export type { ProcessingJobRead, ProcessingJobStatus };

// ── Memory Stores (P9) ──
import type { MemoryStoreRead, MemoryStoreType as _MemoryStoreType } from "@/types";

export interface MemoryStoreCreatePayload {
  name: string;
  type: _MemoryStoreType;
  agent_id?: string | null;
  description?: string | null;
}

export interface MemoryStoreUpdatePayload {
  name?: string;
  type?: _MemoryStoreType;
  agent_id?: string | null;
  description?: string | null;
}

export async function fetchMemoryStores(
  params: { agent_id?: string; unbound_only?: boolean } = {},
): Promise<MemoryStoreRead[]> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    agent_id: params.agent_id,
    unbound_only: params.unbound_only,
  };
  return apiData<MemoryStoreRead[]>("/memory-stores", { params: queryParams });
}

export async function fetchMemoryStore(storeId: string): Promise<MemoryStoreRead> {
  return apiData<MemoryStoreRead>(`/memory-stores/${storeId}`);
}

export async function fetchMemoryStoreByAgent(agentId: string): Promise<MemoryStoreRead | null> {
  return apiData<MemoryStoreRead | null>(`/memory-stores/by-agent/${agentId}`);
}

export async function createMemoryStore(payload: MemoryStoreCreatePayload): Promise<MemoryStoreRead> {
  return apiData<MemoryStoreRead>("/memory-stores", {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function updateMemoryStore(
  storeId: string,
  payload: MemoryStoreUpdatePayload,
): Promise<MemoryStoreRead> {
  return apiData<MemoryStoreRead>(`/memory-stores/${storeId}`, {
    method: "PATCH",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function deleteMemoryStore(storeId: string): Promise<{ deleted: boolean; store_id: string }> {
  return apiData<{ deleted: boolean; store_id: string }>(`/memory-stores/${storeId}`, {
    method: "DELETE",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function bindStoreToAgent(storeId: string, agentId: string): Promise<MemoryStoreRead> {
  return apiData<MemoryStoreRead>(`/memory-stores/${storeId}/bind/${agentId}`, {
    method: "POST",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function unbindStore(storeId: string): Promise<MemoryStoreRead> {
  return apiData<MemoryStoreRead>(`/memory-stores/${storeId}/unbind`, {
    method: "POST",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export type { MemoryStoreRead };

// ── Agent Cards (P8-02) ──
import type {
  AgentCardRead,
  AgentCardCreateRequest,
  AgentCardUpdateRequest,
  AgentToolItemRead,
  AgentToolItemCreateRequest,
  AgentToolItemUpdateRequest,
} from "@/types";

export interface AgentCardFilterParams {
  page?: number;
  page_size?: number;
  card_type?: string;
}

export async function fetchAgentCards(
  params: AgentCardFilterParams = {},
): Promise<PaginatedData<AgentCardRead>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    card_type: params.card_type,
  };
  return apiData<PaginatedData<AgentCardRead>>("/agent-cards", { params: queryParams });
}

export async function fetchAgentCard(cardId: string): Promise<AgentCardRead> {
  return apiData<AgentCardRead>(`/agent-cards/${cardId}`);
}

export async function createAgentCard(
  payload: AgentCardCreateRequest,
): Promise<AgentCardRead> {
  return apiData<AgentCardRead>("/agent-cards", {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function updateAgentCard(
  cardId: string,
  payload: AgentCardUpdateRequest,
): Promise<AgentCardRead> {
  return apiData<AgentCardRead>(`/agent-cards/${cardId}`, {
    method: "PATCH",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function deleteAgentCard(
  cardId: string,
): Promise<{ deleted: boolean; card_id: string }> {
  return apiData<{ deleted: boolean; card_id: string }>(`/agent-cards/${cardId}`, {
    method: "DELETE",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

// ── Agent Tool Items ──
export async function fetchAgentToolItems(
  cardId: string,
): Promise<AgentToolItemRead[]> {
  return apiData<AgentToolItemRead[]>(`/agent-cards/${cardId}/tools`);
}

export async function createAgentToolItem(
  cardId: string,
  payload: AgentToolItemCreateRequest,
): Promise<AgentToolItemRead> {
  return apiData<AgentToolItemRead>(`/agent-cards/${cardId}/tools`, {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function updateAgentToolItem(
  cardId: string,
  itemId: string,
  payload: AgentToolItemUpdateRequest,
): Promise<AgentToolItemRead> {
  return apiData<AgentToolItemRead>(`/agent-cards/${cardId}/tools/${itemId}`, {
    method: "PATCH",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export async function deleteAgentToolItem(
  cardId: string,
  itemId: string,
): Promise<{ deleted: boolean; item_id: string }> {
  return apiData<{ deleted: boolean; item_id: string }>(`/agent-cards/${cardId}/tools/${itemId}`, {
    method: "DELETE",
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export type {
  AgentCardRead,
  AgentCardCreateRequest,
  AgentCardUpdateRequest,
  AgentToolItemRead,
  AgentToolItemCreateRequest,
  AgentToolItemUpdateRequest,
};

// ── Admin Logs ──
import type { AdminLogEntry } from "@/types";

export interface AdminLogFilterParams {
  page?: number;
  page_size?: number;
  level?: string;
  source?: string;
  call_type?: string;
  since?: string;
  until?: string;
}

export async function fetchAdminLogs(
  params: AdminLogFilterParams = {},
): Promise<PaginatedData<AdminLogEntry>> {
  const queryParams: Record<string, string | number | boolean | undefined> = {
    page: params.page || 1,
    page_size: params.page_size || 50,
    level: params.level,
    source: params.source,
    call_type: params.call_type,
    since: params.since,
    until: params.until,
  };
  return apiData<PaginatedData<AdminLogEntry>>("/admin/logs", { params: queryParams });
}

export type { AdminLogEntry };

// ── Context Assembly (P8-01) ──
import type { AssembleRequest, AssembleResponse } from "@/types";

export async function assembleContext(
  payload: AssembleRequest,
): Promise<AssembleResponse> {
  return apiData<AssembleResponse>("/context/assemble", {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export type { AssembleRequest, AssembleResponse };

// ── Create Agent ──
import type { AgentCreateRequest as AgentCreateReq } from "@/types";

export async function createAgent(
  payload: AgentCreateReq,
): Promise<AgentRead> {
  return apiData<AgentRead>("/agents", {
    method: "POST",
    body: payload,
    headers: { "Idempotency-Key": generateRequestId() },
  });
}

export type { AgentCreateReq as AgentCreateRequest };

// ── Backward compat stubs for old ingest pipeline UI ──
import type { IngestPipeline, IngestStore, IngestProcessItem } from "@/types";

/** Map pipeline code to an icon emoji */
function _pipelineIcon(code: string): string {
  if (code.includes("code") || code.includes("parse")) return "💻";
  if (code.includes("ocr") || code.includes("image") || code.includes("vision")) return "🖼️";
  if (code.includes("dialog") || code.includes("chat") || code.includes("msg")) return "💬";
  if (code.includes("chunk") || code.includes("split") || code.includes("standard")) return "🧩";
  if (code.includes("index") || code.includes("embed")) return "📊";
  if (code.includes("classify") || code.includes("tag")) return "🏷️";
  if (code.includes("summarize") || code.includes("summary")) return "📝";
  if (code.includes("translate")) return "🌐";
  if (code.includes("extract") || code.includes("ner")) return "🔍";
  return "⚙️";
}

export async function fetchIngestPipelines(): Promise<IngestPipeline[]> {
  const defs = await fetchPipelineDefs({ pipeline_type: "asset_import" as any, page_size: 100 });
  return defs.items.map((d) => ({
    pipeline_key: d.pipeline_code,
    name: d.name,
    icon: _pipelineIcon(d.pipeline_code),
    description: d.description ?? "",
    supported_formats: (d.config_json?.supported_formats as string[]) ?? ["*"],
  }));
}

export async function fetchIngestStores(): Promise<IngestStore[]> {
  const response = await fetchSubLibraries();
  const stores = response.items;
  return stores.map((s) => ({
    store_key: s.key || s.type,
    name: s.name,
    type: s.type,
    icon: s.type === "vector" ? "🧠" : s.type === "fulltext" ? "🔍" : "📦",
    description: `type: ${s.type}`,
  }));
}

/** Normalize backend status to frontend-compatible status */
function _normalizeStatus(status: string): string {
  // Backend uses "done" as terminal success, frontend expects "completed"
  if (status === "done") return "completed";
  return status;
}

export async function submitIngestProcess(payload: {
  asset_ids: string[];
  pipeline_key: string;
  store_keys: string[];
  project_id: string;
}): Promise<{ run_id: string; items: IngestProcessItem[] }> {
  // Call the dedicated by-asset endpoint — no placeholder files needed
  const jobs = await apiData<ProcessingJobRead[]>("/import/by-asset", {
    method: "POST",
    body: {
      asset_ids: payload.asset_ids,
      pipeline_key: payload.pipeline_key,
      target_stores: payload.store_keys,
      project_id: payload.project_id,
    },
    headers: { "Idempotency-Key": generateRequestId() },
  });

  const items: IngestProcessItem[] = (jobs ?? []).map((job) => ({
    asset_id: job.asset_id,
    status: _normalizeStatus(job.status),
    chunks_produced: job.chunks_produced,
    error: job.error,
    started_at: job.started_at,
    completed_at: job.completed_at,
  }));

  // Include any asset_ids that were skipped (asset not found) as "unknown"
  const foundIds = new Set(items.map((i) => i.asset_id));
  for (const assetId of payload.asset_ids) {
    if (!foundIds.has(assetId)) {
      items.push({
        asset_id: assetId,
        status: "unknown",
        chunks_produced: 0,
        error: "asset not found on server",
      });
    }
  }

  return { run_id: generateRequestId(), items };
}

export async function fetchIngestStatus(assetIds: string[]): Promise<IngestProcessItem[]> {
  // Batch fetch: use the list endpoint which supports asset_id filter
  // For bulk polling, we fetch all available jobs at once to minimize API calls
  const seen = new Set<string>();
  const items: IngestProcessItem[] = [];

  for (const assetId of assetIds) {
    if (seen.has(assetId)) continue;
    seen.add(assetId);
    try {
      const jobs = await fetchImportJobs({ asset_id: assetId, page_size: 5 });
      if (jobs && jobs.length > 0) {
        const job = jobs[0];
        items.push({
          asset_id: assetId,
          status: _normalizeStatus(job.status),
          chunks_produced: job.chunks_produced,
          error: job.error,
          started_at: job.started_at,
          completed_at: job.completed_at,
        });
      } else {
        items.push({
          asset_id: assetId,
          status: "unknown",
          chunks_produced: 0,
          error: null,
        });
      }
    } catch {
      items.push({
        asset_id: assetId,
        status: "unknown",
        chunks_produced: 0,
        error: "failed to fetch status",
      });
    }
  }
  return items;
}

// ═══════════════════════════════════════════════════════════════════
// Phase 1+2 — Knowledge v2 API
// ═══════════════════════════════════════════════════════════════════

export interface ProjectBackend {
  id: string
  backend_type: string
  enabled: boolean
  config_json: Record<string, unknown>
  created_at: string | null
}

export interface TreeNode {
  name: string
  type: 'folder' | 'file'
  path: string
  document_id?: string
  lang?: string
  version?: number
  children?: TreeNode[]
}

export interface TreeResponse {
  project_id: string
  tree: TreeNode[]
}

export interface DocContent {
  document_id: string
  title: string
  lang: string
  folder_path: string | null
  content_markdown: string
  content_hash: string | null
  current_version: number
  project_id: string
  source_asset_id: string | null
  pipeline_def_id: string | null
}

export interface IndexStateItem {
  backend_type: string
  state: string
  indexed_version: number
  target_version: number
  last_error: string | null
  error_count: number
  built_at: string | null
}

export interface ProjectHealth {
  project_id: string
  backends: Array<{
    backend_type: string
    enabled: boolean
    docs_ready: number
    docs_stale: number
    docs_failed: number
    docs_disabled: number
  }>
  overall: 'healthy' | 'degraded' | 'attention'
}

export function fetchProjectBackends(projectId: string): Promise<ProjectBackend[]> {
  return apiData<ProjectBackend[]>(`/projects/${projectId}/backends`)
}

export function toggleBackend(projectId: string, backendType: string, enabled: boolean) {
  return apiData(`/projects/${projectId}/backends/${backendType}`, {
    method: 'PUT',
    body: { enabled },
  })
}

export function fetchProjectTree(projectId: string): Promise<TreeResponse> {
  return apiData<TreeResponse>(`/projects/${projectId}/tree`)
}

export function fetchDocContentV2(documentId: string): Promise<DocContent> {
  return apiData<DocContent>(`/knowledge/documents/${documentId}/v2/content`)
}

export function updateDocContentV2(documentId: string, content_markdown: string) {
  return apiData(`/knowledge/documents/${documentId}/v2/content`, {
    method: 'PATCH',
    body: { content_markdown },
  })
}

export function fetchDocIndexStates(documentId: string): Promise<{ backends: IndexStateItem[] }> {
  return apiData(`/knowledge/documents/${documentId}/index-states`)
}

export function fetchProjectHealth(projectId: string): Promise<ProjectHealth> {
  return apiData<ProjectHealth>(`/projects/${projectId}/health`)
}

export function createDocument(projectId: string, title: string, content?: string, folderPath?: string) {
  return apiData('/knowledge/documents/v2', {
    method: 'POST',
    body: { project_id: projectId, title, content_markdown: content, folder_path: folderPath, lang: 'markdown' },
  })
}

export function moveDocument(documentId: string, targetProjectId: string, targetFolder?: string) {
  return apiData(`/knowledge/documents/${documentId}/move`, {
    method: 'POST',
    body: { target_project_id: targetProjectId, target_folder: targetFolder },
  })
}

export function fetchImportExclusions(projectId: string) {
  return apiData(`/projects/${projectId}/import-exclusions`)
}

export function fetchOriginalsStats() {
  return apiData('/admin/originals/stats')
}

export function searchGlobalV2(query: string, mode: string = 'intelligent') {
  return apiData('/knowledge/search/global', {
    params: { q: query, mode, page: 1, page_size: 20 },
  })
}
