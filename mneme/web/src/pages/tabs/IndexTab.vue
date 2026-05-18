<script setup lang="ts">
import { ref, computed } from "vue";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import {
  fetchKnowledgeDocuments,
  fetchDocumentIndexState,
  refreshKnowledgeIndexes,
  fetchProjects,
} from "@/api/client";
import type {
  KnowledgeDocument,
  IndexState,
  ProjectRead,
} from "@/types";
import { INDEX_STATE_LABELS } from "@/types";

const queryClient = useQueryClient();

// ── List state ──
const page = ref(1);
const pageSize = ref(50);
const filterProjectId = ref("");
const filterStatus = ref("");

// Fetch projects for dropdown
const { data: projectsData } = useQuery({
  queryKey: ["projects-list"],
  queryFn: () => fetchProjects({ page: 1, page_size: 200 }),
  staleTime: 60000,
});
const projects = computed(() => projectsData.value?.items ?? []);

const listKey = computed(() => [
  "index-documents",
  page.value,
  pageSize.value,
  filterProjectId.value || "__none__",
  filterStatus.value,
] as const);

const { data, isLoading, isError, error } = useQuery({
  queryKey: listKey,
  queryFn: () =>
    fetchKnowledgeDocuments({
      page: page.value,
      page_size: pageSize.value,
      project_id: filterProjectId.value || undefined,
      status: filterStatus.value || undefined,
    }),
  placeholderData: (prev) => prev,
  retry: 3,
});

// ── Detail drawer ──
const selectedDoc = ref<KnowledgeDocument | null>(null);
const drawerOpen = ref(false);

const docId = computed(() => selectedDoc.value?.document_id ?? null);

const { data: indexState, isLoading: indexLoading } = useQuery({
  queryKey: ["index-state-detail", docId],
  queryFn: () => fetchDocumentIndexState(docId.value!),
  enabled: computed(() => drawerOpen.value && !!docId.value),
});

const columns = [
  { key: "title", label: "文档", width: "240px" },
  { key: "document_status", label: "文档状态", width: "90px" },
  { key: "current_version", label: "版本", width: "60px" },
  { key: "sensitivity_level", label: "敏感度", width: "90px" },
  { key: "updated_at", label: "更新时间", width: "160px" },
];

function openDetail(doc: KnowledgeDocument) {
  selectedDoc.value = doc;
  drawerOpen.value = true;
}

function closeDrawer() {
  drawerOpen.value = false;
  selectedDoc.value = null;
}

const refreshToast = ref<{ msg: string; type: "success" | "error" } | null>(null);

function showRefreshToast(msg: string, type: "success" | "error") {
  refreshToast.value = { msg, type };
  setTimeout(() => { refreshToast.value = null; }, 4000);
}

async function handleRefreshAll() {
  try {
    await refreshKnowledgeIndexes();
    showRefreshToast("索引刷新已触发", "success");
    queryClient.invalidateQueries({ queryKey: ["index-documents"] });
  } catch (e: unknown) {
    showRefreshToast("刷新失败: " + ((e as Error)?.message || ""), "error");
  }
}

function clearFilters() {
  filterProjectId.value = "";
  filterStatus.value = "";
  page.value = 1;
}

function formatTime(iso: string): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
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

// ── Index state summary grid ──
function indexStateBadgeClass(state: string): string {
  const map: Record<string, string> = {
    ready: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
    pending: "bg-amber-50 text-amber-700 ring-amber-600/20",
    stale: "bg-orange-50 text-orange-700 ring-orange-600/20",
    failed: "bg-red-50 text-red-700 ring-red-600/20",
    not_indexed: "bg-surface-100 text-surface-500 ring-surface-400/20",
  };
  return map[state] ?? "bg-surface-100 text-surface-500 ring-surface-400/20";
}
</script>

<template>
  <div>
    <!-- Toast -->
    <Transition name="fade">
      <div
        v-if="refreshToast"
        :class="[
          'fixed top-4 right-4 z-[100] px-4 py-3 rounded-xl shadow-xl text-sm font-medium transition-all duration-300',
          refreshToast.type === 'success' ? 'bg-emerald-600 text-white' : 'bg-red-600 text-white',
        ]"
      >
        {{ refreshToast.msg }}
      </div>
    </Transition>

    <!-- Description -->
    <div class="card mb-6 p-4 bg-brand-50/50 border-brand-100">
      <div class="flex items-start gap-3">
        <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5 text-brand-500 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M4 7v10c0 2 1 3 3 3h10c2 0 3-1 3-3V7M4 7c0-2 1-3 3-3h10c2 0 3 1 3 3M4 7h16m-9 4h4m-4 4h4" />
        </svg>
        <div>
          <p class="text-sm font-medium text-surface-700">索引管理</p>
          <p class="mt-0.5 text-xs text-surface-500">
            查看每个文档的 FTS / Vector / Graph / Citation 索引构建状态。点击文档行查看详细索引版本信息。
          </p>
        </div>
      </div>
    </div>

    <!-- Filters -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-end gap-3">
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">项目</label>
          <select
            v-model="filterProjectId"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            @change="page = 1"
          >
            <option value="">全部项目</option>
            <option v-for="p in projects" :key="p.project_id" :value="p.project_id">
              {{ p.name }}
            </option>
          </select>
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">文档状态</label>
          <select
            v-model="filterStatus"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部状态</option>
            <option value="active">活跃</option>
            <option value="archived">已归档</option>
            <option value="deleted">已删除</option>
          </select>
        </div>

        <button class="btn btn-ghost btn-sm" @click="clearFilters">清除</button>

        <button
          class="btn btn-sm bg-brand-50 text-brand-700 border border-brand-200 hover:bg-brand-100 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors"
          @click="handleRefreshAll"
        >
          🔄 刷新过期索引
        </button>

        <span class="text-xs text-surface-400 ml-auto self-end pb-1">
          {{ data?.page_info?.total_items ?? 0 }} 份文档
        </span>
      </div>
    </div>

    <!-- Error -->
    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载文档列表失败: {{ (error as Error)?.message }}
    </div>

    <!-- Table -->
    <DataTable
      :items="data?.items ?? []"
      :columns="columns"
      :loading="isLoading"
      empty-message="暂无知识文档 — 请先通过资产管理上传资产"
      clickable
      row-key="document_id"
      @row-click="openDetail"
    >
      <template #cell-title="{ item }">
        <div class="max-w-xs">
          <div class="flex items-center gap-2">
            <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4 text-surface-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
            <span class="text-sm font-medium text-surface-800 truncate">
              {{ (item as KnowledgeDocument).title }}
            </span>
          </div>
          <div class="ml-6 text-2xs text-surface-400 font-mono truncate">
            {{ (item as KnowledgeDocument).document_id.slice(0, 16) }}…
          </div>
        </div>
      </template>

      <template #cell-document_status="{ value }">
        <StatusBadge :status="value as string" />
      </template>

      <template #cell-current_version="{ value }">
        <span class="font-mono text-xs">v{{ value }}</span>
      </template>

      <template #cell-sensitivity_level="{ value }">
        <span :class="['badge ring-1 ring-inset text-2xs', sensitivityColor(value as string)]">
          {{ value }}
        </span>
      </template>

      <template #cell-updated_at="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
      </template>
    </DataTable>

    <Pagination
      v-if="data?.page_info"
      :page-info="data.page_info"
      @page-change="(p: number) => (page = p)"
    />

    <!-- ── Index Detail Drawer ── -->
    <DetailDrawer
      :open="drawerOpen"
      :title="`索引详情 · ${selectedDoc?.title ?? ''}`"
      width="w-[480px] max-w-full"
      @close="closeDrawer"
    >
      <!-- Loading -->
      <div v-if="indexLoading" class="py-4">
        <LoadingSkeleton variant="detail" />
      </div>

      <template v-else-if="indexState">
        <!-- Index state grid -->
        <div class="mb-6">
          <h3 class="text-2xs font-semibold uppercase text-surface-400 mb-3">索引状态</h3>
          <div class="grid grid-cols-2 gap-3">
            <div
              v-for="idx in [
                { key: 'fts', label: 'FTS 全文检索', icon: '🔍', state: indexState.fts_state },
                { key: 'vector', label: 'Vector 向量', icon: '📐', state: indexState.vector_state },
                { key: 'graph', label: 'Graph 图谱', icon: '🕸️', state: indexState.graph_state },
                { key: 'citation', label: 'Citation 溯源', icon: '📎', state: indexState.citation_state },
              ]"
              :key="idx.key"
              class="rounded-lg border border-surface-200 p-3 hover:border-surface-300 transition-colors"
            >
              <div class="flex items-center justify-between mb-2">
                <div class="flex items-center gap-1.5">
                  <span class="text-sm">{{ idx.icon }}</span>
                  <span class="text-xs font-medium text-surface-700">{{ idx.label }}</span>
                </div>
              </div>
              <span
                :class="[
                  'badge ring-1 ring-inset text-2xs font-medium',
                  indexStateBadgeClass(idx.state),
                ]"
              >
                {{ INDEX_STATE_LABELS[idx.state] || idx.state }}
              </span>
            </div>
          </div>
        </div>

        <!-- Version info -->
        <div class="mb-6">
          <h3 class="text-2xs font-semibold uppercase text-surface-400 mb-3">版本信息</h3>
          <dl class="space-y-3">
            <div class="flex items-center justify-between rounded-lg border border-surface-200 px-4 py-2.5">
              <dt class="text-xs text-surface-500">就绪版本 (Ready)</dt>
              <dd class="font-mono text-sm font-medium text-surface-700">v{{ indexState.ready_version }}</dd>
            </div>
            <div class="flex items-center justify-between rounded-lg border border-surface-200 px-4 py-2.5">
              <dt class="text-xs text-surface-500">待刷新版本 (Stale)</dt>
              <dd class="font-mono text-sm font-medium text-surface-700">v{{ indexState.stale_version }}</dd>
            </div>
            <div
              v-if="indexState.ready_version !== indexState.stale_version"
              class="rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 text-xs text-amber-700"
            >
              ⚠ 索引版本不一致，可能需要刷新
            </div>
          </dl>
        </div>

        <!-- Refresh info -->
        <div>
          <h3 class="text-2xs font-semibold uppercase text-surface-400 mb-3">刷新信息</h3>
          <dl class="space-y-3">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">最后刷新时间</dt>
              <dd class="mt-0.5 text-sm">
                {{ indexState.last_refreshed_at ? formatTime(indexState.last_refreshed_at) : '—' }}
              </dd>
            </div>
            <div v-if="indexState.last_error">
              <dt class="text-2xs font-semibold uppercase text-surface-400">最后错误</dt>
              <dd class="mt-0.5 rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 font-mono break-all">
                {{ indexState.last_error }}
              </dd>
            </div>
            <div class="grid grid-cols-2 gap-3">
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt>
                <dd class="mt-0.5 text-xs text-surface-600">{{ formatTime(indexState.created_at) }}</dd>
              </div>
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">更新时间</dt>
                <dd class="mt-0.5 text-xs text-surface-600">{{ formatTime(indexState.updated_at) }}</dd>
              </div>
            </div>
          </dl>
        </div>

        <!-- Internal IDs -->
        <div class="mt-6 pt-4 border-t border-surface-200">
          <dl class="space-y-1">
            <div class="flex items-center gap-2">
              <dt class="text-2xs text-surface-400">Index State ID:</dt>
              <dd class="font-mono text-2xs text-surface-500">{{ indexState.index_state_id }}</dd>
            </div>
            <div class="flex items-center gap-2">
              <dt class="text-2xs text-surface-400">Object ID:</dt>
              <dd class="font-mono text-2xs text-surface-500">{{ indexState.object_id }}</dd>
            </div>
            <div class="flex items-center gap-2">
              <dt class="text-2xs text-surface-400">Object Type:</dt>
              <dd class="font-mono text-2xs text-surface-500">{{ indexState.object_type }}</dd>
            </div>
          </dl>
        </div>
      </template>

      <!-- Empty -->
      <div v-else class="flex flex-col items-center justify-center py-12 text-sm text-surface-400">
        加载索引状态失败
      </div>

      <!-- Footer -->
      <template #footer>
        <div class="flex items-center justify-end">
          <button class="btn btn-secondary btn-sm" @click="closeDrawer">关闭</button>
        </div>
      </template>
    </DetailDrawer>
  </div>
</template>