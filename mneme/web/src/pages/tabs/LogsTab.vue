<script setup lang="ts">
/**
 * LogsTab — API 调用日志筛选与查看
 *
 * 对接 GET /api/v4/admin/logs 端点，支持：
 *   - level   → call_state（快捷芯片 + 下拉）
 *   - source  → actor_type 下拉 + provider/关键词搜索
 *   - since / until → 时间范围 + 快捷预设
 *   - call_type → chat / embedding / completion 下拉
 */
import { ref, computed } from "vue";
import { useQuery } from "@tanstack/vue-query";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import { fetchAdminLogs } from "@/api/client";
import type { AdminLogEntry } from "@/types";

const page = ref(1);
const pageSize = ref(50);
const filterLevel = ref("");
const filterActorType = ref("");
const filterSource = ref("");
const filterCallType = ref("");
const filterSince = ref("");
const filterUntil = ref("");
const filterSearch = ref("");

const selectedItem = ref<AdminLogEntry | null>(null);
const drawerOpen = ref(false);

// ── active filter count ──
const activeFilterCount = computed(() => {
  let n = 0;
  if (filterLevel.value) n++;
  if (filterActorType.value) n++;
  if (filterSource.value) n++;
  if (filterCallType.value) n++;
  if (filterSince.value || filterUntil.value) n++;
  if (filterSearch.value) n++;
  return n;
});

// ── computed source param: actor_type dropdown takes priority ──
const effectiveSource = computed(() => {
  return filterActorType.value || filterSource.value || filterSearch.value || undefined;
});

const queryKey = computed(() => [
  "admin-logs", page.value, pageSize.value,
  filterLevel.value, effectiveSource.value, filterCallType.value,
  filterSince.value, filterUntil.value,
] as const);

const { data, isLoading, isError, error } = useQuery({
  queryKey,
  queryFn: () =>
    fetchAdminLogs({
      page: page.value,
      page_size: pageSize.value,
      level: filterLevel.value || undefined,
      source: effectiveSource.value,
      call_type: filterCallType.value || undefined,
      since: filterSince.value
        ? (filterSince.value.includes("T")
            ? new Date(filterSince.value).toISOString()
            : filterSince.value + "T00:00:00.000Z")
        : undefined,
      until: filterUntil.value
        ? (filterUntil.value.includes("T")
            ? new Date(filterUntil.value).toISOString()
            : filterUntil.value + "T23:59:59.999Z")
        : undefined,
    }),
  placeholderData: (prev) => prev,
});

const columns = [
  { key: "created_at", label: "时间", width: "170px" },
  { key: "call_type", label: "调用类型", width: "110px" },
  { key: "call_state", label: "状态", width: "110px" },
  { key: "actor_type", label: "来源", width: "100px" },
  { key: "total_tokens", label: "Tokens", width: "100px" },
  { key: "latency_ms", label: "延迟", width: "90px" },
  { key: "error_message", label: "错误信息" },
];

function openDetail(item: AdminLogEntry) {
  selectedItem.value = item;
  drawerOpen.value = true;
}

function formatTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function formatLatency(ms: number | null): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function clearFilters() {
  filterLevel.value = "";
  filterActorType.value = "";
  filterSource.value = "";
  filterCallType.value = "";
  filterSince.value = "";
  filterUntil.value = "";
  filterSearch.value = "";
  page.value = 1;
}

function quickFilter(level: string) {
  filterLevel.value = filterLevel.value === level ? "" : level;
  page.value = 1;
}

// ── Time presets ──
function applyTimePreset(preset: string) {
  const now = new Date();
  filterUntil.value = "";

  switch (preset) {
    case "1h": {
      const d = new Date(now.getTime() - 60 * 60 * 1000);
      filterSince.value = d.toISOString().slice(0, 16);
      filterUntil.value = now.toISOString().slice(0, 16);
      break;
    }
    case "24h": {
      const d = new Date(now.getTime() - 24 * 60 * 60 * 1000);
      filterSince.value = d.toISOString().slice(0, 16);
      filterUntil.value = now.toISOString().slice(0, 16);
      break;
    }
    case "7d": {
      const d = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
      filterSince.value = d.toISOString().slice(0, 16);
      filterUntil.value = now.toISOString().slice(0, 16);
      break;
    }
    case "30d": {
      const d = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
      filterSince.value = d.toISOString().slice(0, 16);
      filterUntil.value = now.toISOString().slice(0, 16);
      break;
    }
    case "today": {
      const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      filterSince.value = today.toISOString().slice(0, 16);
      filterUntil.value = now.toISOString().slice(0, 16);
      break;
    }
    default:
      break;
  }
  page.value = 1;
}

function isTimePresetActive(preset: string): boolean {
  if (!filterSince.value) return false;
  const now = new Date();
  const since = new Date(filterSince.value);
  switch (preset) {
    case "1h": return now.getTime() - since.getTime() <= 60 * 60 * 1000 + 60000;
    case "24h": return now.getTime() - since.getTime() <= 24 * 60 * 60 * 1000 + 60000;
    case "7d": return now.getTime() - since.getTime() <= 7 * 24 * 60 * 60 * 1000 + 60000;
    case "30d": return now.getTime() - since.getTime() <= 30 * 24 * 60 * 60 * 1000 + 60000;
    default: return false;
  }
}

const QUICK_FILTERS = [
  { key: "succeeded", label: "✅ 成功", activeClass: "bg-emerald-100 text-emerald-700 border-emerald-300" },
  { key: "failed", label: "❌ 失败", activeClass: "bg-red-100 text-red-700 border-red-300" },
  { key: "timeout", label: "⏱ 超时", activeClass: "bg-amber-100 text-amber-700 border-amber-300" },
  { key: "denied", label: "🚫 被拒", activeClass: "bg-orange-100 text-orange-700 border-orange-300" },
  { key: "in_flight", label: "🔄 执行中", activeClass: "bg-blue-100 text-blue-700 border-blue-300" },
];

const ACTOR_TYPES = [
  { value: "", label: "全部来源" },
  { value: "user", label: "👤 用户" },
  { value: "agent", label: "🤖 Agent" },
  { value: "system", label: "⚙️ 系统" },
  { value: "service", label: "🔌 服务" },
];

const TIME_PRESETS = [
  { key: "1h", label: "最近1小时" },
  { key: "24h", label: "最近24小时" },
  { key: "today", label: "今天" },
  { key: "7d", label: "最近7天" },
  { key: "30d", label: "最近30天" },
];

const CALL_STATE_LABELS: Record<string, string> = {
  succeeded: "成功",
  failed: "失败",
  timeout: "超时",
  cancelled: "已取消",
  denied: "被拒",
  dead_letter: "死信",
  in_flight: "执行中",
  planned: "计划中",
  budget_reserved: "预算预留",
  credential_checked: "凭据已检查",
};

const CALL_TYPE_LABELS: Record<string, string> = {
  chat: "对话",
  completion: "补全",
  embedding: "嵌入",
  image: "图像",
  audio: "音频",
  rerank: "重排",
  ocr: "OCR",
  search: "搜索",
};
</script>

<template>
  <div>
    <!-- ════════════════ Filter Bar ════════════════ -->
    <div class="card mb-6">
      <!-- Row 1: 级别 — 快捷过滤芯片 + 总览 -->
      <div class="px-4 pt-4 pb-2 flex flex-wrap items-center gap-2 border-b border-surface-100">
        <span class="text-2xs font-bold uppercase text-surface-400 tracking-wider shrink-0">级别</span>
        <button
          v-for="qf in QUICK_FILTERS"
          :key="qf.key"
          class="px-3 py-1 text-xs rounded-full border transition-colors"
          :class="filterLevel === qf.key ? qf.activeClass : 'border-surface-200 text-surface-500 hover:border-surface-300 hover:text-surface-700'"
          @click="quickFilter(qf.key)"
        >
          {{ qf.label }}
        </button>
        <select
          v-if="filterLevel"
          v-model="filterLevel"
          class="ml-1 rounded-lg border border-surface-200 bg-white px-2.5 py-1 text-xs text-surface-700 focus:border-brand-500 focus:outline-none"
        >
          <option value="">全部状态 ×</option>
          <option value="succeeded">成功</option>
          <option value="failed">失败</option>
          <option value="timeout">超时</option>
          <option value="cancelled">已取消</option>
          <option value="denied">被拒</option>
          <option value="dead_letter">死信</option>
          <option value="in_flight">执行中</option>
          <option value="planned">计划中</option>
          <option value="budget_reserved">预算预留</option>
          <option value="credential_checked">凭据已检查</option>
        </select>
        <span class="flex-1"></span>
        <span class="text-xs text-surface-400 font-mono">
          {{ data?.page_info?.total_items?.toLocaleString() ?? "—" }} 条日志
        </span>
      </div>

      <!-- Row 2: 来源 + 时间 + 类型 — 主要筛选控件 -->
      <div class="px-4 py-3 flex flex-wrap items-end gap-3">
        <!-- 来源 -->
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">来源</label>
          <select
            v-model="filterActorType"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 min-w-[120px]"
          >
            <option v-for="at in ACTOR_TYPES" :key="at.value" :value="at.value">{{ at.label }}</option>
          </select>
        </div>

        <!-- Provider / 关键词 -->
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">Provider / ID</label>
          <input
            v-model="filterSource"
            type="text"
            placeholder="provider UUID…"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 w-40"
          />
        </div>

        <!-- 关键词搜索 -->
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">关键词</label>
          <input
            v-model="filterSearch"
            type="text"
            placeholder="ID / 关键词…"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 w-36"
          />
        </div>

        <!-- 调用类型 -->
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">调用类型</label>
          <select
            v-model="filterCallType"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部类型</option>
            <option value="chat">对话</option>
            <option value="completion">补全</option>
            <option value="embedding">嵌入</option>
            <option value="image">图像</option>
            <option value="audio">音频</option>
            <option value="rerank">重排</option>
            <option value="ocr">OCR</option>
            <option value="search">搜索</option>
          </select>
        </div>

        <!-- 分隔 -->
        <div class="w-px h-8 bg-surface-200 hidden lg:block"></div>

        <!-- 开始时间 -->
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">开始时间</label>
          <input
            v-model="filterSince"
            type="datetime-local"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          />
        </div>

        <!-- 结束时间 -->
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">结束时间</label>
          <input
            v-model="filterUntil"
            type="datetime-local"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          />
        </div>

        <!-- 清除 -->
        <button
          v-if="activeFilterCount > 0"
          class="btn btn-ghost btn-sm text-red-500 hover:text-red-700"
          @click="clearFilters"
        >
          清除 {{ activeFilterCount }} 项筛选
        </button>
      </div>

      <!-- Row 3: 时间快捷预设 -->
      <div class="px-4 pb-3 flex flex-wrap items-center gap-1.5">
        <span class="text-2xs font-semibold uppercase text-surface-400 mr-1 shrink-0">时间范围</span>
        <button
          v-for="preset in TIME_PRESETS"
          :key="preset.key"
          class="px-2.5 py-1 text-2xs rounded-full border transition-colors"
          :class="isTimePresetActive(preset.key)
            ? 'bg-brand-100 text-brand-700 border-brand-300'
            : 'border-surface-200 text-surface-500 hover:border-surface-300 hover:text-surface-700'"
          @click="applyTimePreset(preset.key)"
        >
          {{ preset.label }}
        </button>
        <span v-if="!filterSince && !filterUntil" class="text-2xs text-surface-400">— 未设置时间过滤</span>
      </div>
    </div>

    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载日志失败: {{ (error as Error)?.message }}
    </div>

    <DataTable
      :items="data?.items ?? []"
      :columns="columns"
      :loading="isLoading"
      :empty-message="isError ? '数据加载出错' : '未找到 API 调用日志'"
      clickable
      row-key="api_call_log_id"
      @row-click="openDetail"
    >
      <template #cell-created_at="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
      </template>

      <template #cell-call_type="{ value }">
        <span class="badge text-2xs bg-indigo-50 text-indigo-600">
          {{ CALL_TYPE_LABELS[value as string] ?? value }}
        </span>
      </template>

      <template #cell-call_state="{ value }">
        <StatusBadge :status="value as string" :label="CALL_STATE_LABELS[value as string] ?? (value as string)" />
      </template>

      <template #cell-actor_type="{ value }">
        <span class="badge text-2xs" :class="value === 'agent' ? 'bg-violet-50 text-violet-700' : value === 'user' ? 'bg-blue-50 text-blue-700' : 'bg-surface-100 text-surface-500'">
          {{ value }}
        </span>
      </template>

      <template #cell-total_tokens="{ value }">
        <span class="font-mono text-xs text-surface-600 tabular-nums">
          {{ value != null ? (value as number).toLocaleString() : '—' }}
        </span>
      </template>

      <template #cell-latency_ms="{ value }">
        <span
          class="font-mono text-xs tabular-nums"
          :class="{
            'text-emerald-600': (value as number) < 500,
            'text-amber-600': (value as number) >= 500 && (value as number) < 2000,
            'text-red-600': (value as number) >= 2000,
            'text-surface-400': value == null,
          }"
        >
          {{ formatLatency(value as number | null) }}
        </span>
      </template>

      <template #cell-error_message="{ value }">
        <span class="text-xs text-red-500 truncate max-w-[200px] inline-block">
          {{ value || '—' }}
        </span>
      </template>
    </DataTable>

    <Pagination
      v-if="data?.page_info"
      :page-info="data.page_info"
      @page-change="(p: number) => (page = p)"
    />

    <DetailDrawer :open="drawerOpen" title="API 调用详情" @close="drawerOpen = false">
      <template v-if="selectedItem">
        <dl class="space-y-4">
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">日志 ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-700 break-all">{{ selectedItem.api_call_log_id }}</dd>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">调用类型</dt>
              <dd class="mt-1">
                <span class="badge text-2xs bg-indigo-50 text-indigo-600">
                  {{ CALL_TYPE_LABELS[selectedItem.call_type] ?? selectedItem.call_type }}
                </span>
              </dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">状态</dt>
              <dd class="mt-1">
                <StatusBadge :status="selectedItem.call_state" :label="CALL_STATE_LABELS[selectedItem.call_state] ?? selectedItem.call_state" />
              </dd>
            </div>
          </div>

          <div class="grid grid-cols-3 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">输入 Tokens</dt>
              <dd class="mt-1 font-mono text-sm text-surface-700 tabular-nums">
                {{ selectedItem.input_tokens?.toLocaleString() ?? '—' }}
              </dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">输出 Tokens</dt>
              <dd class="mt-1 font-mono text-sm text-surface-700 tabular-nums">
                {{ selectedItem.output_tokens?.toLocaleString() ?? '—' }}
              </dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">总计 Tokens</dt>
              <dd class="mt-1 font-mono text-sm text-surface-700 tabular-nums font-semibold">
                {{ selectedItem.total_tokens?.toLocaleString() ?? '—' }}
              </dd>
            </div>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">延迟</dt>
              <dd class="mt-1 font-mono text-sm text-surface-700">{{ formatLatency(selectedItem.latency_ms) }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">重试次数</dt>
              <dd class="mt-1 font-mono text-sm text-surface-700">{{ selectedItem.retry_count }}</dd>
            </div>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">操作者类型</dt>
              <dd class="mt-1 text-sm text-surface-700">{{ selectedItem.actor_type }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">Provider</dt>
              <dd class="mt-1 font-mono text-xs text-surface-500">
                {{ selectedItem.provider_id?.slice(0, 12) ?? '—' }}
              </dd>
            </div>
          </div>

          <div v-if="selectedItem.error_code || selectedItem.error_message">
            <dt class="text-2xs font-semibold uppercase text-surface-400">错误</dt>
            <dd class="mt-1">
              <span v-if="selectedItem.error_code" class="font-mono text-xs text-red-600 mr-2">{{ selectedItem.error_code }}</span>
              <span class="text-sm text-red-600">{{ selectedItem.error_message }}</span>
            </dd>
          </div>

          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">请求 ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ selectedItem.request_id || '—' }}</dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">关联 ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ selectedItem.correlation_id || '—' }}</dd>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">开始时间</dt>
              <dd class="mt-1 text-xs">{{ formatTime(selectedItem.started_at) }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">完成时间</dt>
              <dd class="mt-1 text-xs">{{ formatTime(selectedItem.finished_at) }}</dd>
            </div>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt>
            <dd class="mt-1 text-xs">{{ formatTime(selectedItem.created_at) }}</dd>
          </div>
        </dl>
      </template>
    </DetailDrawer>
  </div>
</template>
