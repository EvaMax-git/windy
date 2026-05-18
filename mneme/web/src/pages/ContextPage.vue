<script setup lang="ts">
import { ref, computed } from "vue";
import { useQuery } from "@tanstack/vue-query";
import PageHeader from "@/components/PageHeader.vue";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import {
  fetchContextPacks,
  fetchContextPack,
} from "@/api/client";
import type { ContextPackSummary, ContextPackDetail } from "@/types";

// ── List state ──
const page = ref(1);
const pageSize = ref(50);

const listKey = computed(() => [
  "context-packs",
  page.value,
  pageSize.value,
] as const);

const { data, isLoading, isError, error } = useQuery({
  queryKey: listKey,
  queryFn: () =>
    fetchContextPacks({
      page: page.value,
      page_size: pageSize.value,
    }),
  placeholderData: (prev) => prev,
});

// ── Detail drawer ──
const selectedPackId = ref<string | null>(null);
const drawerOpen = ref(false);

const { data: packDetail, isLoading: detailLoading } = useQuery({
  queryKey: ["context-pack", selectedPackId],
  queryFn: () => fetchContextPack(selectedPackId.value!),
  enabled: computed(() => drawerOpen.value && !!selectedPackId.value),
});

// ── Table columns ──
const columns = [
  { key: "name", label: "名称", width: "200px" },
  { key: "description", label: "描述", width: "260px" },
  { key: "memory_count", label: "记忆数", width: "90px" },
  { key: "document_count", label: "文档数", width: "90px" },
  { key: "created_at", label: "创建时间", width: "160px" },
];

// ── Helpers ──
function formatTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function openDetail(pack: ContextPackSummary) {
  selectedPackId.value = pack.pack_id;
  drawerOpen.value = true;
}

function formatJson(obj: Record<string, unknown>): string {
  if (!obj || Object.keys(obj).length === 0) return "(空)";
  try {
    return JSON.stringify(obj, null, 2);
  } catch {
    return String(obj);
  }
}
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader
      title="Context Pack 管理"
      subtitle="查看和管理上下文编译包 — 包含记忆和文档的聚合单元"
    />

    <!-- Stats -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-center gap-3">
        <span class="text-xs text-surface-400">
          {{ data?.page_info?.total_items ?? 0 }} 个 Context Pack
        </span>
      </div>
    </div>

    <!-- Error -->
    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载 Context Pack 失败: {{ (error as Error)?.message }}
    </div>

    <!-- Table -->
    <DataTable
      :items="data?.items ?? []"
      :columns="columns"
      :loading="isLoading"
      empty-message="暂无 Context Pack"
      clickable
      row-key="pack_id"
      @row-click="openDetail"
    >
      <!-- Name -->
      <template #cell-name="{ value, item }">
        <div class="flex items-center gap-2 min-w-0">
          <span class="shrink-0 text-xs text-surface-400">📦</span>
          <div class="min-w-0">
            <p class="truncate text-sm font-medium text-surface-800">{{ value || '(未命名)' }}</p>
            <p class="text-2xs text-surface-400 truncate font-mono">
              {{ (item as ContextPackSummary).pack_id?.slice(0, 12) }}…
            </p>
          </div>
        </div>
      </template>

      <!-- Description -->
      <template #cell-description="{ value }">
        <span class="text-xs text-surface-500 truncate max-w-[260px] inline-block">
          {{ value || '—' }}
        </span>
      </template>

      <!-- Memory Count -->
      <template #cell-memory_count="{ value }">
        <span class="badge text-2xs bg-violet-50 text-violet-700">
          {{ value ?? 0 }} 条
        </span>
      </template>

      <!-- Document Count -->
      <template #cell-document_count="{ value }">
        <span class="badge text-2xs bg-teal-50 text-teal-700">
          {{ value ?? 0 }} 篇
        </span>
      </template>

      <!-- Created At -->
      <template #cell-created_at="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
      </template>
    </DataTable>

    <Pagination
      v-if="data?.page_info"
      :page-info="data.page_info"
      @page-change="(p: number) => (page = p)"
    />

    <!-- Detail Drawer -->
    <DetailDrawer
      :open="drawerOpen"
      title="Context Pack 详情"
      width="w-[540px] max-w-full"
      @close="drawerOpen = false"
    >
      <!-- Loading -->
      <div v-if="detailLoading" class="py-4">
        <LoadingSkeleton variant="detail" />
      </div>

      <template v-else-if="packDetail">
        <dl class="space-y-4">
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">Pack ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-700 break-all">{{ packDetail.pack_id }}</dd>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">名称</dt>
              <dd class="mt-1 text-sm text-surface-700">{{ packDetail.name || '(未命名)' }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">创建者</dt>
              <dd class="mt-1 font-mono text-xs text-surface-500">
                {{ packDetail.created_by_user_id ? packDetail.created_by_user_id.slice(0, 12) + '…' : '—' }}
              </dd>
            </div>
          </div>

          <div v-if="packDetail.description">
            <dt class="text-2xs font-semibold uppercase text-surface-400">描述</dt>
            <dd class="mt-1 text-sm text-surface-700">{{ packDetail.description }}</dd>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">记忆数</dt>
              <dd class="mt-1">
                <span class="badge text-2xs bg-violet-50 text-violet-700">
                  {{ packDetail.memory_count ?? 0 }} 条
                </span>
              </dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">文档数</dt>
              <dd class="mt-1">
                <span class="badge text-2xs bg-teal-50 text-teal-700">
                  {{ packDetail.document_count ?? 0 }} 篇
                </span>
              </dd>
            </div>
          </div>

          <!-- Memory IDs -->
          <div v-if="packDetail.memory_ids && packDetail.memory_ids.length > 0">
            <dt class="text-2xs font-semibold uppercase text-surface-400">关联记忆</dt>
            <dd class="mt-1 flex flex-wrap gap-1.5">
              <span
                v-for="mid in packDetail.memory_ids"
                :key="mid"
                class="badge text-2xs bg-violet-50 text-violet-600 font-mono"
              >
                {{ mid.slice(0, 8) }}…
              </span>
            </dd>
          </div>

          <!-- Document IDs -->
          <div v-if="packDetail.document_ids && packDetail.document_ids.length > 0">
            <dt class="text-2xs font-semibold uppercase text-surface-400">关联文档</dt>
            <dd class="mt-1 flex flex-wrap gap-1.5">
              <span
                v-for="did in packDetail.document_ids"
                :key="did"
                class="badge text-2xs bg-teal-50 text-teal-600 font-mono"
              >
                {{ did.slice(0, 8) }}…
              </span>
            </dd>
          </div>

          <!-- Metadata JSON -->
          <div v-if="packDetail.metadata_json && Object.keys(packDetail.metadata_json).length > 0">
            <dt class="text-2xs font-semibold uppercase text-surface-400">元数据</dt>
            <dd class="mt-1">
              <pre class="rounded-lg bg-surface-50 border border-surface-200 p-3 text-xs font-mono text-surface-600 overflow-x-auto max-h-48">{{ formatJson(packDetail.metadata_json) }}</pre>
            </dd>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt>
              <dd class="mt-1 text-xs">{{ formatTime(packDetail.created_at) }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">更新时间</dt>
              <dd class="mt-1 text-xs">{{ formatTime(packDetail.updated_at) }}</dd>
            </div>
          </div>
        </dl>
      </template>

      <!-- Empty detail -->
      <div v-else class="flex flex-col items-center justify-center py-12 text-sm text-surface-400">
        加载 Context Pack 详情失败
      </div>

      <!-- Footer -->
      <template #footer>
        <div class="flex items-center justify-between">
          <span class="text-2xs text-surface-400">
            ID: {{ selectedPackId ? selectedPackId.slice(0, 8) + '...' : '—' }}
          </span>
          <button
            class="btn btn-secondary btn-sm"
            @click="drawerOpen = false"
          >
            关闭
          </button>
        </div>
      </template>
    </DetailDrawer>
  </div>
</template>
