<script setup lang="ts">
import { ref, computed } from "vue";
import { useQuery } from "@tanstack/vue-query";
import PageHeader from "@/components/PageHeader.vue";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import { fetchEvents, fetchEventDetail } from "@/api/client";
import type { EventRead, EventDetail, EventDelivery } from "@/types";
import { statusToColor } from "@/types";

const page = ref(1);
const pageSize = ref(50);
const filterEventType = ref("");
const filterPublishState = ref("");
const filterDateAfter = ref("");
const filterDateBefore = ref("");

const selectedEvent = ref<EventRead | null>(null);
const eventDetail = ref<EventDetail | null>(null);
const detailLoading = ref(false);
const drawerOpen = ref(false);

const queryKey = computed(() => [
  "outbox-events",
  page.value,
  pageSize.value,
  filterEventType.value,
  filterPublishState.value,
  filterDateAfter.value,
  filterDateBefore.value,
] as const);

const { data, isLoading, isError, error } = useQuery({
  queryKey,
  queryFn: () =>
    fetchEvents({
      page: page.value,
      page_size: pageSize.value,
      event_type: filterEventType.value || undefined,
      publish_state: filterPublishState.value || undefined,
      occurred_after: filterDateAfter.value
        ? (filterDateAfter.value.includes("T")
            ? new Date(filterDateAfter.value).toISOString()
            : filterDateAfter.value + "T00:00:00.000Z")
        : undefined,
      occurred_before: filterDateBefore.value
        ? (filterDateBefore.value.includes("T")
            ? new Date(filterDateBefore.value).toISOString()
            : filterDateBefore.value + "T23:59:59.999Z")
        : undefined,
    }),
  placeholderData: (prev) => prev,
});

const columns = [
  { key: "occurred_at", label: "时间", width: "170px" },
  { key: "event_type", label: "事件类型" },
  { key: "aggregate_type", label: "聚合" },
  { key: "publish_state", label: "状态", width: "110px" },
  { key: "producer", label: "生产者", width: "110px" },
];

async function openDetail(event: EventRead) {
  selectedEvent.value = event;
  drawerOpen.value = true;
  detailLoading.value = true;
  eventDetail.value = null;
  try {
    eventDetail.value = await fetchEventDetail(event.event_id);
  } catch {
    // fall back to list data
    eventDetail.value = null;
  } finally {
    detailLoading.value = false;
  }
}

function formatTime(iso: string): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}

function clearFilters() {
  filterEventType.value = "";
  filterPublishState.value = "";
  filterDateAfter.value = "";
  filterDateBefore.value = "";
  page.value = 1;
}

function getDeliveryColor(state: string): string {
  const map: Record<string, string> = {
    pending: "yellow",
    dispatched: "yellow",
    acknowledged: "green",
    delivered: "green",
    failed: "red",
    dead_letter: "red",
  };
  return map[state] || "gray";
}
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader
      title="发件箱"
      subtitle="事件发布管道 — 待处理、已分发、已送达、失败"
    />

    <!-- Filters -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-end gap-3">
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">状态</label>
          <select
            v-model="filterPublishState"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
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
          <input
            v-model="filterEventType"
            type="text"
            placeholder="e.g. review.created..."
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 w-44"
          />
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">从</label>
          <input
            v-model="filterDateAfter"
            type="datetime-local"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          />
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">至</label>
          <input
            v-model="filterDateBefore"
            type="datetime-local"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          />
        </div>

        <button class="btn btn-ghost btn-sm" @click="clearFilters">清除</button>

        <span class="text-xs text-surface-400 ml-auto self-end pb-1">
          {{ data?.page_info?.total_items ?? 0 }} 条事件
        </span>
      </div>
    </div>

    <!-- Error -->
    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载事件失败: {{ (error as Error)?.message }}
    </div>

    <!-- Table -->
    <DataTable
      :items="data?.items ?? []"
      :columns="columns"
      :loading="isLoading"
      empty-message="未找到事件"
      clickable
      row-key="event_id"
      @row-click="openDetail"
    >
      <template #cell-occurred_at="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
      </template>

      <template #cell-event_type="{ value }">
        <span class="font-mono text-xs">{{ value }}</span>
      </template>

      <template #cell-aggregate_type="{ value }">
        <div class="flex flex-col">
          <span class="text-xs text-surface-500">{{ value }}</span>
        </div>
      </template>

      <template #cell-publish_state="{ value }">
        <StatusBadge :status="value as string" />
      </template>

      <template #cell-producer="{ value }">
        <span class="text-xs text-surface-500">{{ value }}</span>
      </template>
    </DataTable>

    <Pagination
      v-if="data?.page_info"
      :page-info="data.page_info"
      @page-change="(p: number) => (page = p)"
    />

    <!-- Detail Drawer -->
    <DetailDrawer :open="drawerOpen" title="事件详情" @close="drawerOpen = false">
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
              <span class="font-mono text-xs text-surface-400">/ {{ selectedEvent.aggregate_id.slice(0, 12) }}…</span>
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">发布状态</dt>
            <dd class="mt-1">
              <StatusBadge :status="selectedEvent.publish_state" />
            </dd>
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

        <!-- Deliveries section -->
        <div v-if="detailLoading" class="mt-6 border-t border-surface-200 pt-4">
          <div class="flex items-center gap-2 text-sm text-surface-400">
            <div class="h-4 w-4 animate-spin rounded-full border-2 border-surface-200 border-t-brand-500"></div>
            正在加载投递记录...
          </div>
        </div>

        <div v-else-if="eventDetail?.deliveries && eventDetail.deliveries.length > 0" class="mt-6 border-t border-surface-200 pt-4">
          <h4 class="text-xs font-semibold uppercase text-surface-400 mb-3">
            投递记录 ({{ eventDetail.deliveries.length }})
          </h4>
          <div class="space-y-2">
            <div
              v-for="delivery in eventDetail.deliveries"
              :key="delivery.delivery_id"
              class="rounded-lg border border-surface-100 bg-surface-50 p-3 text-xs"
            >
              <div class="flex items-center justify-between mb-1">
                <span class="font-mono font-medium text-surface-700">{{ delivery.consumer_name }}</span>
                <StatusBadge
                  :status="delivery.delivery_state"
                  :label="delivery.delivery_state"
                />
              </div>
              <div class="flex flex-wrap gap-x-4 gap-y-1 text-surface-400">
                <span>尝试次数: {{ delivery.dispatch_attempts }}</span>
                <span v-if="delivery.last_dispatched_at">
                  最后分发: {{ formatTime(delivery.last_dispatched_at) }}
                </span>
                <span v-if="delivery.acknowledged_at">
                  已确认: {{ formatTime(delivery.acknowledged_at) }}
                </span>
              </div>
              <div v-if="delivery.last_error" class="mt-1 font-mono text-red-500 text-2xs">
                {{ delivery.last_error.slice(0, 200) }}{{ delivery.last_error.length > 200 ? '…' : '' }}
              </div>
            </div>
          </div>
        </div>
        <div v-else-if="!detailLoading" class="mt-6 border-t border-surface-200 pt-4">
          <p class="text-sm text-surface-400">未找到此事件的投递记录。</p>
        </div>
      </template>
    </DetailDrawer>
  </div>
</template>
