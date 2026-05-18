<script setup lang="ts">
defineOptions({ name: "MemoryTabApproved" });
import { ref, computed } from "vue";
import { useQuery, useMutation, useQueryClient } from "@tanstack/vue-query";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import {
  fetchMemories,
  fetchMemory,
  fetchMemoryVersions,
  fetchMemorySources,
  fetchMemoryRelations,
  searchMemories,
  expireMemory,
  restoreMemory,
  deleteMemory,
  createMemory,
  updateMemory,
} from "@/api/client";
import type {
  MemoryRead,
  MemoryVersionRead,
  MemorySourceRead,
  MemoryRelation,
  MemorySearchResult,
} from "@/types";

const queryClient = useQueryClient();

// ── List state ──
const page = ref(1);
const pageSize = ref(50);
const filterProjectId = ref("");
const filterStatus = ref("");
const filterSensitivity = ref("");

// ── Search state ──
const searchQuery = ref("");
const searchPage = ref(1);
const searchPageSize = ref(20);
const searchResults = ref<MemorySearchResult[]>([]);
const searchTotal = ref(0);
const searchPerformed = ref(false);
const searchLoading = ref(false);

// ── Detail drawer ──
const selectedMemory = ref<MemoryRead | null>(null);
const drawerTab = ref<"content" | "versions" | "sources" | "relations">("content");
const drawerOpen = ref(false);

// ── Create dialog ──
const showCreateDialog = ref(false);
const createTitle = ref("");
const createText = ref("");
const createProjectId = ref("");
const createSensitivity = ref("private");

// ── Edit dialog ──
const showEditDialog = ref(false);
const editTitle = ref("");
const editText = ref("");
const editMemoryId = ref("");

// ── List query ──
const listKey = computed(() => [
  "memories",
  page.value,
  pageSize.value,
  filterProjectId.value,
  filterStatus.value,
  filterSensitivity.value,
] as const);

const { data: memData, isLoading: memLoading, isError: memError, error: memErr } = useQuery({
  queryKey: listKey,
  queryFn: () =>
    fetchMemories({
      page: page.value,
      page_size: pageSize.value,
      project_id: filterProjectId.value || undefined,
      status: filterStatus.value || undefined,
      sensitivity_level: filterSensitivity.value || undefined,
    }),
  placeholderData: (prev) => prev,
});

// ── Detail queries ──
const memId = computed(() => selectedMemory.value?.memory_id ?? null);

const { data: memDetail } = useQuery({
  queryKey: ["memory-detail", memId],
  queryFn: () => fetchMemory(memId.value!),
  enabled: computed(() => drawerOpen.value && !!memId.value),
});

const { data: versions } = useQuery({
  queryKey: ["memory-versions", memId],
  queryFn: () => fetchMemoryVersions(memId.value!, { page: 1, page_size: 100 }),
  enabled: computed(() => drawerOpen.value && !!memId.value && drawerTab.value === "versions"),
});

const { data: sources } = useQuery({
  queryKey: ["memory-sources", memId],
  queryFn: () => fetchMemorySources(memId.value!),
  enabled: computed(() => drawerOpen.value && !!memId.value && drawerTab.value === "sources"),
});

const { data: relations } = useQuery({
  queryKey: ["memory-relations", memId],
  queryFn: () => fetchMemoryRelations(memId.value!),
  enabled: computed(() => drawerOpen.value && !!memId.value && drawerTab.value === "relations"),
});

// ── Mutations ──
const expireMutation = useMutation({
  mutationFn: (id: string) => expireMemory(id),
  onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ["memories"] });
    queryClient.invalidateQueries({ queryKey: ["memory-detail"] });
  },
});

const restoreMutation = useMutation({
  mutationFn: (id: string) => restoreMemory(id),
  onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ["memories"] });
    queryClient.invalidateQueries({ queryKey: ["memory-detail"] });
  },
});

const deleteMutation = useMutation({
  mutationFn: (id: string) => deleteMemory(id),
  onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ["memories"] });
    queryClient.invalidateQueries({ queryKey: ["memory-detail"] });
  },
});

const createMutation = useMutation({
  mutationFn: () =>
    createMemory({
      project_id: createProjectId.value,
      title: createTitle.value || undefined,
      memory_text: createText.value,
      sensitivity_level: createSensitivity.value,
    }),
  onSuccess: () => {
    showCreateDialog.value = false;
    createTitle.value = "";
    createText.value = "";
    createProjectId.value = "";
    createSensitivity.value = "private";
    queryClient.invalidateQueries({ queryKey: ["memories"] });
  },
});

const updateMutation = useMutation({
  mutationFn: () =>
    updateMemory(editMemoryId.value, {
      title: editTitle.value || undefined,
      memory_text: editText.value || undefined,
    }),
  onSuccess: () => {
    showEditDialog.value = false;
    queryClient.invalidateQueries({ queryKey: ["memories"] });
    queryClient.invalidateQueries({ queryKey: ["memory-detail"] });
  },
});

// ── Helpers ──
function performSearch() {
  const q = searchQuery.value.trim();
  if (!q) {
    searchPerformed.value = false;
    searchResults.value = [];
    searchTotal.value = 0;
    return;
  }
  searchLoading.value = true;
  searchPerformed.value = true;
  searchMemories({
    q,
    project_id: filterProjectId.value || undefined,
    page: searchPage.value,
    page_size: searchPageSize.value,
  })
    .then((data) => {
      searchResults.value = data.items;
      searchTotal.value = data.page_info?.total_items ?? 0;
    })
    .catch(() => {
      searchResults.value = [];
      searchTotal.value = 0;
    })
    .finally(() => {
      searchLoading.value = false;
    });
}

function clearSearch() {
  searchQuery.value = "";
  searchPerformed.value = false;
  searchResults.value = [];
  searchTotal.value = 0;
}

function openDetail(mem: MemoryRead) {
  selectedMemory.value = mem;
  drawerTab.value = "content";
  drawerOpen.value = true;
}

function closeDrawer() {
  drawerOpen.value = false;
  selectedMemory.value = null;
  drawerTab.value = "content";
}

function openEdit(mem: MemoryRead) {
  editMemoryId.value = mem.memory_id;
  editTitle.value = mem.title || "";
  editText.value = mem.memory_text;
  showEditDialog.value = true;
}

function clearFilters() {
  filterProjectId.value = "";
  filterStatus.value = "";
  filterSensitivity.value = "";
  page.value = 1;
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}

function truncate(text: string, max: number): string {
  if (!text) return "";
  return text.length > max ? text.slice(0, max) + "…" : text;
}

function sensitivityColor(level: string): string {
  const map: Record<string, string> = {
    public: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
    normal: "bg-blue-50 text-blue-700 ring-blue-600/20",
    private: "bg-amber-50 text-amber-700 ring-amber-600/20",
    sensitive: "bg-orange-50 text-orange-700 ring-orange-600/20",
    secret: "bg-red-50 text-red-700 ring-red-600/20",
  };
  return map[level] ?? "bg-surface-100 text-surface-600 ring-surface-500/20";
}

function sourceTypeLabel(type: string): string {
  const map: Record<string, string> = {
    message: "消息",
    raw_event: "原始事件",
    manual: "手动",
    importer: "导入",
    agent_submission: "Agent提交",
    candidate: "候选",
    asset: "资产",
    document: "文档",
    block: "块",
  };
  return map[type] ?? type;
}

function relationTypeLabel(type: string): string {
  const map: Record<string, string> = {
    conflicts_with: "冲突",
    supersedes: "替代",
    merged_into: "已合并",
    duplicates: "重复",
    supports: "支撑",
  };
  return map[type] ?? type;
}

function relationTypeColor(type: string): string {
  const map: Record<string, string> = {
    conflicts_with: "bg-red-50 text-red-700 ring-red-600/20",
    supersedes: "bg-orange-50 text-orange-700 ring-orange-600/20",
    merged_into: "bg-purple-50 text-purple-700 ring-purple-600/20",
    duplicates: "bg-amber-50 text-amber-700 ring-amber-600/20",
    supports: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
  };
  return map[type] ?? "bg-surface-100 text-surface-600 ring-surface-500/20";
}

function memoryActionLabel(status: string): string {
  const map: Record<string, string> = {
    active: "活跃",
    draft: "草稿",
    expired: "已过期",
    merged: "已合并",
    deleted: "已删除",
  };
  return map[status] ?? status;
}

function getSourceType(src: MemorySourceRead): string {
  if (src.message_id) return "message";
  if (src.raw_event_id) return "raw_event";
  if (src.asset_id) return "asset";
  if (src.document_id) return "document";
  if (src.block_id) return "block";
  if (src.candidate_id) return "candidate";
  return "unknown";
}

const memColumns = [
  { key: "title", label: "标题" },
  { key: "canonical_key", label: "规范键", width: "160px" },
  { key: "status", label: "状态", width: "80px" },
  { key: "sensitivity_level", label: "敏感度", width: "90px" },
  { key: "current_version", label: "版本", width: "60px" },
  { key: "updated_at", label: "更新时间", width: "160px" },
];

const searchColumns = [
  { key: "title", label: "标题" },
  { key: "canonical_key", label: "规范键", width: "140px" },
  { key: "sensitivity_level", label: "敏感度", width: "80px" },
  { key: "rank", label: "相关度", width: "80px" },
  { key: "fts_state", label: "索引", width: "70px" },
];
</script>

<template>
  <div class="space-y-6">
    <!-- ── Search Bar ── -->
    <div class="card p-4">
      <div class="flex items-center gap-3">
        <div class="relative flex-1">
          <svg
            class="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-surface-400"
            xmlns="http://www.w3.org/2000/svg"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            stroke-width="1.5"
          >
            <path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <input
            v-model="searchQuery"
            type="text"
            placeholder="搜索记忆内容 (FTS + ILIKE)…"
            class="w-full rounded-lg border border-surface-200 bg-white pl-10 pr-4 py-2.5 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            @keyup.enter="performSearch"
          />
        </div>
        <button class="btn btn-primary btn-sm" @click="performSearch" :disabled="searchLoading">
          {{ searchLoading ? "搜索中…" : "搜索" }}
        </button>
        <button v-if="searchPerformed" class="btn btn-ghost btn-sm" @click="clearSearch">清除</button>
      </div>

      <!-- Search Results -->
      <div v-if="searchPerformed" class="mt-4 border-t border-surface-200 pt-4">
        <div class="flex items-center justify-between mb-3">
          <span class="text-sm font-medium text-surface-700">
            搜索 "{{ searchQuery }}" — {{ searchTotal }} 条结果
          </span>
        </div>

        <div v-if="searchResults.length === 0 && !searchLoading" class="py-6 text-center text-sm text-surface-400">
          未找到匹配的记忆
        </div>

        <DataTable
          v-else
          :items="searchResults"
          :columns="searchColumns"
          :loading="searchLoading"
          :empty-message="'暂无搜索结果'"
          clickable
          row-key="memory_id"
          @row-click="(row: MemorySearchResult) => {
            const mem: MemoryRead = {
              memory_id: row.memory_id,
              project_id: null,
              canonical_key: row.canonical_key,
              title: row.title,
              memory_text: row.memory_text,
              current_version: row.current_version,
              sensitivity_level: row.sensitivity_level,
              status: row.status,
              activated_from_candidate_id: null,
              activated_by_review_item_id: null,
              activated_at: null,
              expired_at: null,
              created_at: null,
              updated_at: null,
            };
            openDetail(mem);
          }"
        >
          <template #cell-title="{ item }">
            <div class="max-w-xs">
              <div class="text-sm font-medium text-surface-800 truncate">
                {{ (item as MemorySearchResult).title || "无标题" }}
              </div>
              <div class="text-2xs text-surface-400 truncate mt-0.5">
                {{ truncate((item as MemorySearchResult).memory_text, 80) }}
              </div>
            </div>
          </template>
          <template #cell-canonical_key="{ value }">
            <span class="font-mono text-xs text-surface-500">{{ value }}</span>
          </template>
          <template #cell-sensitivity_level="{ value }">
            <span :class="['badge ring-1 ring-inset', sensitivityColor(value as string)]">
              {{ value }}
            </span>
          </template>
          <template #cell-rank="{ item }">
            <span class="font-mono text-xs text-surface-600">
              {{ ((item as MemorySearchResult).rank ?? 0).toFixed(2) }}
            </span>
          </template>
          <template #cell-fts_state="{ item }">
            <span
              :class="[
                'badge text-2xs',
                (item as MemorySearchResult).fts_state === 'stale'
                  ? 'bg-amber-50 text-amber-700 ring-amber-600/20'
                  : 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
              ]"
            >
              {{ (item as MemorySearchResult).fts_state === "stale" ? "待刷新" : "就绪" }}
            </span>
          </template>
        </DataTable>
      </div>
    </div>

    <!-- ── Filters ── -->
    <div class="card p-4">
      <div class="flex flex-wrap items-end gap-3">
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">项目ID</label>
          <input
            v-model="filterProjectId"
            type="text"
            placeholder="UUID..."
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm font-mono text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 w-64"
          />
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">状态</label>
          <select
            v-model="filterStatus"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部状态</option>
            <option value="active">活跃</option>
            <option value="draft">草稿</option>
            <option value="expired">已过期</option>
            <option value="merged">已合并</option>
            <option value="deleted">已删除</option>
          </select>
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">敏感度</label>
          <select
            v-model="filterSensitivity"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部级别</option>
            <option value="public">公开</option>
            <option value="normal">一般</option>
            <option value="private">私有</option>
            <option value="sensitive">敏感</option>
            <option value="secret">机密</option>
          </select>
        </div>

        <button class="btn btn-ghost btn-sm" @click="clearFilters">清除筛选</button>

        <button class="btn btn-primary btn-sm ml-auto" @click="showCreateDialog = true">
          新建记忆
        </button>

        <span class="text-xs text-surface-400 self-end pb-1">
          {{ memData?.page_info?.total_items ?? 0 }} 条记忆
        </span>
      </div>
    </div>

    <!-- Error -->
    <div
      v-if="memError"
      class="card border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载记忆列表失败: {{ (memErr as Error)?.message }}
    </div>

    <!-- Memory Table -->
    <DataTable
      :items="memData?.items ?? []"
      :columns="memColumns"
      :loading="memLoading"
      :empty-message="'暂无记忆 — 请创建或通过候选审批生成记忆'"
      clickable
      row-key="memory_id"
      @row-click="openDetail"
    >
      <template #cell-title="{ item }">
        <div class="max-w-xs">
          <div class="text-sm font-medium text-surface-800 truncate">
            {{ (item as MemoryRead).title || "无标题" }}
          </div>
          <div class="text-2xs text-surface-400 truncate mt-0.5">
            {{ truncate((item as MemoryRead).memory_text, 80) }}
          </div>
        </div>
      </template>

      <template #cell-canonical_key="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ value }}</span>
      </template>

      <template #cell-status="{ value }">
        <StatusBadge :status="value as string" />
      </template>

      <template #cell-sensitivity_level="{ value }">
        <span :class="['badge ring-1 ring-inset', sensitivityColor(value as string)]">
          {{ value }}
        </span>
      </template>

      <template #cell-current_version="{ value }">
        <span class="font-mono text-xs">v{{ value }}</span>
      </template>

      <template #cell-updated_at="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
      </template>
    </DataTable>

    <Pagination
      v-if="memData?.page_info"
      :page-info="memData.page_info"
      @page-change="(p: number) => (page = p)"
    />

    <!-- ══════════════════════════════════════════════════════ -->
    <!-- Memory Detail Drawer -->
    <!-- ══════════════════════════════════════════════════════ -->
    <DetailDrawer
      :open="drawerOpen"
      :title="selectedMemory?.title ?? '记忆详情'"
      width="w-[640px] max-w-full"
      @close="closeDrawer"
    >
      <!-- Action buttons -->
      <div class="flex flex-wrap items-center gap-2 mb-4 -mx-5 px-5 pb-3 border-b border-surface-200">
        <button
          v-if="selectedMemory?.status === 'active' || selectedMemory?.status === 'draft'"
          class="btn btn-ghost btn-xs"
          @click="openEdit(selectedMemory!)"
        >
          编辑
        </button>
        <button
          v-if="selectedMemory?.status === 'active'"
          class="btn btn-xs bg-amber-50 text-amber-700 hover:bg-amber-100 border border-amber-200"
          @click="expireMutation.mutate(selectedMemory!.memory_id)"
          :disabled="!!expireMutation.isPending"
        >
          过期
        </button>
        <button
          v-if="selectedMemory?.status === 'expired' || selectedMemory?.status === 'deleted'"
          class="btn btn-xs bg-emerald-50 text-emerald-700 hover:bg-emerald-100 border border-emerald-200"
          @click="restoreMutation.mutate(selectedMemory!.memory_id)"
          :disabled="!!restoreMutation.isPending"
        >
          恢复
        </button>
        <button
          v-if="selectedMemory?.status !== 'deleted'"
          class="btn btn-xs bg-red-50 text-red-700 hover:bg-red-100 border border-red-200"
          @click="deleteMutation.mutate(selectedMemory!.memory_id)"
          :disabled="!!deleteMutation.isPending"
        >
          删除
        </button>
        <span class="ml-auto text-xs text-surface-400">
          {{ memoryActionLabel(selectedMemory?.status ?? "") }}
        </span>
      </div>

      <!-- Tab bar -->
      <div class="flex border-b border-surface-200 mb-4 -mx-5 px-5">
        <button
          v-for="tab in [
            { key: 'content', label: '内容' },
            { key: 'versions', label: `版本 (${versions?.items?.length ?? '...'})` },
            { key: 'sources', label: `来源 (${sources?.items?.length ?? '...'})` },
            { key: 'relations', label: `关系 (${relations?.items?.length ?? '...'})` },
          ]"
          :key="tab.key"
          @click="drawerTab = tab.key as 'content' | 'versions' | 'sources' | 'relations'"
          :class="[
            'pb-2.5 px-3 text-sm font-medium border-b-2 transition-colors',
            drawerTab === tab.key
              ? 'border-brand-500 text-brand-600'
              : 'border-transparent text-surface-400 hover:text-surface-600',
          ]"
        >
          {{ tab.label }}
        </button>
      </div>

      <!-- Tab: Content -->
      <div v-if="drawerTab === 'content' && memDetail" class="space-y-4">
        <dl class="space-y-3">
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">记忆ID</dt>
            <dd class="mt-0.5 font-mono text-xs text-surface-700 break-all">{{ memDetail.memory_id }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">规范键</dt>
            <dd class="mt-0.5 font-mono text-xs text-brand-600">{{ memDetail.canonical_key }}</dd>
          </div>
          <div v-if="memDetail.project_id">
            <dt class="text-2xs font-semibold uppercase text-surface-400">项目ID</dt>
            <dd class="mt-0.5 font-mono text-xs text-surface-600">{{ memDetail.project_id }}</dd>
          </div>
          <div class="flex gap-6">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">状态</dt>
              <dd class="mt-0.5"><StatusBadge :status="memDetail.status" /></dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">版本</dt>
              <dd class="mt-0.5 font-mono text-sm">v{{ memDetail.current_version }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">敏感等级</dt>
              <dd class="mt-0.5">
                <span :class="['badge ring-1 ring-inset', sensitivityColor(memDetail.sensitivity_level)]">
                  {{ memDetail.sensitivity_level }}
                </span>
              </dd>
            </div>
          </div>
          <div v-if="memDetail.activated_from_candidate_id">
            <dt class="text-2xs font-semibold uppercase text-surface-400">来源候选</dt>
            <dd class="mt-0.5 font-mono text-xs text-surface-600 break-all">{{ memDetail.activated_from_candidate_id }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt>
            <dd class="mt-0.5 text-sm">{{ formatTime(memDetail.created_at) }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">更新时间</dt>
            <dd class="mt-0.5 text-sm">{{ formatTime(memDetail.updated_at) }}</dd>
          </div>
          <div v-if="memDetail.activated_at">
            <dt class="text-2xs font-semibold uppercase text-surface-400">激活时间</dt>
            <dd class="mt-0.5 text-sm">{{ formatTime(memDetail.activated_at) }}</dd>
          </div>
          <div v-if="memDetail.expired_at">
            <dt class="text-2xs font-semibold uppercase text-surface-400">过期时间</dt>
            <dd class="mt-0.5 text-sm text-amber-600">{{ formatTime(memDetail.expired_at) }}</dd>
          </div>
        </dl>

        <div class="border-t border-surface-200 pt-4 mt-4">
          <h4 class="text-xs font-semibold uppercase text-surface-400 mb-2">记忆内容</h4>
          <div class="rounded-lg border border-surface-200 bg-surface-50 p-4 text-sm text-surface-700 leading-relaxed whitespace-pre-wrap max-h-96 overflow-y-auto">
            {{ memDetail.memory_text }}
          </div>
        </div>
      </div>

      <!-- Tab: Versions -->
      <div v-if="drawerTab === 'versions'">
        <div v-if="!versions?.items || versions.items.length === 0" class="py-8 text-center text-sm text-surface-400">
          暂无版本记录
        </div>
        <div v-else class="space-y-3">
          <div
            v-for="ver in versions.items"
            :key="ver.memory_version_id"
            class="rounded-lg border border-surface-200 p-3"
          >
            <div class="flex items-center justify-between mb-2">
              <div class="flex items-center gap-2">
                <span class="font-mono text-xs font-semibold text-surface-600">v{{ ver.version }}</span>
                <span :class="['badge text-2xs', ver.action === 'create' ? 'bg-emerald-50 text-emerald-700' : ver.action === 'delete' ? 'bg-red-50 text-red-700' : 'bg-blue-50 text-blue-700']">
                  {{ ver.action }}
                </span>
                <span class="text-2xs text-surface-400">{{ ver.actor_type }}</span>
              </div>
              <span class="text-2xs text-surface-400">{{ formatTime(ver.created_at) }}</span>
            </div>
            <div v-if="ver.reason" class="text-xs text-surface-500 mb-2">原因: {{ ver.reason }}</div>
            <div v-if="ver.action !== 'create'" class="text-xs text-surface-500 mb-1">
              <span class="font-medium">变更前:</span>
              <pre class="mt-1 bg-surface-50 rounded p-2 text-2xs overflow-x-auto">{{ JSON.stringify(ver.before_json, null, 2) }}</pre>
            </div>
            <div class="text-xs text-surface-500">
              <span class="font-medium">变更后:</span>
              <pre class="mt-1 bg-surface-50 rounded p-2 text-2xs overflow-x-auto">{{ JSON.stringify(ver.after_json, null, 2) }}</pre>
            </div>
          </div>
        </div>
      </div>

      <!-- Tab: Sources -->
      <div v-if="drawerTab === 'sources'">
        <div v-if="!sources?.items || sources.items.length === 0" class="py-8 text-center text-sm text-surface-400">
          暂无来源关联
        </div>
        <div v-else class="space-y-3">
          <div
            v-for="src in sources.items"
            :key="src.memory_source_id"
            class="rounded-lg border border-surface-200 p-3"
          >
            <div class="flex items-center justify-between mb-2 flex-wrap gap-1">
              <span class="text-xs font-medium text-surface-700">
                {{ sourceTypeLabel(getSourceType(src)) }}
              </span>
              <span :class="['badge text-2xs', src.source_role === 'evidence' ? 'bg-blue-50 text-blue-700' : src.source_role === 'conflict' ? 'bg-red-50 text-red-700' : 'bg-surface-100 text-surface-600']">
                {{ src.source_role }}
              </span>
            </div>
            <div class="font-mono text-2xs text-surface-400 mb-1">
              v{{ src.memory_version }} · {{ src.memory_source_id.slice(0, 12) }}…
            </div>
            <div v-if="src.source_span && Object.keys(src.source_span).length > 0" class="text-xs text-surface-500 bg-surface-50 rounded p-2 mt-2">
              <span class="font-medium">引用片段:</span>
              <pre class="mt-1 text-2xs">{{ JSON.stringify(src.source_span, null, 2) }}</pre>
            </div>
            <div v-if="src.confidence != null" class="text-xs text-surface-500 mt-1">
              置信度: {{ (src.confidence * 100).toFixed(1) }}%
            </div>
          </div>
        </div>
      </div>

      <!-- Tab: Relations -->
      <div v-if="drawerTab === 'relations'">
        <div v-if="!relations?.items || relations.items.length === 0" class="py-8 text-center text-sm text-surface-400">
          暂无记忆关系
        </div>
        <div v-else class="space-y-3">
          <div
            v-for="rel in relations.items"
            :key="rel.memory_relation_id"
            class="rounded-lg border border-surface-200 p-3"
          >
            <div class="flex items-center gap-2 mb-2">
              <span :class="['badge', relationTypeColor(rel.relation_type)]">
                {{ relationTypeLabel(rel.relation_type) }}
              </span>
              <StatusBadge :status="rel.relation_status" />
            </div>
            <div class="text-xs text-surface-500">
              <div class="font-mono text-2xs">
                <span class="text-surface-400">from:</span> {{ rel.from_memory_id.slice(0, 12) }}…
                <span class="text-surface-400 ml-2">to:</span> {{ rel.to_memory_id.slice(0, 12) }}…
              </div>
              <div v-if="rel.reason" class="mt-1">原因: {{ rel.reason }}</div>
              <div class="text-2xs text-surface-400 mt-1">{{ formatTime(rel.created_at) }}</div>
            </div>
          </div>
        </div>
      </div>
    </DetailDrawer>

    <!-- ══════════════════════════════════════════════════════ -->
    <!-- Create Memory Dialog -->
    <!-- ══════════════════════════════════════════════════════ -->
    <Teleport to="body">
      <div
        v-if="showCreateDialog"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
        @click.self="showCreateDialog = false"
      >
        <div class="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4 p-6">
          <h3 class="text-lg font-semibold text-surface-800 mb-4">新建记忆</h3>
          <div class="space-y-4">
            <div>
              <label class="block text-2xs font-semibold uppercase text-surface-400 mb-1">项目ID *</label>
              <input
                v-model="createProjectId"
                type="text"
                placeholder="UUID..."
                class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm font-mono text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              />
            </div>
            <div>
              <label class="block text-2xs font-semibold uppercase text-surface-400 mb-1">标题</label>
              <input
                v-model="createTitle"
                type="text"
                placeholder="记忆标题..."
                class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              />
            </div>
            <div>
              <label class="block text-2xs font-semibold uppercase text-surface-400 mb-1">内容 *</label>
              <textarea
                v-model="createText"
                rows="5"
                placeholder="记忆内容..."
                class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 resize-y"
              ></textarea>
            </div>
            <div>
              <label class="block text-2xs font-semibold uppercase text-surface-400 mb-1">敏感等级</label>
              <select
                v-model="createSensitivity"
                class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              >
                <option value="public">公开</option>
                <option value="normal">一般</option>
                <option value="private">私有</option>
                <option value="sensitive">敏感</option>
                <option value="secret">机密</option>
              </select>
            </div>
          </div>
          <div class="flex justify-end gap-3 mt-6 pt-4 border-t border-surface-200">
            <button class="btn btn-ghost btn-sm" @click="showCreateDialog = false">取消</button>
            <button
              class="btn btn-primary btn-sm"
              :disabled="!createProjectId || !createText || !!createMutation.isPending"
              @click="createMutation.mutate()"
            >
              {{ createMutation.isPending ? "创建中…" : "创建" }}
            </button>
          </div>
          <div v-if="createMutation.isError" class="mt-2 text-xs text-red-600">
            创建失败: {{ (createMutation.error as unknown as Error)?.message }}
          </div>
        </div>
      </div>
    </Teleport>

    <!-- ══════════════════════════════════════════════════════ -->
    <!-- Edit Memory Dialog -->
    <!-- ══════════════════════════════════════════════════════ -->
    <Teleport to="body">
      <div
        v-if="showEditDialog"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
        @click.self="showEditDialog = false"
      >
        <div class="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4 p-6">
          <h3 class="text-lg font-semibold text-surface-800 mb-4">编辑记忆</h3>
          <div class="space-y-4">
            <div>
              <label class="block text-2xs font-semibold uppercase text-surface-400 mb-1">标题</label>
              <input
                v-model="editTitle"
                type="text"
                class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              />
            </div>
            <div>
              <label class="block text-2xs font-semibold uppercase text-surface-400 mb-1">内容</label>
              <textarea
                v-model="editText"
                rows="5"
                class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 resize-y"
              ></textarea>
            </div>
          </div>
          <div class="flex justify-end gap-3 mt-6 pt-4 border-t border-surface-200">
            <button class="btn btn-ghost btn-sm" @click="showEditDialog = false">取消</button>
            <button
              class="btn btn-primary btn-sm"
              :disabled="!!updateMutation.isPending"
              @click="updateMutation.mutate()"
            >
              {{ updateMutation.isPending ? "保存中…" : "保存" }}
            </button>
          </div>
          <div v-if="updateMutation.isError" class="mt-2 text-xs text-red-600">
            更新失败: {{ (updateMutation.error as unknown as Error)?.message }}
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>
