<script setup lang="ts">
import { ref, computed, watch, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useQuery } from "@tanstack/vue-query";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import {
  searchKnowledge,
  fetchCitation,
  fetchProjects,
} from "@/api/client";
import type {
  KnowledgeFtsSearchResult,
  Citation,
} from "@/types";

const route = useRoute();
const router = useRouter();

const searchQuery = ref((route.query.q as string) || "");
const submittedQuery = ref("");
const filterProjectId = ref((route.query.project_id as string) || "");
const filterSensitivity = ref("");

// Project list for filter dropdown
const { data: projectsData } = useQuery({
  queryKey: ["projects-list"],
  queryFn: () => fetchProjects({ page: 1, page_size: 200 }),
  staleTime: 60_000,
});
const projects = computed(() => projectsData.value?.items ?? []);

// If query params pre-filled (from "查看结果" in AssetTab), auto-search
onMounted(() => {
  if (searchQuery.value.trim()) {
    doSearch();
  }
});
const page = ref(1);
const pageSize = ref(20);

const selectedResult = ref<KnowledgeFtsSearchResult | null>(null);
const drawerOpen = ref(false);

// ── Search query ──
const searchKey = computed(() => [
  "knowledge-search",
  submittedQuery.value,
  filterProjectId.value,
  filterSensitivity.value,
  page.value,
  pageSize.value,
] as const);

const { data, isLoading, isError, error } = useQuery({
  queryKey: searchKey,
  queryFn: () =>
    searchKnowledge({
      q: submittedQuery.value,
      project_id: filterProjectId.value || undefined,
      sensitivity_floor: filterSensitivity.value || undefined,
      page: page.value,
      page_size: pageSize.value,
    }),
  enabled: computed(() => submittedQuery.value.length > 0),
  placeholderData: (prev) => prev,
});

// ── Citation query (when drawer opens on a result) ──
const citationChunkId = computed(() => selectedResult.value?.chunk_id ?? null);

const { data: citation } = useQuery({
  queryKey: ["knowledge-citation", citationChunkId],
  queryFn: () => fetchCitation(citationChunkId.value!),
  enabled: computed(() => drawerOpen.value && !!citationChunkId.value),
});

function doSearch() {
  submittedQuery.value = searchQuery.value.trim();
  page.value = 1;
}

function openCitation(result: KnowledgeFtsSearchResult) {
  selectedResult.value = result;
  drawerOpen.value = true;
}

function closeDrawer() {
  drawerOpen.value = false;
  selectedResult.value = null;
}

function viewDocumentInKnowledge(documentId: string) {
  router.push({
    path: "/app/knowledge",
    query: { tab: "knowledge" },
  });
  // Note: We'd ideally pass the document ID, but KnowledgeTab doesn't support
  // that yet. User can find the doc by title in the list.
}

function clearFilters() {
  searchQuery.value = "";
  submittedQuery.value = "";
  filterProjectId.value = "";
  filterSensitivity.value = "";
  page.value = 1;
  router.replace({ query: {} });
}

// Watch page changes to re-trigger if query is active
watch(page, () => {
  if (submittedQuery.value) {
    // Vue Query will auto-refetch due to page being in the query key
  }
});

function formatTime(iso: string): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}

function highlightMatch(text: string, query: string): string {
  if (!query || !text) return text;
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const regex = new RegExp(`(${escaped})`, 'gi');
  return text.replace(regex, '<mark class="bg-yellow-200 text-yellow-900 rounded px-0.5">$1</mark>');
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

// ── Citation chain node type labels ──
function nodeTypeLabel(type: string): string {
  const labels: Record<string, string> = {
    asset: "资产",
    document: "文档",
    block: "块",
    chunk: "分块",
  };
  return labels[type] ?? type;
}
</script>

<template>
  <div>
    <!-- Search bar -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-end gap-3">
        <div class="flex flex-col gap-1 flex-1 min-w-[240px]">
          <label class="text-2xs font-semibold uppercase text-surface-400">搜索</label>
          <div class="flex gap-2">
            <input
              v-model="searchQuery"
              type="text"
              placeholder="输入关键词搜索知识库…"
              class="flex-1 rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              @keyup.enter="doSearch"
            />
            <button class="btn btn-primary btn-sm" @click="doSearch" :disabled="!searchQuery.trim()">
              <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
              </svg>
              搜索
            </button>
          </div>
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">项目</label>
          <select
            v-model="filterProjectId"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部项目</option>
            <option v-for="p in projects" :key="p.project_id" :value="p.project_id">
              {{ p.name }}
            </option>
          </select>
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">最低敏感度</label>
          <select
            v-model="filterSensitivity"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">不限</option>
            <option value="public">公开及以上</option>
            <option value="normal">普通及以上</option>
            <option value="private">内部及以上</option>
            <option value="sensitive">敏感及以上</option>
            <option value="secret">机密</option>
          </select>
        </div>

        <button class="btn btn-ghost btn-sm" @click="clearFilters">清除</button>
      </div>
      <p v-if="submittedQuery" class="mt-3 text-xs text-surface-400">
        搜索 "{{ submittedQuery }}" —
        {{ data?.page_info?.total_items ?? 0 }} 条结果
      </p>
    </div>

    <!-- Error -->
    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      搜索失败: {{ (error as Error)?.message }}
    </div>

    <!-- Empty state (no search yet) -->
    <div
      v-if="!submittedQuery && !isLoading"
      class="card border-dashed border-surface-200 bg-white p-12 text-center"
    >
      <div class="flex flex-col items-center gap-3">
        <svg xmlns="http://www.w3.org/2000/svg" class="h-12 w-12 text-surface-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1">
          <path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
        </svg>
        <p class="text-sm text-surface-400">
          输入关键词并点击搜索，在知识库中查找内容
        </p>
        <p class="text-xs text-surface-300">
          支持中文和英文关键词，搜索结果会显示文档溯源信息
        </p>
      </div>
    </div>

    <!-- Loading skeleton -->
    <div v-if="isLoading && submittedQuery">
      <LoadingSkeleton variant="search-results" :rows="5" />
    </div>

    <!-- Results -->
    <div v-if="submittedQuery && !isLoading && data?.items" class="space-y-3">
      <!-- No results -->
      <div
        v-if="data.items.length === 0"
        class="card border-dashed border-surface-200 bg-white p-12 text-center"
      >
        <div class="flex flex-col items-center gap-2">
          <svg xmlns="http://www.w3.org/2000/svg" class="h-12 w-12 text-surface-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1">
            <path stroke-linecap="round" stroke-linejoin="round" d="M9.172 16.172a4 4 0 015.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <p class="text-sm text-surface-400">未找到匹配结果</p>
          <p class="text-xs text-surface-300">尝试更换关键词或调整过滤条件</p>
        </div>
      </div>

      <!-- Result items -->
      <div
        v-for="result in data.items"
        :key="result.chunk_id"
        class="card bg-white p-4 hover:shadow-md transition-shadow cursor-pointer"
        @click="openCitation(result)"
      >
        <!-- Header -->
        <div class="flex items-start justify-between mb-2 gap-2">
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="text-sm font-semibold text-surface-800 truncate">
                {{ result.document_title }}
              </span>
              <span
                :class="['text-2xs badge ring-1 ring-inset', sensitivityColor(result.document_sensitivity)]"
              >
                {{ result.document_sensitivity }}
              </span>
              <span
                v-if="result.is_stale"
                class="text-2xs badge bg-amber-50 text-amber-700 ring-amber-600/20 ring-1 ring-inset"
                title="索引已过期"
              >
                ⚠ 过期
              </span>
            </div>
            <div class="flex items-center gap-2 mt-0.5 text-2xs text-surface-400 font-mono">
              <span v-if="result.block_key">{{ result.block_key }}</span>
              <span v-if="result.block_type">· {{ result.block_type }}</span>
              <span>· chunk #{{ result.chunk_order }}</span>
            </div>
          </div>
          <div class="flex items-center gap-2">
            <button
              class="btn btn-ghost btn-xs text-2xs text-brand-600 shrink-0"
              title="查看文档详情"
              @click.stop="viewDocumentInKnowledge(result.document_id)"
            >
              📄 查看文档
            </button>
            <div class="text-2xs text-surface-400 font-mono whitespace-nowrap">
              得分 {{ result.rank.toFixed(2) }}
            </div>
          </div>
        </div>

        <!-- Chunk text with highlight -->
        <div
          class="text-sm text-surface-700 leading-relaxed mt-2 bg-surface-50 rounded-lg p-3 border border-surface-100"
          v-html="highlightMatch(result.chunk_text.slice(0, 500), submittedQuery)"
        ></div>

        <!-- Stale warning -->
        <div
          v-if="result.is_stale && result.stale_reason"
          class="mt-2 text-xs text-amber-600 flex items-center gap-1"
        >
          <svg xmlns="http://www.w3.org/2000/svg" class="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L3.34 15.5c-.77.833.192 2.5 1.732 2.5z" />
          </svg>
          {{ result.stale_reason }}
        </div>
      </div>

      <Pagination
        v-if="data?.page_info"
        :page-info="data.page_info"
        @page-change="(p: number) => (page = p)"
      />
    </div>

    <!-- ── Citation Detail Drawer ── -->
    <DetailDrawer
      :open="drawerOpen"
      title="溯源信息"
      width="w-[520px] max-w-full"
      @close="closeDrawer"
    >
      <template v-if="selectedResult">
        <!-- Selected chunk info -->
        <div class="mb-5 p-3 rounded-lg bg-brand-50 border border-brand-100">
          <div class="flex items-center gap-2 mb-1">
            <span class="text-2xs font-semibold uppercase text-brand-500">选中分块</span>
            <span class="text-2xs text-brand-400 font-mono">#{{ selectedResult.chunk_order }}</span>
          </div>
          <p class="text-sm text-surface-700 leading-relaxed">
            {{ selectedResult.chunk_text.slice(0, 300) }}{{ selectedResult.chunk_text.length > 300 ? '…' : '' }}
          </p>
          <div class="mt-1 text-2xs text-surface-400 font-mono">
            chunk_id: {{ selectedResult.chunk_id }}
          </div>
        </div>

        <!-- Citation chain -->
        <div v-if="citation">
          <!-- Staleness warning -->
          <div
            v-if="citation.is_stale"
            class="mb-4 p-3 rounded-lg bg-amber-50 border border-amber-200 text-sm text-amber-700"
          >
            <div class="flex items-center gap-2 font-medium">
              <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L3.34 15.5c-.77.833.192 2.5 1.732 2.5z" />
              </svg>
              索引可能已过期
            </div>
            <p v-if="citation.stale_reason" class="mt-1 text-xs">{{ citation.stale_reason }}</p>
          </div>

          <!-- Chain nodes -->
          <div class="space-y-0">
            <div
              v-for="(node, idx) in citation.chain"
              :key="node.id"
              class="relative flex items-start gap-3 pb-4"
            >
              <!-- Connector line -->
              <div
                v-if="idx < citation.chain.length - 1"
                class="absolute left-[11px] top-8 bottom-0 w-0.5 bg-surface-200"
              ></div>
              <!-- Node dot -->
              <div
                :class="[
                  'relative z-10 mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-2xs font-bold',
                  idx === 0
                    ? 'bg-brand-500 text-white'
                    : 'bg-surface-100 text-surface-500 border border-surface-200',
                ]"
              >
                {{ idx + 1 }}
              </div>
              <!-- Node info -->
              <div class="flex-1 min-w-0 pt-0.5">
                <div class="flex items-center gap-2">
                  <span class="text-2xs font-semibold uppercase text-surface-400">
                    {{ nodeTypeLabel(node.type) }}
                  </span>
                  <span class="font-mono text-2xs text-surface-400 truncate">
                    {{ node.id.slice(0, 16) }}…
                  </span>
                </div>
                <p class="text-sm text-surface-700 mt-0.5 truncate">{{ node.label }}</p>
                <p v-if="node.uri" class="text-2xs text-brand-500 font-mono truncate mt-0.5">{{ node.uri }}</p>
              </div>
            </div>
          </div>

          <!-- Document meta -->
          <div v-if="citation.document_title" class="mt-4 pt-4 border-t border-surface-200">
            <dl class="space-y-1.5">
              <div v-if="citation.document_title">
                <dt class="text-2xs font-semibold uppercase text-surface-400">文档</dt>
                <dd class="text-sm">{{ citation.document_title }}</dd>
              </div>
              <div v-if="citation.document_version !== null">
                <dt class="text-2xs font-semibold uppercase text-surface-400">版本</dt>
                <dd class="text-sm font-mono">v{{ citation.document_version }}</dd>
              </div>
              <div v-if="citation.created_at">
                <dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt>
                <dd class="text-sm">{{ formatTime(citation.created_at) }}</dd>
              </div>
            </dl>
          </div>
        </div>

        <!-- Citation loading -->
        <div v-else class="flex items-center justify-center py-12">
          <svg class="h-5 w-5 animate-spin text-brand-600" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
          </svg>
        </div>
      </template>
    </DetailDrawer>
  </div>
</template>