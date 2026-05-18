<script setup lang="ts">
import { ref, computed } from "vue";
import { useQuery } from "@tanstack/vue-query";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import { fetchJobs, fetchJobDetail, fetchEvents, fetchEventDetail } from "@/api/client";
import type { JobSummary, JobDetail, EventRead, EventDetail } from "@/types";

// ── Sub-tabs ──
type SubTab = "jobs" | "outbox";
const activeTab = ref<SubTab>("jobs");

// ══════════════ JOBS ══════════════
const jobsPage = ref(1);
const jobsPageSize = ref(50);
const filterStatus = ref("");

const selectedJob = ref<JobDetail | null>(null);
const jobDrawerOpen = ref(false);
const jobDetailLoading = ref(false);

const jobsQueryKey = computed(() => [
  "jobs", jobsPage.value, jobsPageSize.value, filterStatus.value,
] as const);

const { data: jobsData, isLoading: jobsLoading, isError: jobsError, error: jobsErr } = useQuery({
  queryKey: jobsQueryKey,
  queryFn: () =>
    fetchJobs({
      page: jobsPage.value,
      page_size: jobsPageSize.value,
      status: filterStatus.value || undefined,
    }),
  placeholderData: (prev) => prev,
  enabled: computed(() => activeTab.value === "jobs"),
});

const jobColumns = [
  { key: "created_at", label: "创建时间", width: "170px" },
  { key: "job_type", label: "类型", width: "100px" },
  { key: "job_key", label: "键" },
  { key: "status", label: "状态", width: "120px" },
  { key: "retry_count", label: "重试次数", width: "80px" },
];

async function openJobDetail(item: JobSummary) {
  jobDetailLoading.value = true;
  jobDrawerOpen.value = true;
  selectedJob.value = null;
  try {
    const detail = await fetchJobDetail(item.job_id);
    selectedJob.value = detail;
  } catch {
    // Keep drawer open
  } finally {
    jobDetailLoading.value = false;
  }
}

// ══════════════ OUTBOX ══════════════
const outboxPage = ref(1);
const outboxPageSize = ref(50);
const filterEventType = ref("");
const filterPublishState = ref("");

const selectedEvent = ref<EventRead | null>(null);
const eventDetail = ref<EventDetail | null>(null);
const eventDetailLoading = ref(false);
const eventDrawerOpen = ref(false);

const outboxQueryKey = computed(() => [
  "outbox-events", outboxPage.value, outboxPageSize.value, filterEventType.value, filterPublishState.value,
] as const);

const { data: outboxData, isLoading: outboxLoading, isError: outboxError, error: outboxErr } = useQuery({
  queryKey: outboxQueryKey,
  queryFn: () =>
    fetchEvents({
      page: outboxPage.value,
      page_size: outboxPageSize.value,
      event_type: filterEventType.value || undefined,
      publish_state: filterPublishState.value || undefined,
    }),
  placeholderData: (prev) => prev,
  enabled: computed(() => activeTab.value === "outbox"),
});

const outboxColumns = [
  { key: "occurred_at", label: "时间", width: "170px" },
  { key: "event_type", label: "事件类型" },
  { key: "aggregate_type", label: "聚合" },
  { key: "publish_state", label: "状态", width: "110px" },
  { key: "producer", label: "生产者", width: "110px" },
];

async function openEventDetail(event: EventRead) {
  selectedEvent.value = event;
  eventDrawerOpen.value = true;
  eventDetailLoading.value = true;
  eventDetail.value = null;
  try {
    eventDetail.value = await fetchEventDetail(event.event_id);
  } catch {
    eventDetail.value = null;
  } finally {
    eventDetailLoading.value = false;
  }
}

// ── Helpers ──
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
  <div>
    <!-- Sub-tab switcher -->
    <div class="flex gap-2 mb-6">
      <button
        :class="['btn btn-sm', activeTab === 'jobs' ? 'btn-primary' : 'btn-secondary']"
        @click="activeTab = 'jobs'"
      >任务队列</button>
      <button
        :class="['btn btn-sm', activeTab === 'outbox' ? 'btn-primary' : 'btn-secondary']"
        @click="activeTab = 'outbox'"
      >发件箱</button>
    </div>

    <!-- ═══ JOBS SUB-TAB ═══ -->
    <template v-if="activeTab === 'jobs'">
      <div class="card mb-6 p-4">
        <div class="flex flex-wrap items-center gap-3">
          <select v-model="filterStatus"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500">
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
          <span class="text-xs text-surface-400">{{ jobsData?.page_info?.total_items ?? 0 }} 个任务</span>
        </div>
      </div>

      <div v-if="jobsError" class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700">
        加载任务失败: {{ (jobsErr as Error)?.message }}
      </div>

      <DataTable
        :items="jobsData?.items ?? []"
        :columns="jobColumns"
        :loading="jobsLoading"
        empty-message="未找到任务"
        clickable row-key="job_id"
        @row-click="openJobDetail"
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
          <span class="font-mono text-xs">{{ (item as JobSummary).retry_count }}/{{ (item as JobSummary).max_retries }}</span>
        </template>
      </DataTable>

      <Pagination v-if="jobsData?.page_info" :page-info="jobsData.page_info" @page-change="(p: number) => (jobsPage = p)" />

      <!-- Job Detail Drawer -->
      <DetailDrawer :open="jobDrawerOpen" title="任务详情" width="w-[480px] max-w-full" @close="jobDrawerOpen = false">
        <div v-if="jobDetailLoading" class="py-4"><LoadingSkeleton variant="detail" /></div>
        <template v-else-if="selectedJob">
          <dl class="space-y-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">任务ID</dt>
              <dd class="mt-1 font-mono text-xs text-surface-700 break-all">{{ selectedJob.job_id }}</dd>
            </div>
            <div class="grid grid-cols-2 gap-4">
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">类型</dt>
                <dd class="mt-1 font-mono text-xs font-medium uppercase">{{ selectedJob.job_type }}</dd>
              </div>
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">状态</dt>
                <dd class="mt-1"><StatusBadge :status="selectedJob.status" /></dd>
              </div>
            </div>
            <div class="grid grid-cols-2 gap-4">
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">优先级</dt>
                <dd class="mt-1 font-mono text-xs">{{ selectedJob.priority }}</dd>
              </div>
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">队列</dt>
                <dd class="mt-1 text-xs">{{ selectedJob.queue_name }}</dd>
              </div>
            </div>
            <div class="grid grid-cols-2 gap-4">
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">重试</dt>
                <dd class="mt-1 font-mono text-xs">{{ selectedJob.retry_count }}/{{ selectedJob.max_retries }}</dd>
              </div>
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">耗时</dt>
                <dd class="mt-1 font-mono text-xs">{{ formatDuration(selectedJob.started_at, selectedJob.finished_at) }}</dd>
              </div>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">任务键</dt>
              <dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ selectedJob.job_key }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">调度时间</dt>
              <dd class="mt-1 text-xs">{{ formatTime(selectedJob.scheduled_at) }}</dd>
            </div>
            <div v-if="selectedJob.started_at">
              <dt class="text-2xs font-semibold uppercase text-surface-400">开始时间</dt>
              <dd class="mt-1 text-xs">{{ formatTime(selectedJob.started_at) }}</dd>
            </div>
            <div v-if="selectedJob.finished_at">
              <dt class="text-2xs font-semibold uppercase text-surface-400">完成时间</dt>
              <dd class="mt-1 text-xs">{{ formatTime(selectedJob.finished_at) }}</dd>
            </div>
            <div v-if="selectedJob.created_by_actor_type">
              <dt class="text-2xs font-semibold uppercase text-surface-400">创建者</dt>
              <dd class="mt-1 text-xs">
                {{ selectedJob.created_by_actor_type }}
                <span v-if="selectedJob.created_by_actor_id" class="font-mono text-xs text-surface-400">({{ selectedJob.created_by_actor_id }})</span>
              </dd>
            </div>
            <div v-if="selectedJob.input_payload && Object.keys(selectedJob.input_payload).length > 0">
              <dt class="text-2xs font-semibold uppercase text-surface-400">输入</dt>
              <dd class="mt-1">
                <pre class="rounded-lg bg-surface-50 p-3 font-mono text-2xs text-surface-600 overflow-x-auto max-h-32">{{ JSON.stringify(selectedJob.input_payload, null, 2) }}</pre>
              </dd>
            </div>
            <div v-if="selectedJob.output && Object.keys(selectedJob.output).length > 0">
              <dt class="text-2xs font-semibold uppercase text-surface-400">输出</dt>
              <dd class="mt-1">
                <pre class="rounded-lg bg-surface-50 p-3 font-mono text-2xs text-surface-600 overflow-x-auto max-h-32">{{ JSON.stringify(selectedJob.output, null, 2) }}</pre>
              </dd>
            </div>
            <div v-if="selectedJob.last_error">
              <dt class="text-2xs font-semibold uppercase text-surface-400">最后错误</dt>
              <dd class="mt-1 font-mono text-xs text-red-600 whitespace-pre-wrap break-all">{{ selectedJob.last_error }}</dd>
            </div>
            <div v-if="selectedJob.logs?.length">
              <dt class="text-2xs font-semibold uppercase text-surface-400 mb-2">日志 ({{ selectedJob.logs.length }})</dt>
              <dd>
                <div class="divide-y divide-surface-100 rounded-lg border border-surface-200 overflow-hidden">
                  <div v-for="log in selectedJob.logs" :key="log.job_log_id" class="px-3 py-2"
                    :class="{ 'bg-red-50/50': log.level === 'error', 'bg-amber-50/50': log.level === 'warn' }">
                    <div class="flex items-center gap-2">
                      <span class="inline-flex items-center rounded px-1.5 py-0.5 text-2xs font-semibold uppercase"
                        :class="{ 'bg-red-100 text-red-700': log.level === 'error', 'bg-amber-100 text-amber-700': log.level === 'warn', 'bg-surface-100 text-surface-500': log.level === 'info' || log.level === 'debug' }">
                        {{ log.level }}
                      </span>
                      <span class="font-mono text-2xs text-surface-400">{{ log.step }}</span>
                      <span class="font-mono text-2xs text-surface-300 ml-auto">{{ formatTime(log.occurred_at) }}</span>
                    </div>
                    <p class="mt-1 text-xs text-surface-600">{{ log.message }}</p>
                  </div>
                </div>
              </dd>
            </div>
          </dl>
        </template>
        <div v-else class="flex flex-col items-center justify-center py-12 text-sm text-surface-400">加载任务详情失败</div>
      </DetailDrawer>
    </template>

    <!-- ═══ OUTBOX SUB-TAB ═══ -->
    <template v-if="activeTab === 'outbox'">
      <div class="card mb-6 p-4">
        <div class="flex flex-wrap items-end gap-3">
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-semibold uppercase text-surface-400">状态</label>
            <select v-model="filterPublishState"
              class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500">
              <option value="">全部状态</option>
              <option value="pending">待处理</option>
              <option value="dispatched">已分发</option>
              <option value="delivered">已送达</option>
              <option value="failed">失败</option>
              <option value="dead_letter">死信</option>
            </select>
          </div>
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-semibold uppercase text-surface-400">事件类型</label>
            <input v-model="filterEventType" type="text" placeholder="e.g. review.created..."
              class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 w-44" />
          </div>
          <button class="btn btn-ghost btn-sm" @click="filterEventType = ''; filterPublishState = ''; outboxPage = 1">清除</button>
          <span class="text-xs text-surface-400 ml-auto self-end pb-1">{{ outboxData?.page_info?.total_items ?? 0 }} 条事件</span>
        </div>
      </div>

      <div v-if="outboxError" class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700">
        加载事件失败: {{ (outboxErr as Error)?.message }}
      </div>

      <DataTable
        :items="outboxData?.items ?? []"
        :columns="outboxColumns"
        :loading="outboxLoading"
        empty-message="未找到事件"
        clickable row-key="event_id"
        @row-click="openEventDetail"
      >
        <template #cell-occurred_at="{ value }">
          <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
        </template>
        <template #cell-event_type="{ value }">
          <span class="font-mono text-xs">{{ value }}</span>
        </template>
        <template #cell-aggregate_type="{ value }">
          <span class="text-xs text-surface-500">{{ value }}</span>
        </template>
        <template #cell-publish_state="{ value }">
          <StatusBadge :status="value as string" />
        </template>
        <template #cell-producer="{ value }">
          <span class="text-xs text-surface-500">{{ value }}</span>
        </template>
      </DataTable>

      <Pagination v-if="outboxData?.page_info" :page-info="outboxData.page_info" @page-change="(p: number) => (outboxPage = p)" />

      <!-- Event Detail Drawer -->
      <DetailDrawer :open="eventDrawerOpen" title="事件详情" width="w-[480px] max-w-full" @close="eventDrawerOpen = false">
        <template v-if="selectedEvent">
          <dl class="space-y-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">事件ID</dt>
              <dd class="mt-1 font-mono text-xs text-surface-700 break-all">{{ selectedEvent.event_id }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">事件类型</dt>
              <dd class="mt-1 font-mono text-sm">{{ selectedEvent.event_type }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">聚合</dt>
              <dd class="mt-1 text-sm">
                {{ selectedEvent.aggregate_type }}
                <span class="font-mono text-xs text-surface-400">/ {{ selectedEvent.aggregate_id.slice(0, 12) }}...</span>
              </dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">发布状态</dt>
              <dd class="mt-1"><StatusBadge :status="selectedEvent.publish_state" /></dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">可见性</dt>
              <dd class="mt-1 text-sm">{{ selectedEvent.visibility }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">发生时间</dt>
              <dd class="mt-1 text-sm">{{ formatTime(selectedEvent.occurred_at) }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">提交时间</dt>
              <dd class="mt-1 text-sm">{{ formatTime(selectedEvent.committed_at) }}</dd>
            </div>
            <div v-if="selectedEvent.published_at">
              <dt class="text-2xs font-semibold uppercase text-surface-400">发布时间</dt>
              <dd class="mt-1 text-sm">{{ formatTime(selectedEvent.published_at) }}</dd>
            </div>
            <div v-if="selectedEvent.last_error">
              <dt class="text-2xs font-semibold uppercase text-surface-400">最后错误</dt>
              <dd class="mt-1 font-mono text-xs text-red-600 break-all">{{ selectedEvent.last_error }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">生产者</dt>
              <dd class="mt-1 text-sm">{{ selectedEvent.producer }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">幂等键</dt>
              <dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ selectedEvent.idempotency_key }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">关联ID</dt>
              <dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ selectedEvent.correlation_id }}</dd>
            </div>
          </dl>

          <!-- Deliveries -->
          <div v-if="eventDetailLoading" class="mt-6 border-t border-surface-200 pt-4">
            <div class="flex items-center gap-2 text-sm text-surface-400">
              <div class="h-4 w-4 animate-spin rounded-full border-2 border-surface-200 border-t-brand-500"></div>
              正在加载投递记录...
            </div>
          </div>
          <div v-else-if="eventDetail?.deliveries?.length" class="mt-6 border-t border-surface-200 pt-4">
            <h4 class="text-xs font-semibold uppercase text-surface-400 mb-3">投递记录 ({{ eventDetail.deliveries.length }})</h4>
            <div class="space-y-2">
              <div v-for="delivery in eventDetail.deliveries" :key="delivery.delivery_id"
                class="rounded-lg border border-surface-100 bg-surface-50 p-3 text-xs">
                <div class="flex items-center justify-between mb-1">
                  <span class="font-mono font-medium text-surface-700">{{ delivery.consumer_name }}</span>
                  <StatusBadge :status="delivery.delivery_state" :label="delivery.delivery_state" />
                </div>
                <div class="flex flex-wrap gap-x-4 gap-y-1 text-surface-400">
                  <span>尝试次数: {{ delivery.dispatch_attempts }}</span>
                  <span v-if="delivery.last_dispatched_at">最后分发: {{ formatTime(delivery.last_dispatched_at) }}</span>
                  <span v-if="delivery.acknowledged_at">已确认: {{ formatTime(delivery.acknowledged_at) }}</span>
                </div>
                <div v-if="delivery.last_error" class="mt-1 font-mono text-red-500 text-2xs">
                  {{ delivery.last_error.slice(0, 200) }}{{ delivery.last_error.length > 200 ? '...' : '' }}
                </div>
              </div>
            </div>
          </div>
          <div v-else-if="!eventDetailLoading" class="mt-6 border-t border-surface-200 pt-4">
            <p class="text-sm text-surface-400">未找到此事件的投递记录。</p>
          </div>
        </template>
      </DetailDrawer>
    </template>
  </div>
</template>
