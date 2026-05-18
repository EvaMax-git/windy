<script setup lang="ts">
import { ref, computed, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import {
  fetchKnowledgeDocuments,
  fetchKnowledgeDocument,
  fetchDocumentBlocks,
  fetchDocumentChunks,
  fetchDocumentIndexState,
  archiveKnowledgeDocument,
  rechunkDocument,
  refreshKnowledgeIndexes,
  fetchSubLibraries,
  updateKnowledgeDocument,
  fetchProjects,
} from "@/api/client";
import type {
  KnowledgeDocument,
  KnowledgeBlock,
  KnowledgeChunk,
  IndexState,
  SubLibraryRead,
  ProjectRead,
} from "@/types";

const route = useRoute();
const router = useRouter();

const page = ref(1);
const pageSize = ref(50);
const filterProjectId = ref((route.query.project_id as string) || "");
const filterStatus = ref("");
const filterSubLibraryId = ref((route.query.sub_library_id as string) || "");

// Sync sub-library filter from URL
watch(() => route.query.sub_library_id, (val) => {
  if (val && typeof val === "string") {
    filterSubLibraryId.value = val;
    page.value = 1;
  }
});

// Fetch sub-libraries for filter dropdown
const { data: subLibsData } = useQuery({
  queryKey: ["sub-libraries-list"],
  queryFn: () => fetchSubLibraries({ page: 1, page_size: 100 }),
  staleTime: 60000,
});
const subLibraries = computed(() => subLibsData.value?.items ?? []);

// Fetch projects for filter dropdown
const { data: projectsData } = useQuery({
  queryKey: ["projects-list"],
  queryFn: () => fetchProjects({ page: 1, page_size: 200 }),
  staleTime: 60000,
});
const projects = computed(() => projectsData.value?.items ?? []);

const queryClient = useQueryClient();

const selectedDoc = ref<KnowledgeDocument | null>(null);
const drawerTab = ref<"info" | "blocks" | "chunks" | "index">("info");
const drawerOpen = ref(false);
const showAdvanced = ref(false);
const showAdvancedOps = ref(false);

// Auto-switch back to info when advanced section is collapsed
watch(showAdvanced, (val) => {
  if (!val && ["blocks", "chunks", "index"].includes(drawerTab.value)) {
    drawerTab.value = "info";
  }
});

// ── Document list query ──
const listKey = computed(() => [
  "knowledge-documents",
  page.value,
  pageSize.value,
  filterProjectId.value.trim() || "__none__",
  filterStatus.value,
  filterSubLibraryId.value || "__none__",
] as const);

const { data, isLoading, isError, error } = useQuery({
  queryKey: listKey,
  queryFn: () =>
    fetchKnowledgeDocuments({
      page: page.value,
      page_size: pageSize.value,
      project_id: filterProjectId.value.trim() || undefined,
      status: filterStatus.value || undefined,
      sub_library_id: filterSubLibraryId.value || undefined,
    }),
  placeholderData: (prev) => prev,
  retry: 3,
});

// ── Detail queries (only when drawer is open) ──
const docId = computed(() => selectedDoc.value?.document_id ?? null);

const { data: docDetail } = useQuery({
  queryKey: ["knowledge-document", docId],
  queryFn: () => fetchKnowledgeDocument(docId.value!),
  enabled: computed(() => drawerOpen.value && !!docId.value),
});

const { data: blocks } = useQuery({
  queryKey: ["knowledge-blocks", docId],
  queryFn: () => fetchDocumentBlocks(docId.value!),
  enabled: computed(() => drawerOpen.value && !!docId.value),
});

const { data: chunks } = useQuery({
  queryKey: ["knowledge-chunks", docId],
  queryFn: () => fetchDocumentChunks(docId.value!),
  enabled: computed(() => drawerOpen.value && !!docId.value && drawerTab.value === "chunks"),
});

const { data: indexState } = useQuery({
  queryKey: ["knowledge-index-state", docId],
  queryFn: () => fetchDocumentIndexState(docId.value!),
  enabled: computed(() => drawerOpen.value && !!docId.value && drawerTab.value === "index"),
});

const columns = [
  { key: "title", label: "文档标题" },
  { key: "document_status", label: "状态", width: "80px" },
  { key: "sensitivity_level", label: "敏感度", width: "90px" },
  { key: "current_version", label: "版本", width: "60px" },
  { key: "updated_at", label: "更新时间", width: "160px" },
];

function openDetail(doc: KnowledgeDocument) {
  selectedDoc.value = doc;
  drawerTab.value = "info";
  showAdvanced.value = false;
  showAdvancedOps.value = false;
  drawerOpen.value = true;
}

function closeDrawer() {
  drawerOpen.value = false;
  selectedDoc.value = null;
  drawerTab.value = "info";
  showAdvanced.value = false;
  showAdvancedOps.value = false;
}

function clearFilters() {
  filterProjectId.value = "";
  filterStatus.value = "";
  page.value = 1;
}

// ── Document actions ──
const actionLoading = ref(false);
const actionToast = ref<{ msg: string; type: "success" | "error" } | null>(null);
const assigningSubLib = ref(false);

function showActionToast(msg: string, type: "success" | "error") {
  actionToast.value = { msg, type };
  setTimeout(() => { actionToast.value = null; }, 4000);
}

async function handleArchive(doc: KnowledgeDocument) {
  if (!confirm(`确认归档文档「${doc.title}」？归档后文档不参与搜索。`)) return;
  actionLoading.value = true;
  try {
    await archiveKnowledgeDocument(doc.document_id);
    showActionToast("文档已归档", "success");
    queryClient.invalidateQueries({ queryKey: ["knowledge-documents"] });
    queryClient.invalidateQueries({ queryKey: ["index-documents"] });
    drawerOpen.value = false;
  } catch (e: unknown) {
    showActionToast((e as Error)?.message || "归档失败", "error");
  } finally { actionLoading.value = false; }
}

async function handleRechunk(doc: KnowledgeDocument) {
  actionLoading.value = true;
  try {
    const result = await rechunkDocument(doc.document_id);
    showActionToast(`重分块完成: ${result.length} 个块`, "success");
    queryClient.invalidateQueries({ queryKey: ["knowledge-chunks", doc.document_id] });
    queryClient.invalidateQueries({ queryKey: ["knowledge-index-state", doc.document_id] });
  } catch (e: unknown) {
    showActionToast((e as Error)?.message || "重分块失败", "error");
  } finally { actionLoading.value = false; }
}

async function handleRefreshIndex(doc: KnowledgeDocument) {
  actionLoading.value = true;
  try {
    await refreshKnowledgeIndexes();
    showActionToast("索引刷新已触发", "success");
    queryClient.invalidateQueries({ queryKey: ["knowledge-index-state", doc.document_id] });
  } catch (e: unknown) {
    showActionToast((e as Error)?.message || "刷新失败", "error");
  } finally { actionLoading.value = false; }
}

async function handleAssignSubLibrary(doc: KnowledgeDocument, subLibId: string | null) {
  assigningSubLib.value = true;
  try {
    await updateKnowledgeDocument(doc.document_id, {
      sub_library_id: subLibId || undefined,
    });
    showActionToast(subLibId ? "已关联到子库" : "已取消子库关联", "success");
    queryClient.invalidateQueries({ queryKey: ["knowledge-document", doc.document_id] });
    queryClient.invalidateQueries({ queryKey: ["knowledge-documents"] });
    queryClient.invalidateQueries({ queryKey: ["index-documents"] });
  } catch (e: unknown) {
    showActionToast((e as Error)?.message || "操作失败", "error");
  } finally { assigningSubLib.value = false; }
}

function formatTime(iso: string): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}

const BLOCK_TYPE_LABELS: Record<string, string> = {
  title: "标题",
  paragraph: "段落",
  list: "列表",
  table: "表格",
  quote: "引用",
  code: "代码",
  image_caption: "图片标注",
  metadata: "元数据",
};

function blockTypeLabel(type: string): string {
  return BLOCK_TYPE_LABELS[type] ?? type;
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
</script>

<template>
  <div>
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
          <label class="text-2xs font-semibold uppercase text-surface-400">子库</label>
          <select
            v-model="filterSubLibraryId"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部子库</option>
            <option v-for="lib in subLibraries" :key="lib.id" :value="lib.id">
              {{ lib.name }}
            </option>
          </select>
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">状态</label>
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

    <!-- Document Table -->
    <DataTable
      :items="data?.items ?? []"
      :columns="columns"
      :loading="isLoading"
      :empty-message="isError ? '数据加载出错' : '暂无知识文档 — 请先通过 Asset 导入创建文档'"
      clickable
      row-key="document_id"
      @row-click="openDetail"
    >
      <template #cell-title="{ item }">
        <div class="max-w-xs">
          <div class="text-sm font-medium text-surface-800 truncate">
            {{ (item as KnowledgeDocument).title }}
          </div>
          <div
            v-if="(item as KnowledgeDocument).summary"
            class="text-2xs text-surface-400 truncate mt-0.5"
          >
            {{ (item as KnowledgeDocument).summary }}
          </div>
        </div>
      </template>

      <template #cell-document_status="{ value }">
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
      v-if="data?.page_info"
      :page-info="data.page_info"
      @page-change="(p: number) => (page = p)"
    />

    <!-- ── Detail Drawer ── -->
    <DetailDrawer
      :open="drawerOpen"
      :title="selectedDoc?.title ?? '文档详情'"
      width="w-[560px] max-w-full"
      @close="closeDrawer"
    >
      <!-- Tab bar -->
      <div class="flex border-b border-surface-200 mb-4 -mx-5 px-5">
        <button
          :class="[
            'pb-2.5 px-3 text-sm font-medium border-b-2 transition-colors',
            drawerTab === 'info'
              ? 'border-brand-500 text-brand-600'
              : 'border-transparent text-surface-400 hover:text-surface-600',
          ]"
          @click="drawerTab = 'info'"
        >
          概览
        </button>
        <button
          :class="[
            'pb-2.5 px-3 text-sm font-medium border-b-2 transition-colors',
            showAdvanced
              ? 'border-brand-500 text-brand-600'
              : 'border-transparent text-surface-400 hover:text-surface-600',
          ]"
          @click="showAdvanced = !showAdvanced"
        >
          高级信息 {{ showAdvanced ? '▾' : '▸' }}
        </button>
      </div>

      <!-- Advanced info sub-tabs -->
      <div v-if="showAdvanced" class="flex border-b border-surface-200 mb-4 -mx-5 px-5">
        <button
          v-for="tab in [
            { key: 'blocks', label: `块 (${blocks?.length ?? '...'})` },
            { key: 'chunks', label: '分块' },
            { key: 'index', label: '索引状态' },
          ]"
          :key="tab.key"
          @click="drawerTab = tab.key as 'blocks' | 'chunks' | 'index'"
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

      <!-- Action buttons -->
      <div class="flex items-center gap-2 mb-4 flex-wrap">
        <button
          class="btn btn-sm bg-red-50 text-red-700 border border-red-200 hover:bg-red-100 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors"
          :disabled="actionLoading"
          data-testid="document-archive"
          @click="handleArchive(selectedDoc!)"
        >
          📦 归档
        </button>

        <!-- Advanced operations dropdown -->
        <div class="relative">
          <button
            class="btn btn-sm bg-surface-100 text-surface-600 border border-surface-200 hover:bg-surface-200 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors"
            @click="showAdvancedOps = !showAdvancedOps"
          >
            ⚙️ 高级操作 {{ showAdvancedOps ? '▴' : '▾' }}
          </button>
          <div
            v-if="showAdvancedOps"
            class="absolute left-0 top-full mt-1 bg-white rounded-lg shadow-lg border border-surface-200 py-1 z-20 min-w-[140px]"
          >
            <button
              class="w-full text-left px-3 py-2 text-xs text-surface-700 hover:bg-amber-50 hover:text-amber-700 transition-colors"
              :disabled="actionLoading"
              data-testid="document-rechunk"
              @click="handleRechunk(selectedDoc!); showAdvancedOps = false"
            >
              🔄 重分块
            </button>
            <button
              class="w-full text-left px-3 py-2 text-xs text-surface-700 hover:bg-blue-50 hover:text-blue-700 transition-colors"
              :disabled="actionLoading"
              data-testid="document-refresh-index"
              @click="handleRefreshIndex(selectedDoc!); showAdvancedOps = false"
            >
              🔍 刷新索引
            </button>
          </div>
        </div>
      </div>

      <!-- Action toast -->
      <div
        v-if="actionToast"
        :class="[
          'mb-4 px-3 py-2 rounded-lg text-xs font-medium',
          actionToast.type === 'success' ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' : 'bg-red-50 text-red-700 border border-red-200',
        ]"
      >
        {{ actionToast.msg }}
      </div>

      <!-- Tab: Info -->
      <div v-if="drawerTab === 'info' && docDetail" class="space-y-4">
        <dl class="space-y-3">
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">文档ID</dt>
            <dd class="mt-0.5 font-mono text-xs text-surface-700 break-all">{{ docDetail.document_id }}</dd>
          </div>
          <div v-if="docDetail.canonical_uri">
            <dt class="text-2xs font-semibold uppercase text-surface-400">URI</dt>
            <dd class="mt-0.5 font-mono text-xs text-brand-600">{{ docDetail.canonical_uri }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">项目ID</dt>
            <dd class="mt-0.5 font-mono text-xs text-surface-600">{{ docDetail.project_id || "—" }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">关联子库</dt>
            <dd class="mt-0.5 flex items-center gap-2">
              <select
                class="rounded border border-surface-200 bg-white px-2 py-1 text-xs text-surface-700 focus:border-brand-500 focus:outline-none"
                :value="docDetail.sub_library_id || ''"
                :disabled="assigningSubLib"
                @change="handleAssignSubLibrary(docDetail!, ($event.target as HTMLSelectElement).value || null)"
              >
                <option value="">未关联</option>
                <option v-for="lib in subLibraries" :key="lib.id" :value="lib.id">
                  {{ lib.name }}
                </option>
              </select>
              <span v-if="assigningSubLib" class="text-2xs text-surface-400">保存中...</span>
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">状态</dt>
            <dd class="mt-0.5">
              <StatusBadge :status="docDetail.document_status" />
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">当前版本</dt>
            <dd class="mt-0.5 font-mono text-sm">v{{ docDetail.current_version }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">敏感等级</dt>
            <dd class="mt-0.5">
              <span :class="['badge ring-1 ring-inset', sensitivityColor(docDetail.sensitivity_level)]">
                {{ docDetail.sensitivity_level }}
              </span>
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">创建者</dt>
            <dd class="mt-0.5 font-mono text-xs text-surface-600">{{ docDetail.created_by_user_id || "—" }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt>
            <dd class="mt-0.5 text-sm">{{ formatTime(docDetail.created_at) }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">更新时间</dt>
            <dd class="mt-0.5 text-sm">{{ formatTime(docDetail.updated_at) }}</dd>
          </div>
          <div v-if="docDetail.summary">
            <dt class="text-2xs font-semibold uppercase text-surface-400">摘要</dt>
            <dd class="mt-1 text-sm text-surface-700 leading-relaxed">{{ docDetail.summary }}</dd>
          </div>
        </dl>
      </div>

      <!-- Tab: Blocks -->
      <div v-if="drawerTab === 'blocks'">
        <div v-if="!blocks || blocks.length === 0" class="py-8 text-center text-sm text-surface-400">
          <div class="flex flex-col items-center gap-2">
            <svg xmlns="http://www.w3.org/2000/svg" class="h-8 w-8 text-surface-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1">
              <path stroke-linecap="round" stroke-linejoin="round" d="M4 7v10c0 2 1 3 3 3h10c2 0 3-1 3-3V7M4 7c0-2 1-3 3-3h10c2 0 3 1 3 3M4 7h16" />
            </svg>
            <span>暂无内容块</span>
          </div>
        </div>
        <div v-else class="space-y-3">
          <div
            v-for="block in blocks"
            :key="block.block_id"
            class="rounded-lg border border-surface-200 p-3 hover:border-surface-300 transition-colors"
          >
            <div class="flex items-center justify-between mb-2 flex-wrap gap-1">
              <div class="flex items-center gap-2">
                <span class="text-2xs font-semibold uppercase text-surface-400">
                  #{{ block.block_order }}
                </span>
                <span class="text-2xs font-medium px-1.5 py-0.5 rounded bg-surface-100 text-surface-600">
                  {{ blockTypeLabel(block.block_type) }}
                </span>
                <span class="font-mono text-2xs text-surface-400">{{ block.block_key }}</span>
              </div>
              <span v-if="block.token_count" class="text-2xs text-surface-400">
                {{ block.token_count }} tokens
              </span>
            </div>
            <div class="text-xs text-surface-700 whitespace-pre-wrap leading-relaxed max-h-48 overflow-y-auto font-mono bg-surface-50 rounded p-2 border border-surface-100">
              {{ block.content_markdown.slice(0, 600) }}{{ block.content_markdown.length > 600 ? '…' : '' }}
            </div>
          </div>
        </div>
      </div>

      <!-- Tab: Chunks -->
      <div v-if="drawerTab === 'chunks'">
        <div v-if="!chunks || chunks.length === 0" class="py-8 text-center text-sm text-surface-400">
          <div class="flex flex-col items-center gap-2">
            <svg xmlns="http://www.w3.org/2000/svg" class="h-8 w-8 text-surface-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1">
              <path stroke-linecap="round" stroke-linejoin="round" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
            </svg>
            <span>暂无分块 — 请对文档执行 rechunk</span>
          </div>
        </div>
        <div v-else class="space-y-3">
          <div
            v-for="chunk in chunks"
            :key="chunk.chunk_id"
            class="rounded-lg border border-surface-200 p-3"
          >
            <div class="flex items-center justify-between mb-2 flex-wrap gap-1">
              <div class="flex items-center gap-2">
                <span class="text-2xs font-semibold uppercase text-surface-400">
                  Chunk #{{ chunk.chunk_order }}
                </span>
                <span class="font-mono text-2xs text-surface-400">
                  v{{ chunk.document_version }}
                </span>
              </div>
              <span v-if="chunk.token_count" class="text-2xs text-surface-400">
                {{ chunk.token_count }} tokens
              </span>
            </div>
            <div class="text-xs text-surface-700 leading-relaxed max-h-32 overflow-y-auto bg-surface-50 rounded p-2 border border-surface-100">
              {{ chunk.chunk_text.slice(0, 400) }}{{ chunk.chunk_text.length > 400 ? '…' : '' }}
            </div>
            <div v-if="chunk.block_id" class="mt-1 text-2xs text-surface-400 font-mono">
              ← block: {{ chunk.block_id.slice(0, 12) }}…
            </div>
          </div>
        </div>
      </div>

      <!-- Tab: Index State -->
      <div v-if="drawerTab === 'index' && indexState">
        <div class="grid grid-cols-2 gap-3">
          <div class="rounded-lg border border-surface-200 p-3">
            <div class="text-2xs font-semibold uppercase text-surface-400 mb-1">FTS</div>
            <StatusBadge :status="indexState.fts_state" />
          </div>
          <div class="rounded-lg border border-surface-200 p-3">
            <div class="text-2xs font-semibold uppercase text-surface-400 mb-1">Vector</div>
            <StatusBadge :status="indexState.vector_state" />
          </div>
          <div class="rounded-lg border border-surface-200 p-3">
            <div class="text-2xs font-semibold uppercase text-surface-400 mb-1">Graph</div>
            <StatusBadge :status="indexState.graph_state" />
          </div>
          <div class="rounded-lg border border-surface-200 p-3">
            <div class="text-2xs font-semibold uppercase text-surface-400 mb-1">Citation</div>
            <StatusBadge :status="indexState.citation_state" />
          </div>
        </div>
        <dl class="mt-4 space-y-2">
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">就绪版本</dt>
            <dd class="text-sm font-mono">v{{ indexState.ready_version }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">待刷新版本</dt>
            <dd class="text-sm font-mono">v{{ indexState.stale_version }}</dd>
          </div>
          <div v-if="indexState.last_refreshed_at">
            <dt class="text-2xs font-semibold uppercase text-surface-400">最后刷新</dt>
            <dd class="text-sm">{{ formatTime(indexState.last_refreshed_at) }}</dd>
          </div>
          <div v-if="indexState.last_error">
            <dt class="text-2xs font-semibold uppercase text-surface-400">最后错误</dt>
            <dd class="text-sm text-red-600">{{ indexState.last_error }}</dd>
          </div>
        </dl>
      </div>
    </DetailDrawer>
  </div>
</template>