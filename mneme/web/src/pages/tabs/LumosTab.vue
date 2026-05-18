<script setup lang="ts">
/**
 * LumosTab — 工具箱 (Agent 任务执行监控 + 本地模型注册 + 一键备份)
 *
 * 功能区块：
 *   1. 快捷操作栏：本地模型注册(内嵌对话框) / 新建Agent / 触发备份
 *   2. 统计卡片：总调用 / Token用量 / 估算成本 / 成功率
 *   3. 任务列表：Agent 工具调用记录（复用 api_call_logs）
 *   4. 本地模型快速注册对话框
 *
 * 数据来源：
 *   - Agent 任务记录 → GET /api/v4/admin/logs (api_call_logs)
 *   - Agent 列表 → GET /api/v4/agents
 *   - 备份触发 → POST /api/v4/admin/backup
 *   - 本地模型注册 → POST /api/v4/gateway/providers + POST /api/v4/gateway/providers/{id}/models
 */
import { ref, computed } from "vue";
import { useQuery, useMutation, useQueryClient } from "@tanstack/vue-query";
import { useRouter } from "vue-router";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import {
  fetchAdminLogs,
  fetchAgents,
  triggerBackup,
  createGateProvider,
  createGateProviderModel,
  fetchGateProviders,
} from "@/api/client";
import type { AdminLogEntry, AgentRead } from "@/types";

const router = useRouter();
const queryClient = useQueryClient();

// ── Sub-view mode ──────────────────────────────────────────────────────
type ViewMode = "tasks" | "providers";
const viewMode = ref<ViewMode>("tasks");

const page = ref(1);
const pageSize = ref(25);
const filterAgentId = ref("");
const filterStatus = ref("");
const filterSince = ref("");
const filterUntil = ref("");

const selectedItem = ref<AdminLogEntry | null>(null);
const drawerOpen = ref(false);

// Fetch agents for dropdown
const { data: agentsData } = useQuery({
  queryKey: ["agents-lumos"],
  queryFn: () => fetchAgents({ page: 1, page_size: 200 }),
});

// Fetch providers for local model dropdown
const { data: providersData } = useQuery({
  queryKey: ["gate-providers-lumos"],
  queryFn: () => fetchGateProviders({ page: 1, page_size: 100 }),
});

const queryKey = computed(() => [
  "lumos-tasks", page.value, pageSize.value,
  filterAgentId.value, filterStatus.value,
  filterSince.value, filterUntil.value,
] as const);

const { data, isLoading, isError, error } = useQuery({
  queryKey,
  queryFn: () =>
    fetchAdminLogs({
      page: page.value,
      page_size: pageSize.value,
      level: filterStatus.value || undefined,
      source: filterAgentId.value || undefined,
      since: filterSince.value || undefined,
      until: filterUntil.value || undefined,
    }),
  placeholderData: (prev) => prev,
});

const columns = [
  { key: "created_at", label: "时间", width: "170px" },
  { key: "actor_type", label: "执行者", width: "100px" },
  { key: "call_type", label: "任务类型", width: "110px" },
  { key: "call_state", label: "状态", width: "110px" },
  { key: "total_tokens", label: "Token用量", width: "110px" },
  { key: "latency_ms", label: "耗时", width: "90px" },
  { key: "error_message", label: "备注" },
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
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60000)}m ${Math.floor((ms % 60000) / 1000)}s`;
}

function formatCost(tokens: number | null): string {
  if (tokens == null) return "—";
  // Rough estimate: $0.002/1K tokens
  const cost = (tokens / 1000) * 0.002;
  return `$${cost.toFixed(4)}`;
}

function clearFilters() {
  filterAgentId.value = "";
  filterStatus.value = "";
  filterSince.value = "";
  filterUntil.value = "";
  page.value = 1;
}

const CALL_STATE_LABELS: Record<string, string> = {
  succeeded: "✅ 成功",
  failed: "❌ 失败",
  timeout: "⏱ 超时",
  cancelled: "⊘ 取消",
  denied: "🚫 拒绝",
  dead_letter: "💀 死信",
  in_flight: "⏳ 执行中",
  planned: "📋 计划中",
  budget_reserved: "💰 预算预留",
  credential_checked: "🔑 凭据检查",
};

// ── Quick actions ──────────────────────────────────────────────────────────
const backupConfirmOpen = ref(false);
const backupMutation = useMutation({
  mutationFn: () => triggerBackup(),
  onSuccess: () => {
    backupConfirmOpen.value = false;
    queryClient.invalidateQueries({ queryKey: ["backups"] });
    queryClient.invalidateQueries({ queryKey: ["jobs"] });
  },
});

function navigateToAgents() {
  router.push("/app/agents");
}

function navigateToGateway() {
  router.push("/app/gateway");
}

function navigateToGatewayModels() {
  router.push("/app/gateway?tab=providers");
}

function switchToProviders() {
  viewMode.value = "providers";
}

function switchToTasks() {
  viewMode.value = "tasks";
}

// ── 本地模型快速注册（内嵌对话框）─────────────────────────────────────────
const localModelOpen = ref(false);
const localModelForm = ref({
  provider_name: "Local Ollama",
  endpoint_url: "http://localhost:11434/v1",
  model_name: "",
  model_type: "chat",
  context_window: 8192,
});
const localModelSubmitting = ref(false);
const localModelResult = ref<{ ok: boolean; msg: string } | null>(null);

function openLocalModelQuick() {
  localModelForm.value = {
    provider_name: "Local Ollama",
    endpoint_url: "http://localhost:11434/v1",
    model_name: "",
    model_type: "chat",
    context_window: 8192,
  };
  localModelResult.value = null;
  localModelOpen.value = true;
}

async function handleLocalModelRegister() {
  localModelSubmitting.value = true;
  localModelResult.value = null;
  try {
    // Step 1: Create provider
    const provider = await createGateProvider({
      provider_code: `local_${Date.now().toString(36)}`,
      name: localModelForm.value.provider_name,
      provider_type: "llm",
      endpoint_base: localModelForm.value.endpoint_url || null,
      config_json: { local: true, backend: "ollama", registered_from: "lumos" },
    });

    // Step 2: Create model under provider
    if (localModelForm.value.model_name) {
      await createGateProviderModel(provider.provider_id, {
        model_code: localModelForm.value.model_name.replace(/[^a-zA-Z0-9_-]/g, "_"),
        external_model_id: localModelForm.value.model_name,
        model_type: localModelForm.value.model_type,
        display_name: localModelForm.value.model_name,
        context_window_tokens: localModelForm.value.context_window,
        input_price_per_1k: 0,
        output_price_per_1k: 0,
        currency_code: "USD",
        sensitivity_ceiling: "normal",
      });
    }

    localModelResult.value = {
      ok: true,
      msg: `✅ Provider "${provider.name}" 已注册` + (localModelForm.value.model_name ? `，模型 "${localModelForm.value.model_name}" 已添加` : ""),
    };
    queryClient.invalidateQueries({ queryKey: ["gate-providers-lumos"] });
    queryClient.invalidateQueries({ queryKey: ["gate-providers"] });
    queryClient.invalidateQueries({ queryKey: ["gate-models"] });
  } catch (e: any) {
    localModelResult.value = { ok: false, msg: `❌ 注册失败: ${e?.message || e}` };
  } finally {
    localModelSubmitting.value = false;
  }
}

// ── Provider summary view ─────────────────────────────────────────────────
const PROMPT_PRESETS = [
  { label: "Ollama (本地)", name: "Local Ollama", url: "http://localhost:11434/v1", type: "llm" },
  { label: "vLLM (本地)", name: "Local vLLM", url: "http://localhost:8000/v1", type: "llm" },
  { label: "LM Studio (本地)", name: "LM Studio", url: "http://localhost:1234/v1", type: "llm" },
  { label: "LocalAI (本地)", name: "LocalAI", url: "http://localhost:8080/v1", type: "llm" },
  { label: "Text Embedding", name: "Local Embedding", url: "http://localhost:11434/v1", type: "embedding" },
];

function fillPreset(preset: typeof PROMPT_PRESETS[number]) {
  localModelForm.value.provider_name = preset.name;
  localModelForm.value.endpoint_url = preset.url;
  localModelForm.value.model_type = preset.type;
}

// Aggregate stats
const totalTokens = computed(() => {
  if (!data.value?.items) return 0;
  return data.value.items.reduce((sum, item) => sum + (item.total_tokens || 0), 0);
});

const totalCost = computed(() => {
  if (totalTokens.value === 0) return "$0.0000";
  const cost = (totalTokens.value / 1000) * 0.002;
  return `$${cost.toFixed(4)}`;
});

const avgLatency = computed(() => {
  if (!data.value?.items) return 0;
  const items = data.value.items.filter(i => i.latency_ms != null);
  if (items.length === 0) return 0;
  return Math.round(items.reduce((sum, i) => sum + (i.latency_ms || 0), 0) / items.length);
});

const successRate = computed(() => {
  if (!data.value?.items) return 0;
  const succeeded = data.value.items.filter(i => i.call_state === "succeeded").length;
  return data.value.items.length > 0 ? Math.round((succeeded / data.value.items.length) * 100) : 0;
});

// Auto-refresh
const autoRefresh = ref(false);
let _refreshTimer: ReturnType<typeof setInterval> | null = null;

function toggleAutoRefresh() {
  autoRefresh.value = !autoRefresh.value;
  if (autoRefresh.value) {
    _refreshTimer = setInterval(() => {
      queryClient.invalidateQueries({ queryKey: ["lumos-tasks"] });
    }, 10_000);
  } else {
    if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
  }
}
</script>

<template>
  <div>
    <!-- Quick Actions Toolbar -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-center gap-3">
        <span class="text-2xs font-semibold uppercase text-surface-400 mr-1">🔧 快捷操作</span>
        <button class="btn btn-secondary btn-sm" @click="navigateToGatewayModels">
          🏠 注册本地模型
        </button>
        <button class="btn btn-secondary btn-sm" @click="navigateToAgents">
          🤖 新建 Agent
        </button>
        <button
          class="btn btn-secondary btn-sm"
          :disabled="backupMutation.isPending.value"
          @click="backupConfirmOpen = true"
        >
          💾 {{ backupMutation.isPending.value ? "备份中…" : "触发备份" }}
        </button>
        <span class="flex-1"></span>
        <span class="text-2xs text-surface-400">
          平均延迟 {{ formatLatency(avgLatency) }} · 成功率 {{ successRate }}%
        </span>
      </div>
    </div>

    <!-- Stats Row -->
    <div class="grid grid-cols-4 gap-3 mb-6">
      <div class="card p-4 text-center">
        <p class="text-2xs font-semibold uppercase text-surface-400">总调用次数</p>
        <p class="mt-1 font-mono text-xl font-bold text-surface-800 tabular-nums">
          {{ data?.page_info?.total_items ?? 0 }}
        </p>
      </div>
      <div class="card p-4 text-center">
        <p class="text-2xs font-semibold uppercase text-surface-400">总 Token 用量</p>
        <p class="mt-1 font-mono text-xl font-bold text-surface-800 tabular-nums">
          {{ totalTokens.toLocaleString() }}
        </p>
      </div>
      <div class="card p-4 text-center">
        <p class="text-2xs font-semibold uppercase text-surface-400">估算成本</p>
        <p class="mt-1 font-mono text-xl font-bold text-amber-600 tabular-nums">
          {{ totalCost }}
        </p>
      </div>
      <div class="card p-4 text-center">
        <p class="text-2xs font-semibold uppercase text-surface-400">成功率</p>
        <p class="mt-1 font-mono text-xl font-bold tabular-nums"
          :class="successRate >= 90 ? 'text-emerald-600' : successRate >= 70 ? 'text-amber-600' : 'text-red-600'">
          {{ successRate }}%
        </p>
      </div>
    </div>

    <!-- Filters -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-end gap-3">
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">Agent</label>
          <select
            v-model="filterAgentId"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部 Agent</option>
            <option
              v-for="agent in agentsData?.items ?? []"
              :key="agent.agent_id"
              :value="agent.agent_id"
            >
              {{ agent.name }}
            </option>
          </select>
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">状态</label>
          <select
            v-model="filterStatus"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部状态</option>
            <option value="succeeded">成功</option>
            <option value="failed">失败</option>
            <option value="timeout">超时</option>
            <option value="in_flight">执行中</option>
          </select>
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">开始时间</label>
          <input
            v-model="filterSince"
            type="datetime-local"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          />
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">结束时间</label>
          <input
            v-model="filterUntil"
            type="datetime-local"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          />
        </div>

        <button class="btn btn-ghost btn-sm" @click="clearFilters">清除</button>
      </div>
    </div>

    <!-- Error -->
    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载工具调用记录失败: {{ (error as Error)?.message }}
    </div>

    <!-- Table -->
    <DataTable
      :items="data?.items ?? []"
      :columns="columns"
      :loading="isLoading"
      empty-message="未找到工具调用记录"
      clickable
      row-key="api_call_log_id"
      @row-click="openDetail"
    >
      <template #cell-created_at="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
      </template>

      <template #cell-actor_type="{ value }">
        <span
          class="badge text-2xs"
          :class="value === 'agent' ? 'bg-violet-50 text-violet-700' : 'bg-surface-100 text-surface-500'"
        >
          {{ value === 'agent' ? '🤖 Agent' : value }}
        </span>
      </template>

      <template #cell-call_type="{ value }">
        <span class="badge text-2xs bg-indigo-50 text-indigo-600">{{ value }}</span>
      </template>

      <template #cell-call_state="{ value }">
        <StatusBadge :status="value as string" :label="CALL_STATE_LABELS[value as string] ?? (value as string)" />
      </template>

      <template #cell-total_tokens="{ value }">
        <span class="font-mono text-xs text-surface-600 tabular-nums">
          {{ value != null ? (value as number).toLocaleString() : '—' }}
        </span>
      </template>

      <template #cell-latency_ms="{ value }">
        <span class="font-mono text-xs tabular-nums"
          :class="{
            'text-emerald-600': (value as number) < 1000,
            'text-amber-600': (value as number) >= 1000 && (value as number) < 5000,
            'text-red-600': (value as number) >= 5000,
            'text-surface-400': value == null,
          }">
          {{ formatLatency(value as number | null) }}
        </span>
      </template>

      <template #cell-error_message="{ value }">
        <span class="text-xs text-red-500 truncate max-w-[180px] inline-block">
          {{ value || '—' }}
        </span>
      </template>
    </DataTable>

    <Pagination
      v-if="data?.page_info"
      :page-info="data.page_info"
      @page-change="(p: number) => (page = p)"
    />

    <!-- Backup Confirm Dialog -->
    <DetailDrawer :open="backupConfirmOpen" title="触发数据库备份" width="w-[420px] max-w-full" @close="backupConfirmOpen = false">
      <div class="space-y-4">
        <div class="rounded-lg bg-brand-50 p-4 text-sm text-brand-700">
          <p class="font-medium">将使用 pg_dump 创建完整数据库备份。</p>
          <p class="mt-1 text-xs text-brand-600">备份以异步任务执行，可在「系统 → 任务队列」跟踪进度。</p>
        </div>
      </div>
      <template #footer>
        <div class="flex gap-2">
          <button class="btn btn-secondary flex-1 btn-sm" @click="backupConfirmOpen = false">取消</button>
          <button class="btn btn-primary flex-1 btn-sm" :disabled="backupMutation.isPending.value" @click="backupMutation.mutate()">
            {{ backupMutation.isPending.value ? "启动中…" : "开始备份" }}
          </button>
        </div>
      </template>
    </DetailDrawer>

    <!-- Detail Drawer -->
    <DetailDrawer :open="drawerOpen" title="工具调用详情" @close="drawerOpen = false">
      <template v-if="selectedItem">
        <dl class="space-y-4">
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">任务 ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-700 break-all">{{ selectedItem.api_call_log_id }}</dd>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">执行者类型</dt>
              <dd class="mt-1 text-sm">{{ selectedItem.actor_type }}</dd>
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
              <dd class="mt-1 font-mono text-sm tabular-nums">{{ selectedItem.input_tokens?.toLocaleString() ?? '—' }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">输出 Tokens</dt>
              <dd class="mt-1 font-mono text-sm tabular-nums">{{ selectedItem.output_tokens?.toLocaleString() ?? '—' }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">估算成本</dt>
              <dd class="mt-1 font-mono text-sm font-semibold text-amber-600 tabular-nums">
                {{ formatCost(selectedItem.total_tokens) }}
              </dd>
            </div>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">耗时</dt>
              <dd class="mt-1 font-mono text-sm">{{ formatLatency(selectedItem.latency_ms) }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">重试次数</dt>
              <dd class="mt-1 font-mono text-sm">{{ selectedItem.retry_count }}</dd>
            </div>
          </div>

          <div v-if="selectedItem.error_code || selectedItem.error_message">
            <dt class="text-2xs font-semibold uppercase text-surface-400">错误信息</dt>
            <dd class="mt-1">
              <span v-if="selectedItem.error_code" class="font-mono text-xs text-red-600 mr-2">{{ selectedItem.error_code }}</span>
              <span class="text-sm text-red-600">{{ selectedItem.error_message }}</span>
            </dd>
          </div>

          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">时间线</dt>
            <dd class="mt-1 space-y-1 text-xs text-surface-500">
              <div>开始: {{ formatTime(selectedItem.started_at) }}</div>
              <div>完成: {{ formatTime(selectedItem.finished_at) }}</div>
              <div>记录: {{ formatTime(selectedItem.created_at) }}</div>
            </dd>
          </div>

          <div v-if="selectedItem.request_id">
            <dt class="text-2xs font-semibold uppercase text-surface-400">请求 ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-400 break-all">{{ selectedItem.request_id }}</dd>
          </div>
        </dl>
      </template>
    </DetailDrawer>
  </div>
</template>
