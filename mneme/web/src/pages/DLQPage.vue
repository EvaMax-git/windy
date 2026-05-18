<script setup lang="ts">
import { ref, computed } from "vue";
import { useQuery, useMutation, useQueryClient } from "@tanstack/vue-query";
import PageHeader from "@/components/PageHeader.vue";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import { fetchDeadLetters, replayDeadLetter } from "@/api/client";
import type { DeadLetter } from "@/types";

const page = ref(1);
const pageSize = ref(50);
const filterFailureClass = ref("");
const filterReplayState = ref("");

const selectedItem = ref<DeadLetter | null>(null);
const drawerOpen = ref(false);

const queryClient = useQueryClient();

const queryKey = computed(() => [
  "dead-letters",
  page.value,
  pageSize.value,
  filterFailureClass.value,
  filterReplayState.value,
] as const);

const { data, isLoading, isError, error } = useQuery({
  queryKey,
  queryFn: () =>
    fetchDeadLetters({
      page: page.value,
      page_size: pageSize.value,
      failure_class: filterFailureClass.value || undefined,
      replay_state: filterReplayState.value || undefined,
    }),
  placeholderData: (prev) => prev,
});

const replayMutation = useMutation({
  mutationFn: (id: string) => replayDeadLetter(id),
  onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ["dead-letters"] });
  },
});

const columns = [
  { key: "created_at", label: "时间", width: "170px" },
  { key: "failure_class", label: "故障类型" },
  { key: "source_type", label: "来源" },
  { key: "replay_state", label: "重放状态", width: "130px" },
  { key: "error_message", label: "错误信息" },
];

function openDetail(item: DeadLetter) {
  selectedItem.value = item;
  drawerOpen.value = true;
}

function formatTime(iso: string): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}

function canReplay(state: string): boolean {
  return state === "pending";
}

function handleReplay() {
  if (!selectedItem.value) return;
  replayMutation.mutate(selectedItem.value.dead_letter_id);
}
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader
      title="死信队列"
      subtitle="耗尽重试次数的失败投递 — 查看与重放"
    />

    <!-- Filters -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-center gap-3">
        <select
          v-model="filterFailureClass"
          class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
        >
          <option value="">全部故障类型</option>
          <option value="provider_transient_exhausted">Provider 瞬态耗尽</option>
          <option value="policy_denied_terminal">策略终端拒绝</option>
          <option value="payload_invalid">有效载荷无效</option>
          <option value="code_bug">代码缺陷</option>
          <option value="external_side_effect_unknown">外部副作用未知</option>
        </select>

        <select
          v-model="filterReplayState"
          class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
        >
          <option value="">全部重放状态</option>
          <option value="pending">待处理</option>
          <option value="under_review">审核中</option>
          <option value="replayed">已重放</option>
          <option value="cancelled">已取消</option>
          <option value="resolved">已解决</option>
        </select>

        <span class="text-xs text-surface-400">
          {{ data?.page_info?.total_items ?? 0 }} 条死信
        </span>
      </div>
    </div>

    <!-- Error -->
    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载死信失败: {{ (error as Error)?.message }}
    </div>

    <!-- Table -->
    <DataTable
      :items="data?.items ?? []"
      :columns="columns"
      :loading="isLoading"
      empty-message="无死信记录"
      clickable
      row-key="dead_letter_id"
      @row-click="openDetail"
    >
      <template #cell-created_at="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
      </template>

      <template #cell-failure_class="{ value }">
        <StatusBadge :status="value as string" />
      </template>

      <template #cell-source_type="{ value }">
        <span class="font-mono text-xs">{{ value }}</span>
      </template>

      <template #cell-replay_state="{ value }">
        <StatusBadge :status="value as string" />
      </template>

      <template #cell-error_message="{ value }">
        <span class="text-xs text-surface-400 truncate block max-w-[200px]">
          {{ value || "—" }}
        </span>
      </template>
    </DataTable>

    <Pagination
      v-if="data?.page_info"
      :page-info="data.page_info"
      @page-change="(p: number) => (page = p)"
    />

    <!-- Detail Drawer -->
    <DetailDrawer :open="drawerOpen" title="死信详情" @close="drawerOpen = false">
      <template v-if="selectedItem">
        <dl class="space-y-4">
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">死信ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-700">{{ selectedItem.dead_letter_id }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">来源</dt>
            <dd class="mt-1 text-sm">
              {{ selectedItem.source_type }}
              <span class="font-mono text-xs text-surface-400">/ {{ selectedItem.source_id }}</span>
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">故障类型</dt>
            <dd class="mt-1">
              <StatusBadge
                :status="selectedItem.failure_class"
              />
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">重放状态</dt>
            <dd class="mt-1">
              <StatusBadge :status="selectedItem.replay_state" />
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">错误信息</dt>
            <dd class="mt-1 font-mono text-xs text-red-600 whitespace-pre-wrap">
              {{ selectedItem.error_message || "—" }}
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt>
            <dd class="mt-1 text-sm">{{ formatTime(selectedItem.created_at) }}</dd>
          </div>
          <div v-if="selectedItem.last_retry_at">
            <dt class="text-2xs font-semibold uppercase text-surface-400">最后重试</dt>
            <dd class="mt-1 text-sm">{{ formatTime(selectedItem.last_retry_at) }}</dd>
          </div>
          <div v-if="selectedItem.resolved_at">
            <dt class="text-2xs font-semibold uppercase text-surface-400">解决时间</dt>
            <dd class="mt-1 text-sm">{{ formatTime(selectedItem.resolved_at) }}</dd>
          </div>
        </dl>
      </template>

      <template v-if="selectedItem && canReplay(selectedItem.replay_state)" #footer>
        <button
          class="btn btn-primary w-full"
          :disabled="replayMutation.isPending.value"
          @click="handleReplay"
        >
          {{
            replayMutation.isPending.value
              ? "提交中..."
              : "提交审核重放"
          }}
        </button>
      </template>
    </DetailDrawer>
  </div>
</template>
