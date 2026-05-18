<script setup lang="ts">
import { ref, computed } from "vue";
import { useQuery, useMutation, useQueryClient } from "@tanstack/vue-query";
import PageHeader from "@/components/PageHeader.vue";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import { fetchReviewItems, approveReviewItem, rejectReviewItem } from "@/api/client";
import type { ReviewItem } from "@/types";
import { statusToColor } from "@/types";

const page = ref(1);
const pageSize = ref(50);
const filterReviewType = ref("");
const filterStatus = ref("");
const filterDateAfter = ref("");
const filterDateBefore = ref("");

const selectedItem = ref<ReviewItem | null>(null);
const drawerOpen = ref(false);
const actionReason = ref("");
const actionError = ref("");

const queryClient = useQueryClient();

const queryKey = computed(() => [
  "review-items",
  page.value,
  pageSize.value,
  filterReviewType.value,
  filterStatus.value,
  filterDateAfter.value,
  filterDateBefore.value,
] as const);

const { data, isLoading, isError, error } = useQuery({
  queryKey,
  queryFn: () =>
    fetchReviewItems({
      page: page.value,
      page_size: pageSize.value,
      review_type: filterReviewType.value || undefined,
      status: filterStatus.value || undefined,
      created_after: filterDateAfter.value
        ? (filterDateAfter.value.includes("T")
            ? new Date(filterDateAfter.value).toISOString()
            : filterDateAfter.value + "T00:00:00.000Z")
        : undefined,
      created_before: filterDateBefore.value
        ? (filterDateBefore.value.includes("T")
            ? new Date(filterDateBefore.value).toISOString()
            : filterDateBefore.value + "T23:59:59.999Z")
        : undefined,
    }),
  placeholderData: (prev) => prev,
});

const approveMutation = useMutation({
  mutationFn: ({ id, reason }: { id: string; reason: string }) =>
    approveReviewItem(id, reason),
  onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ["review-items"] });
    drawerOpen.value = false;
    actionError.value = "";
  },
  onError: (err: Error) => {
    actionError.value = err.message || "审批失败";
  },
});

const rejectMutation = useMutation({
  mutationFn: ({ id, reason }: { id: string; reason: string }) =>
    rejectReviewItem(id, reason),
  onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ["review-items"] });
    drawerOpen.value = false;
    actionError.value = "";
  },
  onError: (err: Error) => {
    actionError.value = err.message || "拒绝失败";
  },
});

const columns = [
  { key: "created_at", label: "创建时间", width: "170px" },
  { key: "review_type", label: "类型", width: "140px" },
  { key: "target_type", label: "目标", width: "120px" },
  { key: "status", label: "状态", width: "110px" },
  { key: "priority", label: "优先级", width: "70px" },
];

function openDetail(item: ReviewItem) {
  selectedItem.value = item;
  drawerOpen.value = true;
  actionReason.value = "";
  actionError.value = "";
}

function formatTime(iso: string): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}

function canAct(status: string): boolean {
  return status === "pending" || status === "in_review";
}

function isFinal(status: string): boolean {
  return ["approved", "rejected", "cancelled", "expired"].includes(status);
}

async function handleApprove() {
  if (!selectedItem.value) return;
  actionError.value = "";
  approveMutation.mutate({
    id: selectedItem.value.review_item_id,
    reason: actionReason.value || "通过治理界面批准",
  });
}

async function handleReject() {
  if (!selectedItem.value) return;
  actionError.value = "";
  rejectMutation.mutate({
    id: selectedItem.value.review_item_id,
    reason: actionReason.value || "通过治理界面拒绝",
  });
}

function clearFilters() {
  filterReviewType.value = "";
  filterStatus.value = "";
  filterDateAfter.value = "";
  filterDateBefore.value = "";
  page.value = 1;
}

const reviewTypeLabels: Record<string, string> = {
  dlq_replay: "DLQ重放",
  sensitive_access: "敏感访问",
  high_cost_call: "高成本调用",
  restore_confirm: "恢复确认",
  memory_candidate: "记忆候选项",
  import_confirm: "导入确认",
  manual: "手动",
};
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader
      title="审核队列"
      subtitle="批准或拒绝敏感操作、DLQ重放和恢复操作"
    />

    <!-- Filters -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-end gap-3">
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">类型</label>
          <select
            v-model="filterReviewType"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部类型</option>
            <option value="dlq_replay">DLQ重放</option>
            <option value="sensitive_access">敏感访问</option>
            <option value="high_cost_call">高成本调用</option>
            <option value="restore_confirm">恢复确认</option>
            <option value="memory_candidate">记忆候选项</option>
            <option value="import_confirm">导入确认</option>
            <option value="manual">手动</option>
          </select>
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">状态</label>
          <select
            v-model="filterStatus"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部状态</option>
            <option value="pending">待处理</option>
            <option value="in_review">审核中</option>
            <option value="approved">已批准</option>
            <option value="rejected">已拒绝</option>
            <option value="cancelled">已取消</option>
            <option value="expired">已过期</option>
          </select>
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
          {{ data?.page_info?.total_items ?? 0 }} 条
        </span>
      </div>
    </div>

    <!-- Error -->
    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载审核项失败: {{ (error as Error)?.message }}
    </div>

    <!-- Table -->
    <DataTable
      :items="data?.items ?? []"
      :columns="columns"
      :loading="isLoading"
      empty-message="无审核项"
      clickable
      row-key="review_item_id"
      @row-click="openDetail"
    >
      <template #cell-created_at="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
      </template>

      <template #cell-review_type="{ value }">
        <StatusBadge
          :status="value as string"
          :label="reviewTypeLabels[value as string] || (value as string)"
        />
      </template>

      <template #cell-target_type="{ item }">
        <span class="text-xs text-surface-500">
          {{ (item as ReviewItem).target_type }}
        </span>
      </template>

      <template #cell-status="{ value }">
        <StatusBadge :status="value as string" :pulse="value === 'pending' || value === 'in_review'" />
      </template>

      <template #cell-priority="{ value }">
        <span
          :class="[
            'inline-flex items-center justify-center w-8 h-6 rounded text-2xs font-bold',
            Number(value) >= 200 ? 'bg-red-50 text-red-700' :
            Number(value) >= 100 ? 'bg-amber-50 text-amber-700' :
            'bg-surface-100 text-surface-500'
          ]"
        >
          {{ value }}
        </span>
      </template>
    </DataTable>

    <Pagination
      v-if="data?.page_info"
      :page-info="data.page_info"
      @page-change="(p: number) => (page = p)"
    />

    <!-- Detail Drawer with approve/reject actions -->
    <DetailDrawer :open="drawerOpen" title="审核项详情" @close="drawerOpen = false">
      <template v-if="selectedItem">
        <dl class="space-y-4">
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">审核ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-700 break-all">{{ selectedItem.review_item_id }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">类型</dt>
            <dd class="mt-1">
              <StatusBadge
                :status="selectedItem.review_type"
                :label="reviewTypeLabels[selectedItem.review_type] || selectedItem.review_type"
              />
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">状态</dt>
            <dd class="mt-1">
              <StatusBadge
                :status="selectedItem.status"
                :pulse="selectedItem.status === 'pending' || selectedItem.status === 'in_review'"
              />
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">目标</dt>
            <dd class="mt-1 text-sm">
              <span class="font-medium">{{ selectedItem.target_type }}</span>
              <span class="font-mono text-xs text-surface-400 ml-1">{{ selectedItem.target_id.slice(0, 16) }}…</span>
            </dd>
          </div>
          <div v-if="selectedItem.target_version != null">
            <dt class="text-2xs font-semibold uppercase text-surface-400">目标版本</dt>
            <dd class="mt-1 text-sm font-mono">{{ selectedItem.target_version }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">优先级</dt>
            <dd class="mt-1 text-sm">
              <span
                :class="[
                  'inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-semibold',
                  (selectedItem.priority >= 200) ? 'bg-red-50 text-red-700' :
                  (selectedItem.priority >= 100) ? 'bg-amber-50 text-amber-700' :
                  'bg-surface-100 text-surface-500'
                ]"
              >{{ selectedItem.priority }}</span>
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">请求者</dt>
            <dd class="mt-1 text-sm">
              {{ selectedItem.requester_actor_type }}
              <span v-if="selectedItem.requester_actor_id" class="font-mono text-xs text-surface-400">
                ({{ selectedItem.requester_actor_id.slice(0, 12) }}…)
              </span>
            </dd>
          </div>
          <div v-if="selectedItem.reviewer_id">
            <dt class="text-2xs font-semibold uppercase text-surface-400">审核人</dt>
            <dd class="mt-1 font-mono text-xs text-surface-600">{{ selectedItem.reviewer_id.slice(0, 16) }}…</dd>
          </div>
          <div v-if="selectedItem.decided_at">
            <dt class="text-2xs font-semibold uppercase text-surface-400">决定时间</dt>
            <dd class="mt-1 text-sm">{{ formatTime(selectedItem.decided_at) }}</dd>
          </div>
          <div v-if="selectedItem.decision">
            <dt class="text-2xs font-semibold uppercase text-surface-400">决定</dt>
            <dd class="mt-1">
              <StatusBadge :status="selectedItem.decision" />
            </dd>
          </div>
          <div v-if="selectedItem.reason">
            <dt class="text-2xs font-semibold uppercase text-surface-400">原因</dt>
            <dd class="mt-1 text-sm text-surface-600">{{ selectedItem.reason }}</dd>
          </div>
          <div v-if="selectedItem.expires_at">
            <dt class="text-2xs font-semibold uppercase text-surface-400">过期时间</dt>
            <dd class="mt-1 text-sm">{{ formatTime(selectedItem.expires_at) }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt>
            <dd class="mt-1 text-sm">{{ formatTime(selectedItem.created_at) }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">关联ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ selectedItem.correlation_id }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">请求ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ selectedItem.request_id }}</dd>
          </div>
        </dl>
      </template>

      <!-- Approve / Reject actions -->
      <template v-if="selectedItem && canAct(selectedItem.status)" #footer>
        <div class="space-y-3">
          <div
            v-if="actionError"
            class="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
          >
            {{ actionError }}
          </div>

          <div>
            <label class="block text-2xs font-semibold uppercase text-surface-400 mb-1"
              >决定原因</label
            >
            <textarea
              v-model="actionReason"
              rows="2"
              placeholder="请输入决定原因..."
              class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 resize-none"
            ></textarea>
          </div>

          <div class="flex gap-2">
            <button
              class="btn btn-success flex-1 btn-sm"
              :disabled="approveMutation.isPending.value || rejectMutation.isPending.value"
              @click="handleApprove"
            >
              <svg v-if="approveMutation.isPending.value" class="h-3.5 w-3.5 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>
              <svg v-else xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>
              {{ approveMutation.isPending.value ? "审批中..." : "批准" }}
            </button>
            <button
              class="btn btn-danger flex-1 btn-sm"
              :disabled="approveMutation.isPending.value || rejectMutation.isPending.value"
              @click="handleReject"
            >
              <svg v-if="rejectMutation.isPending.value" class="h-3.5 w-3.5 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>
              <svg v-else xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
              {{ rejectMutation.isPending.value ? "拒绝中..." : "拒绝" }}
            </button>
          </div>
        </div>
      </template>

      <!-- Final state indicator -->
      <template v-if="selectedItem && isFinal(selectedItem.status)" #footer>
        <div
          :class="[
            'rounded-lg px-4 py-3 text-sm font-medium text-center',
            selectedItem.status === 'approved' ? 'bg-emerald-50 text-emerald-700' :
            selectedItem.status === 'rejected' ? 'bg-red-50 text-red-700' :
            'bg-surface-100 text-surface-500'
          ]"
        >
          此审核项已被 <strong>{{ selectedItem.status }}</strong>
          <span v-if="selectedItem.decided_at">，时间: {{ formatTime(selectedItem.decided_at) }}</span>
        </div>
      </template>
    </DetailDrawer>
  </div>
</template>
