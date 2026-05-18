<script setup lang="ts">
import { ref, computed } from "vue";
import { useQuery } from "@tanstack/vue-query";
import PageHeader from "@/components/PageHeader.vue";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import { fetchJobs, fetchJobDetail } from "@/api/client";
import type { JobSummary, JobDetail } from "@/types";

const page = ref(1);
const pageSize = ref(50);
const filterStatus = ref("");

const selectedItem = ref<JobDetail | null>(null);
const drawerOpen = ref(false);
const detailLoading = ref(false);

const queryKey = computed(() => [
  "jobs",
  page.value,
  pageSize.value,
  filterStatus.value,
] as const);

const { data, isLoading, isError, error } = useQuery({
  queryKey,
  queryFn: () =>
    fetchJobs({
      page: page.value,
      page_size: pageSize.value,
      status: filterStatus.value || undefined,
    }),
  placeholderData: (prev) => prev,
});

const columns = [
  { key: "created_at", label: "创建时间", width: "170px" },
  { key: "job_type", label: "类型", width: "100px" },
  { key: "job_key", label: "键" },
  { key: "status", label: "状态", width: "120px" },
  { key: "retry_count", label: "重试次数", width: "80px" },
];

async function openDetail(item: JobSummary) {
  detailLoading.value = true;
  drawerOpen.value = true;
  selectedItem.value = null;
  try {
    const detail = await fetchJobDetail(item.job_id);
    selectedItem.value = detail;
  } catch {
    // Keep drawer open with empty state
  } finally {
    detailLoading.value = false;
  }
}

function formatTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function formatDuration(start: string | null, end: string | null): string {
  if (!start || !end) return "—";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader
      title="任务"
      subtitle="后台任务执行 — 备份、恢复和维护任务"
    />

    <!-- Filters -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-center gap-3">
        <select
          v-model="filterStatus"
          class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
        >
          <option value="">全部状态</option>
          <option value="pending">待处理</option>
          <option value="scheduled">已调度</option>
          <option value="running">运行中</option>
          <option value="succeeded">已成功</option>
          <option value="failed">失败</option>
          <option value="retrying">重试中</option>
          <option value="cancelled">已取消</option>
          <option value="dead_letter">死信</option>
        </select>

        <span class="text-xs text-surface-400">
          {{ data?.page_info?.total_items ?? 0 }} 个任务
        </span>
      </div>
    </div>

    <!-- Error -->
    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载任务失败: {{ (error as Error)?.message }}
    </div>

    <!-- Table -->
    <DataTable
      :items="data?.items ?? []"
      :columns="columns"
      :loading="isLoading"
      empty-message="未找到任务"
      clickable
      row-key="job_id"
      @row-click="openDetail"
    >
      <template #cell-created_at="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
      </template>

      <template #cell-job_type="{ value }">
        <span class="font-mono text-xs font-medium uppercase">{{ value }}</span>
      </template>

      <template #cell-job_key="{ value }">
        <span class="font-mono text-xs text-surface-500 truncate max-w-[200px] block">{{ value }}</span>
      </template>

      <template #cell-status="{ value }">
        <StatusBadge :status="value as string" />
      </template>

      <template #cell-retry_count="{ item }">
        <span class="font-mono text-xs">
          {{ (item as JobSummary).retry_count }}/{{ (item as JobSummary).max_retries }}
        </span>
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
      title="任务详情"
      width="w-[480px] max-w-full"
      @close="drawerOpen = false"
    >
      <!-- Loading -->
      <div v-if="detailLoading" class="py-4">
        <LoadingSkeleton variant="detail" />
      </div>

      <!-- Detail content -->
      <template v-else-if="selectedItem">
        <dl class="space-y-4">
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">任务ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-700 break-all">{{ selectedItem.job_id }}</dd>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">类型</dt>
              <dd class="mt-1 font-mono text-xs font-medium uppercase">{{ selectedItem.job_type }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">状态</dt>
              <dd class="mt-1">
                <StatusBadge :status="selectedItem.status" />
              </dd>
            </div>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">优先级</dt>
              <dd class="mt-1 font-mono text-xs">{{ selectedItem.priority }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">队列</dt>
              <dd class="mt-1 text-xs">{{ selectedItem.queue_name }}</dd>
            </div>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">重试</dt>
              <dd class="mt-1 font-mono text-xs">
                {{ selectedItem.retry_count }}/{{ selectedItem.max_retries }}
              </dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">耗时</dt>
              <dd class="mt-1 font-mono text-xs">
                {{ formatDuration(selectedItem.started_at, selectedItem.finished_at) }}
              </dd>
            </div>
          </div>

          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">任务键</dt>
            <dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ selectedItem.job_key }}</dd>
          </div>

          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">调度时间</dt>
            <dd class="mt-1 text-xs">{{ formatTime(selectedItem.scheduled_at) }}</dd>
          </div>

          <div v-if="selectedItem.started_at">
            <dt class="text-2xs font-semibold uppercase text-surface-400">开始时间</dt>
            <dd class="mt-1 text-xs">{{ formatTime(selectedItem.started_at) }}</dd>
          </div>

          <div v-if="selectedItem.finished_at">
            <dt class="text-2xs font-semibold uppercase text-surface-400">完成时间</dt>
            <dd class="mt-1 text-xs">{{ formatTime(selectedItem.finished_at) }}</dd>
          </div>

          <div v-if="selectedItem.created_by_actor_type">
            <dt class="text-2xs font-semibold uppercase text-surface-400">创建者</dt>
            <dd class="mt-1 text-xs">
              {{ selectedItem.created_by_actor_type }}
              <span
                v-if="selectedItem.created_by_actor_id"
                class="font-mono text-xs text-surface-400"
              >
                ({{ selectedItem.created_by_actor_id }})
              </span>
            </dd>
          </div>

          <!-- Input payload -->
          <div v-if="selectedItem.input_payload && Object.keys(selectedItem.input_payload).length > 0">
            <dt class="text-2xs font-semibold uppercase text-surface-400">输入</dt>
            <dd class="mt-1">
              <pre class="rounded-lg bg-surface-50 p-3 font-mono text-2xs text-surface-600 overflow-x-auto max-h-32">{{ JSON.stringify(selectedItem.input_payload, null, 2) }}</pre>
            </dd>
          </div>

          <!-- Output -->
          <div v-if="selectedItem.output && Object.keys(selectedItem.output).length > 0">
            <dt class="text-2xs font-semibold uppercase text-surface-400">输出</dt>
            <dd class="mt-1">
              <pre class="rounded-lg bg-surface-50 p-3 font-mono text-2xs text-surface-600 overflow-x-auto max-h-32">{{ JSON.stringify(selectedItem.output, null, 2) }}</pre>
            </dd>
          </div>

          <!-- Last error -->
          <div v-if="selectedItem.last_error">
            <dt class="text-2xs font-semibold uppercase text-surface-400">最后错误</dt>
            <dd class="mt-1 font-mono text-xs text-red-600 whitespace-pre-wrap break-all">
              {{ selectedItem.last_error }}
            </dd>
          </div>

          <!-- Job Logs -->
          <div v-if="selectedItem.logs && selectedItem.logs.length > 0">
            <dt class="text-2xs font-semibold uppercase text-surface-400 mb-2">
              日志 ({{ selectedItem.logs.length }})
            </dt>
            <dd>
              <div class="divide-y divide-surface-100 rounded-lg border border-surface-200 overflow-hidden">
                <div
                  v-for="log in selectedItem.logs"
                  :key="log.job_log_id"
                  class="px-3 py-2"
                  :class="{
                    'bg-red-50/50': log.level === 'error',
                    'bg-amber-50/50': log.level === 'warn',
                  }"
                >
                  <div class="flex items-center gap-2">
                    <span
                      class="inline-flex items-center rounded px-1.5 py-0.5 text-2xs font-semibold uppercase"
                      :class="{
                        'bg-red-100 text-red-700': log.level === 'error',
                        'bg-amber-100 text-amber-700': log.level === 'warn',
                        'bg-surface-100 text-surface-500': log.level === 'info' || log.level === 'debug',
                      }"
                    >
                      {{ log.level }}
                    </span>
                    <span class="font-mono text-2xs text-surface-400">{{ log.step }}</span>
                    <span class="font-mono text-2xs text-surface-300 ml-auto">
                      {{ formatTime(log.occurred_at) }}
                    </span>
                  </div>
                  <p class="mt-1 text-xs text-surface-600">{{ log.message }}</p>
                </div>
              </div>
            </dd>
          </div>
        </dl>
      </template>

      <!-- Empty / error in detail -->
      <div v-else class="flex flex-col items-center justify-center py-12 text-sm text-surface-400">
        加载任务详情失败
      </div>
    </DetailDrawer>
  </div>
</template>
