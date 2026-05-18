<script setup lang="ts">
import { ref, computed } from "vue";
import { useQuery } from "@tanstack/vue-query";
import PageHeader from "@/components/PageHeader.vue";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import { fetchAuditEvents } from "@/api/client";
import type { AuditEvent } from "@/types";

const page = ref(1);
const pageSize = ref(50);
const filterActorType = ref("");
const filterAction = ref("");
const filterResult = ref("");
const filterDateAfter = ref("");
const filterDateBefore = ref("");

const selectedItem = ref<AuditEvent | null>(null);
const drawerOpen = ref(false);

const queryKey = computed(() => [
  "audit-events",
  page.value,
  pageSize.value,
  filterActorType.value,
  filterAction.value,
  filterResult.value,
  filterDateAfter.value,
  filterDateBefore.value,
] as const);

function buildDateParam(dateStr: string): string | undefined {
  if (!dateStr) return undefined;
  // Append time if only date is provided
  if (dateStr.length <= 10) {
    return filterDateAfter.value ? dateStr + "T00:00:00Z" : undefined;
  }
  return new Date(dateStr).toISOString();
}

const { data, isLoading, isError, error } = useQuery({
  queryKey,
  queryFn: () =>
    fetchAuditEvents({
      page: page.value,
      page_size: pageSize.value,
      actor_type: filterActorType.value || undefined,
      action: filterAction.value || undefined,
      result: filterResult.value || undefined,
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
  { key: "actor_type", label: "操作者" },
  { key: "action", label: "操作" },
  { key: "object_type", label: "对象" },
  { key: "result", label: "结果", width: "100px" },
];

function openDetail(item: AuditEvent) {
  selectedItem.value = item;
  drawerOpen.value = true;
}

function formatTime(iso: string): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}

function clearFilters() {
  filterActorType.value = "";
  filterAction.value = "";
  filterResult.value = "";
  filterDateAfter.value = "";
  filterDateBefore.value = "";
  page.value = 1;
}
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader
      title="审计日志"
      subtitle="安全事件追踪 — 所有操作、拒绝和失败记录"
    />

    <!-- Filters -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-end gap-3">
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">操作者</label>
          <select
            v-model="filterActorType"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部操作者</option>
            <option value="user">用户</option>
            <option value="agent">代理</option>
            <option value="service">服务</option>
            <option value="system">系统</option>
          </select>
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">结果</label>
          <select
            v-model="filterResult"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部结果</option>
            <option value="success">成功</option>
            <option value="denied">拒绝</option>
            <option value="failed">失败</option>
          </select>
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">操作</label>
          <input
            v-model="filterAction"
            type="text"
            placeholder="e.g. review.approved..."
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

        <button
          class="btn btn-ghost btn-sm"
          @click="clearFilters"
        >
          清除
        </button>

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
      加载审计事件失败: {{ (error as Error)?.message }}
    </div>

    <!-- Table -->
    <DataTable
      :items="data?.items ?? []"
      :columns="columns"
      :loading="isLoading"
      :empty-message="isError ? '数据加载出错' : '未找到审计事件'"
      clickable
      row-key="audit_id"
      @row-click="openDetail"
    >
      <template #cell-occurred_at="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
      </template>

      <template #cell-actor_type="{ item }">
        <div class="flex flex-col">
          <span class="text-sm">{{ (item as AuditEvent).actor?.actor_type ?? "—" }}</span>
          <span v-if="(item as AuditEvent).actor?.actor_id" class="font-mono text-2xs text-surface-400">
            {{ (item as AuditEvent).actor!.actor_id!.slice(0, 8) }}…
          </span>
        </div>
      </template>

      <template #cell-action="{ value }">
        <span class="font-mono text-xs">{{ value }}</span>
      </template>

      <template #cell-object_type="{ value }">
        <span class="text-xs text-surface-500">{{ value || "—" }}</span>
      </template>

      <template #cell-result="{ value }">
        <StatusBadge :status="value as string" />
      </template>
    </DataTable>

    <Pagination
      v-if="data?.page_info"
      :page-info="data.page_info"
      @page-change="(p: number) => (page = p)"
    />

    <!-- Detail Drawer -->
    <DetailDrawer :open="drawerOpen" title="审计事件详情" @close="drawerOpen = false">
      <template v-if="selectedItem">
        <dl class="space-y-4">
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">审计ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-700 break-all">{{ selectedItem.audit_id }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">发生时间</dt>
            <dd class="mt-1 text-sm">{{ formatTime(selectedItem.occurred_at) }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">操作者</dt>
            <dd class="mt-1 text-sm">
              <StatusBadge
                :status="selectedItem.actor?.actor_type || ''"
                :label="selectedItem.actor?.actor_type || '—'"
              />
            </dd>
            <dd v-if="selectedItem.actor?.actor_id" class="mt-0.5 font-mono text-xs text-surface-400">
              ID: {{ selectedItem.actor.actor_id }}
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">操作</dt>
            <dd class="mt-1 font-mono text-sm">{{ selectedItem.action }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">对象</dt>
            <dd class="mt-1 text-sm">
              {{ selectedItem.object_type || "—" }}
              <span v-if="selectedItem.object_id" class="font-mono text-xs text-surface-400">
                / {{ selectedItem.object_id.slice(0, 12) }}…
              </span>
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">结果</dt>
            <dd class="mt-1">
              <StatusBadge :status="selectedItem.result" />
            </dd>
          </div>
          <div v-if="selectedItem.reason_code">
            <dt class="text-2xs font-semibold uppercase text-surface-400">原因</dt>
            <dd class="mt-1 text-sm font-mono text-surface-600">{{ selectedItem.reason_code }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">敏感度</dt>
            <dd class="mt-1">
              <StatusBadge :status="selectedItem.sensitivity_level" :label="selectedItem.sensitivity_level" />
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">请求ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ selectedItem.request_id }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">关联ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ selectedItem.correlation_id }}</dd>
          </div>
          <div v-if="selectedItem.project_id">
            <dt class="text-2xs font-semibold uppercase text-surface-400">项目ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-500">{{ selectedItem.project_id }}</dd>
          </div>
          <div v-if="selectedItem.review_item_id">
            <dt class="text-2xs font-semibold uppercase text-surface-400">审核项</dt>
            <dd class="mt-1 font-mono text-xs text-surface-500">{{ selectedItem.review_item_id }}</dd>
          </div>
        </dl>
      </template>
    </DetailDrawer>
  </div>
</template>
