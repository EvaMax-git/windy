<script setup lang="ts">
/**
 * StoreTab — 知识子库管理 (对齐 /api/v4/sub-libraries)
 *
 * 功能:
 *   - 子库列表 (带分页)
 *   - 新建子库 (含 capability_json 配置)
 *   - 编辑子库 (名称 / key / capability_json)
 *   - 删除子库 (带错误提示)
 *
 * 管理知识库后端: 向量库 / 知识图谱 / 全文索引 / 自定义
 */
import { ref, computed } from "vue";
import { useRouter } from "vue-router";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import {
  fetchSubLibraries,
  createSubLibrary,
  updateSubLibrary,
  deleteSubLibrary,
} from "@/api/client";
import type { SubLibraryRead } from "@/types";
import {
  SUB_LIBRARY_TYPE_LABELS,
  SUB_LIBRARY_TYPE_ICONS,
} from "@/types";

const router = useRouter();
const queryClient = useQueryClient();

// ── List state ──
const filterType = ref("");
const currentPage = ref(1);
const pageSize = 10;

const {
  data: listResponse,
  isLoading,
  isError,
  error,
} = useQuery({
  queryKey: ["sub-libraries-tab", filterType, currentPage],
  queryFn: () =>
    fetchSubLibraries({
      type: filterType.value || undefined,
      page: currentPage.value,
      page_size: pageSize,
    }),
  retry: 3,
});

const libraries = computed(() => listResponse.value?.items ?? []);
const pageInfo = computed(() => listResponse.value?.page_info ?? null);

// ── Create form ──
const showCreateForm = ref(false);
const createForm = ref({
  name: "",
  type: "vector" as string,
  key: "",
  capability_json: "",
});
const creating = ref(false);

// ── Edit form ──
const editingLib = ref<SubLibraryRead | null>(null);
const editForm = ref({
  name: "",
  type: "vector" as string,
  key: "",
  capability_json: "",
});
const updating = ref(false);

// ── Confirm delete ──
const confirmDeleteId = ref<string | null>(null);
const deleteError = ref<string | null>(null);

// ── Toast notification ──
const toastMessage = ref<string | null>(null);
const toastType = ref<"success" | "error">("success");

function showToast(msg: string, type: "success" | "error" = "success") {
  toastMessage.value = msg;
  toastType.value = type;
  setTimeout(() => {
    toastMessage.value = null;
  }, 4000);
}

// ── Type options ──
const TYPE_OPTIONS: { value: string; label: string; icon: string; description: string }[] = [
  { value: "vector", label: "向量库", icon: "🧠", description: "语义向量嵌入，支持余弦相似度搜索" },
  { value: "graph", label: "知识图谱", icon: "🔗", description: "实体/关系图谱，支持图遍历查询" },
  { value: "fulltext", label: "全文索引", icon: "🔍", description: "全文搜索引擎索引，支持 BM25 检索" },
  { value: "custom", label: "自定义", icon: "⚙️", description: "自定义知识库后端" },
];

// ── Helpers ──
function formatTime(iso?: string): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function getTypeLabel(type: string): string {
  return SUB_LIBRARY_TYPE_LABELS[type] || type;
}

function getTypeIcon(type: string): string {
  return SUB_LIBRARY_TYPE_ICONS[type] || "📦";
}

function getTypeColor(type: string): string {
  const colors: Record<string, string> = {
    vector: "bg-violet-50 text-violet-700 border-violet-200",
    graph: "bg-emerald-50 text-emerald-700 border-emerald-200",
    fulltext: "bg-blue-50 text-blue-700 border-blue-200",
    custom: "bg-amber-50 text-amber-700 border-amber-200",
  };
  return colors[type] || "bg-surface-100 text-surface-600 border-surface-200";
}

function formatCapability(cap: Record<string, unknown>): string {
  if (!cap || Object.keys(cap).length === 0) return "—";
  const parts: string[] = [];
  if (cap.accept_chunks) {
    const chunks = (cap.accept_chunks as string[]);
    parts.push(`接受: ${chunks.join(", ")}`);
  }
  if (cap.search) parts.push(`搜索: ${cap.search}`);
  if (cap.normalize) parts.push(`归一化: ${cap.normalize}`);
  return parts.length > 0 ? parts.join(" · ") : JSON.stringify(cap);
}

function parseCapabilityJson(text: string): Record<string, unknown> {
  if (!text.trim()) return {};
  try {
    return JSON.parse(text.trim()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

// ── Navigate to KnowledgeTab filtered by this sub-library ──
function viewDocuments(libId: string) {
  router.push({ path: "/app/knowledge", query: { tab: "knowledge", sub_library_id: libId } });
}

// ── Pagination ──
function goToPage(page: number) {
  currentPage.value = page;
}

// ── Create actions ──
async function handleCreate() {
  if (!createForm.value.name.trim()) return;
  creating.value = true;
  deleteError.value = null;
  try {
    const cap = parseCapabilityJson(createForm.value.capability_json);
    await createSubLibrary({
      name: createForm.value.name.trim(),
      type: createForm.value.type,
      key: createForm.value.key.trim() || undefined,
      capability_json: Object.keys(cap).length > 0 ? cap : undefined,
    });
    showCreateForm.value = false;
    createForm.value = { name: "", type: "vector", key: "", capability_json: "" };
    // Reset to page 1 to make the new item visible
    currentPage.value = 1;
    filterType.value = "";
    await queryClient.invalidateQueries({ queryKey: ["sub-libraries-tab"] });
    showToast("子库创建成功", "success");
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : "创建失败";
    showToast(msg, "error");
  } finally {
    creating.value = false;
  }
}

// ── Edit actions ──
function openEdit(lib: SubLibraryRead) {
  editingLib.value = lib;
  editForm.value = {
    name: lib.name,
    type: lib.type,
    key: lib.key,
    capability_json: JSON.stringify(lib.capability_json || {}, null, 2),
  };
}

function cancelEdit() {
  editingLib.value = null;
}

async function handleUpdate() {
  if (!editingLib.value) return;
  updating.value = true;
  deleteError.value = null;
  try {
    const cap = parseCapabilityJson(editForm.value.capability_json);
    await updateSubLibrary(editingLib.value.id, {
      name: editForm.value.name.trim() || undefined,
      type: editForm.value.type || undefined,
      key: editForm.value.key.trim() || undefined,
      capability_json: Object.keys(cap).length > 0 ? cap : undefined,
    });
    editingLib.value = null;
    await queryClient.invalidateQueries({ queryKey: ["sub-libraries-tab"] });
    showToast("子库更新成功", "success");
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : "更新失败";
    showToast(msg, "error");
  } finally {
    updating.value = false;
  }
}

// ── Delete actions ──
async function handleDelete(libId: string) {
  deleteError.value = null;
  try {
    await deleteSubLibrary(libId);
    confirmDeleteId.value = null;
    // If we deleted the last item on the page, go back
    if (libraries.value.length === 1 && currentPage.value > 1) {
      currentPage.value--;
    }
    await queryClient.invalidateQueries({ queryKey: ["sub-libraries-tab"] });
    showToast("子库已删除", "success");
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : "删除失败";
    deleteError.value = msg;
    showToast(msg, "error");
  }
}
</script>

<template>
  <div>
    <!-- Toast notification -->
    <Transition name="fade">
      <div
        v-if="toastMessage"
        :class="[
          'fixed top-4 right-4 z-[100] px-4 py-3 rounded-xl shadow-xl text-sm font-medium transition-all duration-300',
          toastType === 'success' ? 'bg-emerald-600 text-white' : 'bg-red-600 text-white',
        ]"
      >
        {{ toastMessage }}
      </div>
    </Transition>

    <!-- Toolbar -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-center gap-3">
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-medium text-surface-400">类型</label>
          <select
            v-model="filterType"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            @change="currentPage = 1"
          >
            <option value="">全部类型</option>
            <option v-for="opt in TYPE_OPTIONS" :key="opt.value" :value="opt.value">
              {{ opt.icon }} {{ opt.label }}
            </option>
          </select>
        </div>

        <span class="self-end text-xs text-surface-400 ml-auto">
          {{ pageInfo?.total_items ?? '?' }} 个子库
        </span>

        <button class="btn btn-primary btn-sm self-end" data-testid="store-create" @click="showCreateForm = true">
          + 注册子库
        </button>
      </div>
    </div>

    <!-- Error -->
    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载子库失败: {{ (error as Error)?.message }}
    </div>

    <!-- Delete error -->
    <div
      v-if="deleteError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      删除失败: {{ deleteError }}
    </div>

    <!-- Create Form -->
    <div v-if="showCreateForm" class="card mb-6 border-brand-200 bg-brand-50 p-5">
      <h3 class="text-sm font-semibold text-surface-700 mb-4">注册新的知识子库</h3>
      <div class="space-y-3">
        <div class="grid grid-cols-2 gap-3">
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-medium text-surface-400">名称 *</label>
            <input
              v-model="createForm.name"
              type="text"
              class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              placeholder="例如: 默认向量库"
            />
          </div>
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-medium text-surface-400">类型 *</label>
            <select
              v-model="createForm.type"
              class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            >
              <option v-for="opt in TYPE_OPTIONS" :key="opt.value" :value="opt.value">
                {{ opt.label }}
              </option>
            </select>
          </div>
        </div>
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-medium text-surface-400">Key (可选标识)</label>
          <input
            v-model="createForm.key"
            type="text"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm font-mono text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            placeholder="例如: default_vector"
          />
        </div>
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-medium text-surface-400">Capability JSON (可选)</label>
          <textarea
            v-model="createForm.capability_json"
            rows="3"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-xs font-mono text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            placeholder='{"accept_chunks": ["text"], "search": "cosine", "normalize": "minmax"}'
          ></textarea>
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button class="btn btn-secondary btn-sm" @click="showCreateForm = false">取消</button>
          <button
            class="btn btn-primary btn-sm"
            :disabled="creating || !createForm.name.trim()"
            @click="handleCreate"
          >
            {{ creating ? "注册中..." : "注册" }}
          </button>
        </div>
      </div>
    </div>

    <!-- Edit Form -->
    <div v-if="editingLib" class="card mb-6 border-amber-200 bg-amber-50 p-5">
      <h3 class="text-sm font-semibold text-surface-700 mb-4">
        编辑子库: {{ editingLib.name }}
        <span class="text-2xs font-mono text-surface-400 ml-2">{{ editingLib.id.slice(0, 8) }}...</span>
      </h3>
      <div class="space-y-3">
        <div class="grid grid-cols-2 gap-3">
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-medium text-surface-400">名称</label>
            <input
              v-model="editForm.name"
              type="text"
              class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            />
          </div>
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-medium text-surface-400">类型</label>
            <select
              v-model="editForm.type"
              class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            >
              <option v-for="opt in TYPE_OPTIONS" :key="opt.value" :value="opt.value">
                {{ opt.label }}
              </option>
            </select>
          </div>
        </div>
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-medium text-surface-400">Key (标识)</label>
          <input
            v-model="editForm.key"
            type="text"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm font-mono text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          />
        </div>
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-medium text-surface-400">Capability JSON</label>
          <textarea
            v-model="editForm.capability_json"
            rows="4"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-xs font-mono text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          ></textarea>
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button class="btn btn-secondary btn-sm" @click="cancelEdit">取消</button>
          <button
            class="btn btn-primary btn-sm"
            :disabled="updating"
            @click="handleUpdate"
          >
            {{ updating ? "保存中..." : "保存更改" }}
          </button>
        </div>
      </div>
    </div>

    <!-- Loading -->
    <div v-if="isLoading" class="py-8">
      <LoadingSkeleton variant="detail" />
    </div>

    <!-- Library List -->
    <div v-else-if="libraries.length > 0" class="space-y-3">
      <div
        v-for="lib in libraries"
        :key="lib.id"
        class="card p-4 hover:shadow-sm transition-shadow"
      >
        <div class="flex items-start gap-4">
          <!-- Type badge -->
          <span
            :class="[
              'shrink-0 inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-2xs font-medium border',
              getTypeColor(lib.type),
            ]"
          >
            <span>{{ getTypeIcon(lib.type) }}</span>
            {{ getTypeLabel(lib.type) }}
          </span>

          <!-- Content -->
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <p class="text-sm font-medium text-surface-800">{{ lib.name }}</p>
              <span
                v-if="lib.key"
                class="text-2xs font-mono text-surface-400 bg-surface-100 rounded px-1.5 py-0.5"
              >
                {{ lib.key }}
              </span>
            </div>

            <!-- Capability info -->
            <p v-if="lib.capability_json && Object.keys(lib.capability_json).length > 0" class="text-xs text-surface-500 mt-1">
              {{ formatCapability(lib.capability_json) }}
            </p>

            <!-- ID and timestamp -->
            <div class="flex items-center gap-3 mt-2 text-2xs text-surface-400">
              <span class="font-mono">{{ lib.id.slice(0, 8) }}...</span>
              <span v-if="lib.created_at">创建于 {{ formatTime(lib.created_at) }}</span>
            </div>
          </div>

          <!-- Actions -->
          <div class="shrink-0 flex items-center gap-1">
            <button
              class="btn btn-ghost btn-xs text-xs text-brand-600"
              data-testid="store-view-docs"
              @click="viewDocuments(lib.id)"
            >
              查看文档
            </button>
            <button
              class="btn btn-ghost btn-xs text-xs text-amber-600"
              data-testid="store-edit"
              @click="openEdit(lib)"
            >
              编辑
            </button>
            <button
              class="btn btn-ghost btn-xs text-xs text-red-500"
              data-testid="store-delete"
              @click="confirmDeleteId = lib.id"
            >
              删除
            </button>
          </div>
        </div>
      </div>

      <!-- Pagination -->
      <div v-if="pageInfo && pageInfo.total_pages > 1" class="flex items-center justify-between pt-2">
        <span class="text-2xs text-surface-400">
          共 {{ pageInfo.total_items }} 条，第 {{ pageInfo.page }} / {{ pageInfo.total_pages }} 页
        </span>
        <div class="flex gap-1">
          <button
            class="btn btn-ghost btn-xs"
            :disabled="!pageInfo.has_previous"
            @click="goToPage(pageInfo.page - 1)"
          >
            ← 上一页
          </button>
          <button
            v-for="p in Math.min(pageInfo.total_pages, 10)"
            :key="p"
            :class="[
              'btn btn-ghost btn-xs min-w-[28px]',
              p === pageInfo.page ? 'bg-brand-100 text-brand-700 font-semibold' : '',
            ]"
            @click="goToPage(p)"
          >
            {{ p }}
          </button>
          <button
            class="btn btn-ghost btn-xs"
            :disabled="!pageInfo.has_next"
            @click="goToPage(pageInfo.page + 1)"
          >
            下一页 →
          </button>
        </div>
      </div>
    </div>

    <!-- Empty -->
    <div
      v-else-if="!isLoading"
      class="flex flex-col items-center justify-center py-16 text-sm text-surface-400"
    >
      <span class="text-5xl mb-3">📚</span>
      <span>暂无知识子库</span>
      <span class="text-2xs text-surface-300 mt-1">点击上方"注册子库"创建第一个子库</span>
    </div>

    <!-- Delete confirmation -->
    <div
      v-if="confirmDeleteId"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      @click.self="confirmDeleteId = null"
    >
      <div class="card bg-white rounded-xl p-6 shadow-xl max-w-sm w-full mx-4">
        <p class="text-sm font-medium text-surface-700 mb-2">确认删除？</p>
        <p class="text-xs text-surface-500 mb-4">
          删除后不可恢复。该子库将从所有绑定中移除。
        </p>
        <div class="flex justify-end gap-2">
          <button class="btn btn-secondary btn-sm" @click="confirmDeleteId = null">取消</button>
          <button class="btn btn-danger btn-sm" @click="handleDelete(confirmDeleteId!)">确认删除</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
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
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
