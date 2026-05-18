<script setup lang="ts">
/**
 * PipelineTab — 管道定义管理 (对齐 /api/v4/pipelines/defs)
 *
 * 功能:
 *   - 管道定义列表 (从 API 读取)
 *   - 新建管道定义 (POST /pipelines/defs)
 *   - 编辑管道 (PATCH /pipelines/defs/:id)
 *   - 归档/禁用 管道
 */
import { ref, computed } from "vue";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import Pagination from "@/components/Pagination.vue";
import {
  fetchPipelineDefs,
  createPipelineDef,
  updatePipelineDef,
} from "@/api/client";
import type { PipelineDefRead, PipelineType, PipelineDefStatus } from "@/types";
import {
  PIPELINE_TYPE_LABELS,
  PIPELINE_STATUS_LABELS,
} from "@/types";

const queryClient = useQueryClient();

// ── List state ──
const page = ref(1);
const pageSize = ref(50);
const filterPipelineType = ref("");
const filterStatus = ref("");

const listKey = computed(() => [
  "pipeline-defs",
  page.value,
  pageSize.value,
  filterPipelineType.value,
  filterStatus.value,
] as const);

const {
  data: pipelineData,
  isLoading,
  isError,
  error,
} = useQuery({
  queryKey: listKey,
  queryFn: () =>
    fetchPipelineDefs({
      page: page.value,
      page_size: pageSize.value,
      pipeline_type: (filterPipelineType.value || undefined) as PipelineType | undefined,
      status: (filterStatus.value || undefined) as PipelineDefStatus | undefined,
    }),
  placeholderData: (prev) => prev,
});

const pipelines = computed(() => pipelineData.value?.items ?? []);
const totalPipelines = computed(() => pipelineData.value?.page_info?.total_items ?? 0);

// ── Create / Edit form ──
const showForm = ref(false);
const editingId = ref<string | null>(null);
const formSubmitting = ref(false);

interface PipelineForm {
  pipeline_code: string;
  name: string;
  description: string;
  pipeline_type: PipelineType;
  config_json_str: string;
}

const emptyForm = (): PipelineForm => ({
  pipeline_code: "",
  name: "",
  description: "",
  pipeline_type: "asset_import",
  config_json_str: "{}",
});

const form = ref<PipelineForm>(emptyForm());
const configError = ref("");

// ── Delete (archive) confirmation ──
const confirmArchiveId = ref<string | null>(null);
const archiving = ref(false);

// ── Pipeline types ──
const TYPE_OPTIONS: { value: PipelineType; label: string; description: string }[] = [
  { value: "asset_import", label: "资产导入", description: "上传资产 → 提取元数据 → 触发知识索引" },
  { value: "knowledge_index", label: "知识索引", description: "文档分块 → 向量化 → 全文索引" },
  { value: "memory_extract", label: "记忆提取", description: "对话/文档 → 候选记忆提取" },
  { value: "backup", label: "备份", description: "数据库快照备份流程" },
  { value: "restore", label: "恢复", description: "备份恢复流程" },
  { value: "importer", label: "导入器", description: "外部数据批量导入流程" },
  { value: "maintenance", label: "维护", description: "定期清理/优化等维护流程" },
];

// ── Helpers ──
function formatTime(iso?: string): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function getTypeColor(type: string): string {
  const colors: Record<string, string> = {
    asset_import: "bg-violet-50 text-violet-700 border-violet-200",
    knowledge_index: "bg-blue-50 text-blue-700 border-blue-200",
    memory_extract: "bg-emerald-50 text-emerald-700 border-emerald-200",
    backup: "bg-amber-50 text-amber-700 border-amber-200",
    restore: "bg-orange-50 text-orange-700 border-orange-200",
    importer: "bg-cyan-50 text-cyan-700 border-cyan-200",
    maintenance: "bg-slate-50 text-slate-700 border-slate-200",
  };
  return colors[type] || "bg-surface-100 text-surface-600 border-surface-200";
}

function getStatusColor(status: string): string {
  const colors: Record<string, string> = {
    draft: "bg-surface-100 text-surface-600",
    active: "bg-emerald-50 text-emerald-700",
    disabled: "bg-amber-50 text-amber-700",
    archived: "bg-red-50 text-red-700",
  };
  return colors[status] || "bg-surface-100 text-surface-600";
}

function openCreate() {
  editingId.value = null;
  form.value = emptyForm();
  configError.value = "";
  showForm.value = true;
}

function openEdit(pipeline: PipelineDefRead) {
  editingId.value = pipeline.pipeline_def_id;
  form.value = {
    pipeline_code: pipeline.pipeline_code,
    name: pipeline.name,
    description: pipeline.description || "",
    pipeline_type: pipeline.pipeline_type as PipelineType,
    config_json_str: JSON.stringify(pipeline.config_json ?? {}, null, 2),
  };
  configError.value = "";
  showForm.value = true;
}

function closeForm() {
  showForm.value = false;
  editingId.value = null;
  form.value = emptyForm();
  configError.value = "";
}

function validateConfig(): Record<string, unknown> | undefined {
  const raw = form.value.config_json_str.trim();
  if (!raw || raw === "{}") return {};
  try {
    const parsed = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      configError.value = "配置必须是 JSON 对象";
      return undefined;
    }
    configError.value = "";
    return parsed as Record<string, unknown>;
  } catch {
    configError.value = "JSON 格式无效";
    return undefined;
  }
}

// Shared error state
const actionError = ref<string | null>(null);

function showError(msg: string) {
  actionError.value = msg;
  setTimeout(() => { actionError.value = null; }, 6000);
}

async function handleSubmit() {
  if (!form.value.pipeline_code.trim() || !form.value.name.trim()) return;

  actionError.value = null;
  if (!editingId.value) {
    const config = validateConfig();
    if (configError.value) return;
    formSubmitting.value = true;
    try {
      await createPipelineDef({
        pipeline_code: form.value.pipeline_code.trim(),
        pipeline_type: form.value.pipeline_type,
        name: form.value.name.trim(),
        description: form.value.description.trim() || undefined,
        config_json: config && Object.keys(config).length > 0 ? config : undefined,
        status: "active",
      });
      closeForm();
      queryClient.invalidateQueries({ queryKey: ["pipeline-defs"] });
    } catch (e) {
      showError((e as Error)?.message || "创建失败");
    } finally {
      formSubmitting.value = false;
    }
  } else {
    const config = validateConfig();
    if (configError.value) return;
    formSubmitting.value = true;
    try {
      await updatePipelineDef(editingId.value, {
        name: form.value.name.trim(),
        description: form.value.description.trim() || undefined,
        config_json: config && Object.keys(config).length > 0 ? config : undefined,
      });
      closeForm();
      queryClient.invalidateQueries({ queryKey: ["pipeline-defs"] });
    } catch (e) {
      showError((e as Error)?.message || "更新失败");
    } finally {
      formSubmitting.value = false;
    }
  }
}

async function handleArchive() {
  if (!confirmArchiveId.value) return;
  actionError.value = null;
  archiving.value = true;
  try {
    await updatePipelineDef(confirmArchiveId.value, { status: "archived" });
    confirmArchiveId.value = null;
    queryClient.invalidateQueries({ queryKey: ["pipeline-defs"] });
  } catch (e) {
    showError((e as Error)?.message || "归档失败");
  } finally {
    archiving.value = false;
  }
}

async function handleDisable(pipeId: string) {
  if (!confirm("确认禁用此管道？禁用后关联的资产导入将停止工作。")) return;
  actionError.value = null;
  archiving.value = true;
  try {
    await updatePipelineDef(pipeId, { status: "disabled" });
    queryClient.invalidateQueries({ queryKey: ["pipeline-defs"] });
  } catch (e) {
    showError((e as Error)?.message || "禁用失败");
  } finally {
    archiving.value = false;
  }
}

async function handleActivate(pipeId: string) {
  actionError.value = null;
  archiving.value = true;
  try {
    await updatePipelineDef(pipeId, { status: "active" });
    queryClient.invalidateQueries({ queryKey: ["pipeline-defs"] });
  } catch (e) {
    showError((e as Error)?.message || "启用失败");
  } finally {
    archiving.value = false;
  }
}

</script>

<template>
  <div>
    <!-- Filters & Toolbar -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-center gap-3">
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-medium text-surface-400">类型</label>
          <select
            v-model="filterPipelineType"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部类型</option>
            <option v-for="opt in TYPE_OPTIONS" :key="opt.value" :value="opt.value">
              {{ opt.label }}
            </option>
          </select>
        </div>
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-medium text-surface-400">状态</label>
          <select
            v-model="filterStatus"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部状态</option>
            <option value="draft">草稿</option>
            <option value="active">活跃</option>
            <option value="disabled">已禁用</option>
            <option value="archived">已归档</option>
          </select>
        </div>
        <span class="self-end text-xs text-surface-400 ml-auto">
          {{ totalPipelines }} 条管道
        </span>
        <button class="btn btn-primary btn-sm self-end" @click="openCreate">
          + 新建管道
        </button>
      </div>
    </div>

    <!-- Error -->
    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载管道失败: {{ (error as Error)?.message }}
    </div>

    <!-- Loading -->
    <div v-if="isLoading" class="py-8">
      <LoadingSkeleton variant="detail" />
    </div>

    <!-- Pipeline List -->
    <div v-else-if="pipelines.length > 0" class="space-y-3">
      <div
        v-for="pipe in pipelines"
        :key="pipe.pipeline_def_id"
        class="card p-4 hover:shadow-sm transition-shadow"
      >
        <div class="flex items-start gap-4">
          <!-- Type badge -->
          <span
            :class="[
              'shrink-0 inline-flex items-center rounded-full px-2.5 py-0.5 text-2xs font-medium border',
              getTypeColor(pipe.pipeline_type),
            ]"
          >
            {{ PIPELINE_TYPE_LABELS[pipe.pipeline_type] || pipe.pipeline_type }}
          </span>

          <!-- Content -->
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2 flex-wrap">
              <p class="text-sm font-medium text-surface-800">{{ pipe.name }}</p>
              <span class="text-2xs font-mono text-surface-400 bg-surface-100 rounded px-1.5 py-0.5">
                {{ pipe.pipeline_code }}
              </span>
              <span
                :class="[
                  'shrink-0 inline-flex items-center rounded-full px-2 py-0.5 text-2xs font-medium',
                  getStatusColor(pipe.status),
                ]"
              >
                {{ PIPELINE_STATUS_LABELS[pipe.status] || pipe.status }}
              </span>
            </div>
            <p v-if="pipe.description" class="text-xs text-surface-500 mt-1 line-clamp-2">
              {{ pipe.description }}
            </p>

            <!-- Meta -->
            <div class="flex flex-wrap items-center gap-2 mt-2 text-2xs text-surface-400">
              <span>版本 v{{ pipe.version }}</span>
              <span v-if="pipe.config_json && Object.keys(pipe.config_json).length > 0">
                · {{ Object.keys(pipe.config_json).length }} 个配置项
              </span>
            </div>

            <!-- Timestamps -->
            <div class="flex items-center gap-2 mt-1 text-2xs text-surface-400">
              <span>创建: {{ formatTime(pipe.created_at) }}</span>
              <span v-if="pipe.updated_at">· 更新: {{ formatTime(pipe.updated_at) }}</span>
            </div>
          </div>

          <!-- Actions -->
          <div class="shrink-0 flex items-center gap-1 flex-wrap justify-end">
            <button class="btn btn-ghost btn-xs text-xs" @click="openEdit(pipe)">
              编辑
            </button>
            <button
              v-if="pipe.status === 'active'"
              class="btn btn-ghost btn-xs text-xs text-amber-600"
              data-testid="pipeline-disable"
              @click="handleDisable(pipe.pipeline_def_id)"
            >
              禁用
            </button>
            <button
              v-if="pipe.status === 'disabled'"
              class="btn btn-ghost btn-xs text-xs text-emerald-600"
              data-testid="pipeline-enable"
              @click="handleActivate(pipe.pipeline_def_id)"
            >
              启用
            </button>
            <button
              v-if="pipe.status !== 'archived'"
              class="btn btn-ghost btn-xs text-xs text-red-500"
              data-testid="pipeline-archive"
              @click="confirmArchiveId = pipe.pipeline_def_id"
            >
              归档
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Empty -->
    <div
      v-else-if="!isLoading"
      class="flex flex-col items-center justify-center py-16 text-sm text-surface-400"
    >
      <span class="text-5xl mb-3">⚙️</span>
      <span>暂无管道定义</span>
      <span class="text-2xs text-surface-300 mt-1">点击上方"新建管道"创建第一条管道</span>
    </div>

    <Pagination
      v-if="pipelineData?.page_info && totalPipelines > 0"
      :page-info="pipelineData.page_info"
      @page-change="(p: number) => (page = p)"
    />

    <!-- Create / Edit Modal -->
    <Teleport to="body">
      <Transition name="modal">
        <div
          v-if="showForm"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4"
          @click.self="closeForm"
        >
          <div class="bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto" @click.stop>
            <div class="px-6 py-4 border-b border-surface-200 flex items-center justify-between">
              <h3 class="text-lg font-semibold text-surface-800">
                {{ editingId ? "编辑管道" : "新建管道" }}
              </h3>
              <button class="rounded-lg p-1.5 text-surface-400 hover:bg-surface-100 hover:text-surface-600" @click="closeForm">
                <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
              </button>
            </div>

            <div class="px-6 py-4 space-y-4">
              <!-- Pipeline code (create only) -->
              <div v-if="!editingId" class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">
                  管道编码 <span class="text-red-400">*</span>
                </label>
                <input
                  v-model="form.pipeline_code"
                  type="text"
                  placeholder="例如: asset_import_v1"
                  class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm font-mono text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                />
              </div>

              <!-- Name -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">
                  名称 <span class="text-red-400">*</span>
                </label>
                <input
                  v-model="form.name"
                  type="text"
                  placeholder="例如: 标准资产导入"
                  class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                />
              </div>

              <!-- Description -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">描述</label>
                <textarea
                  v-model="form.description"
                  rows="2"
                  placeholder="管道用途说明..."
                  class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                />
              </div>

              <!-- Pipeline Type -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">
                  类型 <span v-if="!editingId" class="text-red-400">*</span>
                </label>
                <div v-if="!editingId" class="space-y-2">
                  <label
                    v-for="opt in TYPE_OPTIONS"
                    :key="opt.value"
                    :class="[
                      'flex items-start gap-3 rounded-xl border-2 p-3 cursor-pointer transition-all',
                      form.pipeline_type === opt.value
                        ? 'border-brand-400 bg-brand-50/50'
                        : 'border-surface-200 hover:border-surface-300 hover:bg-surface-50',
                    ]"
                  >
                    <input
                      type="radio"
                      :value="opt.value"
                      v-model="form.pipeline_type"
                      class="mt-0.5 shrink-0 h-4 w-4 text-brand-600 focus:ring-brand-500"
                    />
                    <div class="min-w-0 flex-1">
                      <span class="text-sm font-medium text-surface-700">{{ opt.label }}</span>
                      <p class="text-xs text-surface-400 mt-0.5">{{ opt.description }}</p>
                    </div>
                  </label>
                </div>
                <div v-else class="rounded-lg border border-surface-200 bg-surface-50 px-3 py-2 text-sm text-surface-600">
                  <span class="font-medium">{{ PIPELINE_TYPE_LABELS[form.pipeline_type] || form.pipeline_type }}</span>
                  <span class="text-2xs text-surface-400 ml-2">(创建后不可更改)</span>
                </div>
              </div>

              <!-- Config JSON -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">
                  管道配置 <span class="text-surface-300">(JSON)</span>
                </label>
                <textarea
                  v-model="form.config_json_str"
                  rows="6"
                  placeholder='{"steps": [...]}'
                  class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-xs font-mono text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                  :class="configError ? 'border-red-300 bg-red-50' : ''"
                />
                <p v-if="configError" class="text-xs text-red-500 mt-0.5">{{ configError }}</p>
                <p v-else class="text-2xs text-surface-400 mt-0.5">可选的管道步骤配置</p>
              </div>
            </div>

            <!-- Error banner -->
            <div
              v-if="actionError"
              class="px-6 py-2 bg-red-50 border-t border-red-200 text-xs text-red-700"
            >
              {{ actionError }}
            </div>

            <div class="px-6 py-4 border-t border-surface-200 bg-surface-50 rounded-b-2xl flex items-center justify-end gap-2">
              <button class="btn btn-secondary btn-sm" @click="closeForm">取消</button>
              <button
                class="btn btn-primary btn-sm"
                :disabled="formSubmitting || !form.name.trim() || (!editingId && !form.pipeline_code.trim())"
                @click="handleSubmit"
              >
                {{ formSubmitting ? "保存中..." : editingId ? "保存修改" : "创建管道" }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- Archive Confirmation Modal -->
    <div
      v-if="confirmArchiveId"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      @click.self="confirmArchiveId = null"
    >
      <div class="card bg-white rounded-xl p-6 shadow-xl max-w-sm w-full mx-4">
        <div class="flex items-center gap-3 mb-3">
          <span class="shrink-0 flex h-10 w-10 items-center justify-center rounded-full bg-red-100 text-red-600">
            <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z"/></svg>
          </span>
          <div>
            <p class="text-sm font-medium text-surface-700">确认归档管道？</p>
            <p class="text-xs text-surface-500 mt-0.5 font-mono">{{ confirmArchiveId.slice(0, 12) }}...</p>
          </div>
        </div>
        <p class="text-xs text-surface-500 mb-4">归档后管道将不再可用，已有的处理流程不受影响。</p>
        <div class="flex justify-end gap-2">
          <button class="btn btn-secondary btn-sm" @click="confirmArchiveId = null">取消</button>
          <button class="btn btn-danger btn-sm" :disabled="archiving" @click="handleArchive">
            {{ archiving ? "归档中..." : "确认归档" }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-enter-active,
.modal-leave-active {
  transition: opacity 0.2s ease;
}
.modal-enter-active > div,
.modal-leave-active > div {
  transition: transform 0.2s ease, opacity 0.2s ease;
}
.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}
.modal-enter-from > div {
  transform: scale(0.95) translateY(10px);
  opacity: 0;
}
.modal-leave-to > div {
  transform: scale(0.95) translateY(10px);
  opacity: 0;
}

.btn-danger {
  background-color: #ef4444;
  color: white;
}
.btn-danger:hover {
  background-color: #dc2626;
}
.btn-ghost {
  background: transparent;
  color: inherit;
}
.btn-ghost:hover {
  background: rgba(0, 0, 0, 0.04);
}
.btn-xs {
  padding: 0.25rem 0.5rem;
  font-size: 0.75rem;
  line-height: 1rem;
  border-radius: 0.375rem;
  border: 1px solid transparent;
  cursor: pointer;
  transition: all 0.15s;
}
.btn-xs:hover {
  border-color: #e5e7eb;
}
</style>
