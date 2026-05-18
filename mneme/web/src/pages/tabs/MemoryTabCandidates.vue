<script setup lang="ts">
defineOptions({ name: "MemoryTabCandidates" });
import { ref, computed } from "vue";
import { useQuery, useMutation, useQueryClient } from "@tanstack/vue-query";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import {
  fetchCandidates,
  fetchCandidate,
  approveCandidate,
  rejectCandidate,
} from "@/api/client";
import type { MemoryCandidate } from "@/types";

const queryClient = useQueryClient();

// ── List state ──
const candidatePage = ref(1);
const candidatePageSize = ref(50);
const candidateFilterStatus = ref("pending_review");
const candidateFilterProjectId = ref("");

// ── Detail drawer ──
const selectedCandidate = ref<MemoryCandidate | null>(null);
const drawerOpen = ref(false);
const actionReason = ref("");
const actionError = ref("");

// ── List query ──
const candKey = computed(() => [
  "candidates",
  candidatePage.value,
  candidatePageSize.value,
  candidateFilterStatus.value,
  candidateFilterProjectId.value,
] as const);

const { data: candData, isLoading: candLoading, isError: candError, error: candErr } = useQuery({
  queryKey: candKey,
  queryFn: () =>
    fetchCandidates({
      page: candidatePage.value,
      page_size: candidatePageSize.value,
      candidate_status: candidateFilterStatus.value || undefined,
      project_id: candidateFilterProjectId.value || undefined,
    }),
  placeholderData: (prev) => prev,
});

// ── Detail query ──
const candId = computed(() => selectedCandidate.value?.candidate_id ?? null);

const { data: candDetail, isLoading: detailLoading } = useQuery({
  queryKey: ["candidate-detail", candId],
  queryFn: () => fetchCandidate(candId.value!),
  enabled: computed(() => drawerOpen.value && !!candId.value),
});

// ── Mutations ──
const approveMutation = useMutation({
  mutationFn: ({ id, reason }: { id: string; reason?: string }) => approveCandidate(id, reason),
  onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ["candidates"] });
    queryClient.invalidateQueries({ queryKey: ["memories"] });
    drawerOpen.value = false;
    actionError.value = "";
  },
  onError: (err: Error) => {
    actionError.value = err.message || "批准失败";
  },
});

const rejectMutation = useMutation({
  mutationFn: ({ id, reason }: { id: string; reason?: string }) => rejectCandidate(id, reason),
  onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ["candidates"] });
    drawerOpen.value = false;
    actionError.value = "";
  },
  onError: (err: Error) => {
    actionError.value = err.message || "拒绝失败";
  },
});

// ── Helpers ──
function openDetail(cand: MemoryCandidate) {
  selectedCandidate.value = cand;
  actionReason.value = "";
  actionError.value = "";
  drawerOpen.value = true;
}

function closeDrawer() {
  drawerOpen.value = false;
  selectedCandidate.value = null;
  actionReason.value = "";
  actionError.value = "";
}

function handleApprove() {
  if (!selectedCandidate.value) return;
  actionError.value = "";
  approveMutation.mutate({
    id: selectedCandidate.value.candidate_id,
    reason: actionReason.value || undefined,
  });
}

function handleReject() {
  if (!selectedCandidate.value) return;
  actionError.value = "";
  rejectMutation.mutate({
    id: selectedCandidate.value.candidate_id,
    reason: actionReason.value || undefined,
  });
}

function clearFilters() {
  candidateFilterStatus.value = "pending_review";
  candidateFilterProjectId.value = "";
  candidatePage.value = 1;
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString();
}

function truncate(text: string, max: number): string {
  if (!text) return "";
  return text.length > max ? text.slice(0, max) + "…" : text;
}

function sourceTypeLabel(type: string): string {
  const map: Record<string, string> = {
    message: "消息",
    raw_event: "原始事件",
    manual: "手动",
    importer: "导入",
    agent_submission: "Agent提交",
    candidate: "候选",
    asset: "资产",
    document: "文档",
    block: "块",
  };
  return map[type] ?? type;
}

function canAct(status: string): boolean {
  return status === "pending_review" || status === "conflict";
}

const candColumns = [
  { key: "title", label: "标题" },
  { key: "source_type", label: "来源", width: "90px" },
  { key: "candidate_status", label: "状态", width: "100px" },
  { key: "confidence_score", label: "置信度", width: "80px" },
  { key: "created_at", label: "提交时间", width: "160px" },
];
</script>

<template>
  <div class="space-y-6">
    <!-- ── Filters ── -->
    <div class="card p-4">
      <div class="flex flex-wrap items-end gap-3">
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">项目ID</label>
          <input
            v-model="candidateFilterProjectId"
            type="text"
            placeholder="UUID..."
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm font-mono text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 w-64"
          />
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">状态</label>
          <select
            v-model="candidateFilterStatus"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部</option>
            <option value="pending_review">待审核</option>
            <option value="approved">已批准</option>
            <option value="rejected">已拒绝</option>
            <option value="superseded">已替代</option>
            <option value="conflict">冲突</option>
          </select>
        </div>

        <button class="btn btn-ghost btn-sm" @click="clearFilters">清除筛选</button>

        <span class="text-xs text-surface-400 ml-auto self-end pb-1">
          {{ candData?.page_info?.total_items ?? 0 }} 条候选
        </span>
      </div>
    </div>

    <!-- Error -->
    <div
      v-if="candError"
      class="card border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载候选列表失败: {{ (candErr as Error)?.message }}
    </div>

    <!-- Candidate Table -->
    <DataTable
      :items="candData?.items ?? []"
      :columns="candColumns"
      :loading="candLoading"
      :empty-message="'暂无候选记忆'"
      clickable
      row-key="candidate_id"
      @row-click="openDetail"
    >
      <template #cell-title="{ item }">
        <div class="max-w-xs">
          <div class="text-sm font-medium text-surface-800 truncate">
            {{ (item as MemoryCandidate).title || "无标题" }}
          </div>
          <div class="text-2xs text-surface-400 truncate mt-0.5">
            {{ truncate((item as MemoryCandidate).candidate_text, 80) }}
          </div>
        </div>
      </template>

      <template #cell-source_type="{ value }">
        <span class="text-xs text-surface-500">{{ sourceTypeLabel(value as string) }}</span>
      </template>

      <template #cell-candidate_status="{ value }">
        <StatusBadge :status="value as string" />
      </template>

      <template #cell-confidence_score="{ item }">
        <span class="font-mono text-xs" :class="(item as MemoryCandidate).confidence_score != null ? 'text-surface-700' : 'text-surface-400'">
          {{ (item as MemoryCandidate).confidence_score != null
            ? ((item as MemoryCandidate).confidence_score! * 100).toFixed(1) + '%'
            : '—' }}
        </span>
      </template>

      <template #cell-created_at="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
      </template>
    </DataTable>

    <Pagination
      v-if="candData?.page_info"
      :page-info="candData.page_info"
      @page-change="(p: number) => (candidatePage = p)"
    />

    <!-- Candidate quick actions (for pending_review) -->
    <div v-if="(candData?.items ?? []).filter((c: MemoryCandidate) => c.candidate_status === 'pending_review').length > 0" class="card p-4">
      <h4 class="text-sm font-semibold text-surface-700 mb-3">待审核候选 (快速操作)</h4>
      <div class="space-y-2">
        <div
          v-for="c in (candData?.items ?? []).filter((c: MemoryCandidate) => c.candidate_status === 'pending_review')"
          :key="c.candidate_id"
          class="flex items-center justify-between rounded-lg border border-surface-200 p-3"
        >
          <div class="min-w-0 flex-1 mr-4">
            <div class="text-sm font-medium text-surface-800 truncate">{{ c.title || "无标题" }}</div>
            <div class="text-2xs text-surface-400 truncate">{{ truncate(c.candidate_text, 100) }}</div>
            <div class="text-2xs text-surface-500 mt-0.5">
              置信度: {{ c.confidence_score != null ? (c.confidence_score * 100).toFixed(1) + '%' : '—' }}
              · 来源: {{ sourceTypeLabel(c.source_type) }}
            </div>
          </div>
          <div class="flex items-center gap-1.5 shrink-0">
            <button
              class="btn btn-xs bg-emerald-50 text-emerald-700 hover:bg-emerald-100 border border-emerald-200"
              @click="approveMutation.mutate({ id: c.candidate_id })"
              :disabled="!!approveMutation.isPending"
            >
              批准
            </button>
            <button
              class="btn btn-xs bg-red-50 text-red-700 hover:bg-red-100 border border-red-200"
              @click="rejectMutation.mutate({ id: c.candidate_id })"
              :disabled="!!rejectMutation.isPending"
            >
              拒绝
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- ══════════════════════════════════════════════════════ -->
    <!-- Candidate Detail Drawer -->
    <!-- ══════════════════════════════════════════════════════ -->
    <DetailDrawer
      :open="drawerOpen"
      :title="selectedCandidate?.title ?? '候选详情'"
      width="w-[640px] max-w-full"
      @close="closeDrawer"
    >
      <template v-if="selectedCandidate">
        <dl class="space-y-3">
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">候选ID</dt>
            <dd class="mt-0.5 font-mono text-xs text-surface-700 break-all">{{ selectedCandidate.candidate_id }}</dd>
          </div>
          <div v-if="selectedCandidate.project_id">
            <dt class="text-2xs font-semibold uppercase text-surface-400">项目ID</dt>
            <dd class="mt-0.5 font-mono text-xs text-surface-600">{{ selectedCandidate.project_id }}</dd>
          </div>
          <div class="flex gap-6">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">状态</dt>
              <dd class="mt-0.5"><StatusBadge :status="selectedCandidate.candidate_status" /></dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">来源</dt>
              <dd class="mt-0.5 text-sm text-surface-600">{{ sourceTypeLabel(selectedCandidate.source_type) }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">置信度</dt>
              <dd class="mt-0.5 font-mono text-sm">
                {{ selectedCandidate.confidence_score != null
                  ? (selectedCandidate.confidence_score * 100).toFixed(1) + '%'
                  : '—' }}
              </dd>
            </div>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">敏感等级</dt>
            <dd class="mt-0.5 text-sm text-surface-600">{{ selectedCandidate.sensitivity_level }}</dd>
          </div>
          <div v-if="selectedCandidate.source_id">
            <dt class="text-2xs font-semibold uppercase text-surface-400">来源ID</dt>
            <dd class="mt-0.5 font-mono text-xs text-surface-600 break-all">{{ selectedCandidate.source_id }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">提交者</dt>
            <dd class="mt-0.5 text-sm text-surface-600">
              {{ selectedCandidate.submitted_by_actor_type }}
              <span v-if="selectedCandidate.submitted_by_actor_id" class="font-mono text-xs text-surface-400 ml-1">
                ({{ selectedCandidate.submitted_by_actor_id.slice(0, 12) }}…)
              </span>
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">需审核</dt>
            <dd class="mt-0.5 text-sm text-surface-600">{{ selectedCandidate.review_required ? '是' : '否' }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">候选哈希</dt>
            <dd class="mt-0.5 font-mono text-2xs text-surface-400">{{ selectedCandidate.candidate_hash.slice(0, 24) }}…</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">提交时间</dt>
            <dd class="mt-0.5 text-sm">{{ formatTime(selectedCandidate.created_at) }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">更新时间</dt>
            <dd class="mt-0.5 text-sm">{{ formatTime(selectedCandidate.updated_at) }}</dd>
          </div>
        </dl>

        <div class="border-t border-surface-200 pt-4 mt-4">
          <h4 class="text-xs font-semibold uppercase text-surface-400 mb-2">候选内容</h4>
          <div class="rounded-lg border border-surface-200 bg-surface-50 p-4 text-sm text-surface-700 leading-relaxed whitespace-pre-wrap max-h-96 overflow-y-auto">
            {{ selectedCandidate.candidate_text }}
          </div>
        </div>

        <!-- Metadata -->
        <div v-if="selectedCandidate.metadata_json && Object.keys(selectedCandidate.metadata_json).length > 0" class="border-t border-surface-200 pt-4 mt-4">
          <h4 class="text-xs font-semibold uppercase text-surface-400 mb-2">元数据</h4>
          <pre class="rounded-lg border border-surface-200 bg-surface-50 p-3 text-2xs text-surface-600 overflow-x-auto">{{ JSON.stringify(selectedCandidate.metadata_json, null, 2) }}</pre>
        </div>
      </template>

      <!-- Approve / Reject actions for actionable candidates -->
      <template v-if="selectedCandidate && canAct(selectedCandidate.candidate_status)" #footer>
        <div class="space-y-3">
          <div
            v-if="actionError"
            class="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
          >
            {{ actionError }}
          </div>

          <div>
            <label class="block text-2xs font-semibold uppercase text-surface-400 mb-1">决定原因</label>
            <textarea
              v-model="actionReason"
              rows="2"
              placeholder="请输入决定原因（可选）..."
              class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 resize-none"
            ></textarea>
          </div>

          <div class="flex gap-2">
            <button
              class="btn btn-success flex-1 btn-sm"
              :disabled="!!approveMutation.isPending || !!rejectMutation.isPending"
              @click="handleApprove"
            >
              {{ approveMutation.isPending ? "批准中..." : "批准为记忆" }}
            </button>
            <button
              class="btn btn-danger flex-1 btn-sm"
              :disabled="!!approveMutation.isPending || !!rejectMutation.isPending"
              @click="handleReject"
            >
              {{ rejectMutation.isPending ? "拒绝中..." : "拒绝" }}
            </button>
          </div>
        </div>
      </template>
    </DetailDrawer>
  </div>
</template>
