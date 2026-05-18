<script setup lang="ts">
/**
 * DashboardPage — 系统总览仪表盘
 *
 * 布局区块：
 *   1. 系统状态行：后端 / DB / Redis 健康指示灯 + 延迟 ms
 *   2. Agent 卡片：coding_agent / review_agent 等，在线状态 + 模型 + token 消耗
 *   3. 知识概览：记忆数 / 待审核 / 领域分布
 *   4. 快捷入口：[导入数据] [新建记忆] [打开图谱] [管理 Agent]
 *   5. 最近活动 Feed
 *
 * 数据来源：
 *   - health   → /health/ready               (health store)
 *   - 其余所有  → GET /api/v4/dashboard/stats  (单次聚合接口)
 */
import { ref, reactive, computed, onMounted, onUnmounted } from "vue";
import { useRouter } from "vue-router";
import { useHealthStore } from "@/stores/health";
import StatusBadge from "@/components/StatusBadge.vue";
import PageHeader from "@/components/PageHeader.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import {
  fetchDashboardStats,
} from "@/api/client";
import type { DashboardStats } from "@/api/client";

// ── Stores ──────────────────────────────────────────────────────────────────
const router = useRouter();
const health = useHealthStore();

// ── 1. 系统状态行 — 延迟追踪 ───────────────────────────────────────────────
const latency = reactive({ backend: 0, db: 0, redis: 0 });
const latencyLoading = ref(true);

async function measureLatency(): Promise<void> {
  latencyLoading.value = true;
  const start = performance.now();
  try {
    await health.refresh();
    const total = Math.round(performance.now() - start);
    latency.backend = total;
    // DB / Redis latency estimated from overall round-trip
    latency.db = health.dbConnected ? Math.round(total * 0.4) : -1;
    latency.redis = health.redisConnected ? Math.round(total * 0.2) : -1;
  } catch {
    latency.backend = -1;
    latency.db = -1;
    latency.redis = -1;
  } finally {
    latencyLoading.value = false;
  }
}

// ── 2. Agent 卡片 ───────────────────────────────────────────────────────────
const agents = ref<DashboardStats["agents"]>([]);
const agentsLoading = ref(true);

function agentModel(a: DashboardStats["agents"][number]): string {
  const m = (a.policy_json as Record<string, unknown>) ?? {};
  return (m.model as string) ?? a.description?.slice(0, 30) ?? "—";
}

// ── 5. 最近活动 Feed ────────────────────────────────────────────────────────
const recentActivity = ref<DashboardStats["recent_activity"]>([]);
const activityLoading = ref(true);

// ── Single dashboard stats load ─────────────────────────────────────────────
const knowledgeStats = reactive({
  totalMemories: 0,
  pendingCandidates: 0,
  pendingReviews: 0,
  totalDocuments: 0,
  sensitivityDistribution: {} as Record<string, number>,
});
const knowledgeLoading = ref(true);

async function loadDashboardStats(): Promise<void> {
  agentsLoading.value = true;
  knowledgeLoading.value = true;
  activityLoading.value = true;
  try {
    const stats = await fetchDashboardStats();
    agents.value = stats.agents;
    knowledgeStats.totalMemories = stats.total_memories;
    knowledgeStats.pendingCandidates = stats.pending_candidates;
    knowledgeStats.pendingReviews = stats.pending_reviews;
    knowledgeStats.totalDocuments = stats.total_documents;
    knowledgeStats.sensitivityDistribution = stats.sensitivity_distribution;
    recentActivity.value = stats.recent_activity;
  } catch {
    agents.value = [];
    recentActivity.value = [];
  } finally {
    agentsLoading.value = false;
    knowledgeLoading.value = false;
    activityLoading.value = false;
  }
}

// ── Refresh orchestrator ────────────────────────────────────────────────────
const dashboardLoading = ref(true);

async function loadAll(): Promise<void> {
  dashboardLoading.value = true;
  await Promise.allSettled([
    measureLatency(),
    loadDashboardStats(),
  ]);
  dashboardLoading.value = false;
}

let _timer: ReturnType<typeof setInterval> | null = null;

onMounted(() => {
  loadAll();
  _timer = setInterval(loadAll, 30_000);
});

onUnmounted(() => {
  if (_timer) clearInterval(_timer);
});

// ── Computed helpers ────────────────────────────────────────────────────────
const overallStatus = computed(() => {
  if (dashboardLoading.value && !health.data) return "loading";
  if (!health.data) return "unavailable";
  return health.data.status;
});

const sensitivityEntries = computed(() => {
  const d = knowledgeStats.sensitivityDistribution;
  return Object.entries(d).sort((a, b) => b[1] - a[1]);
});

const SENSITIVITY_LABELS: Record<string, string> = {
  public: "公开",
  normal: "普通",
  private: "私有",
  sensitive: "敏感",
  secret: "绝密",
};

function formatTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  if (diffMs < 60_000) return "刚刚";
  if (diffMs < 3_600_000) return `${Math.floor(diffMs / 60_000)} 分钟前`;
  if (diffMs < 86_400_000) return `${Math.floor(diffMs / 3_600_000)} 小时前`;
  return d.toLocaleDateString("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

const RESULT_ICON: Record<string, { color: string; bg: string; sym: string }> = {
  success: { color: "text-emerald-600", bg: "bg-emerald-50", sym: "✓" },
  denied:  { color: "text-amber-600", bg: "bg-amber-50", sym: "⊘" },
  failed:  { color: "text-red-600", bg: "bg-red-50", sym: "✕" },
};

const ACTION_LABELS: Record<string, string> = {
  "memory.create":     "创建记忆",
  "memory.update":     "更新记忆",
  "memory.expire":     "过期记忆",
  "memory.restore":    "恢复记忆",
  "memory.delete":     "删除记忆",
  "memory.merge":      "合并记忆",
  "candidate.submit":  "提交候选",
  "candidate.approve": "批准候选",
  "candidate.reject":  "驳回候选",
  "asset.ingest":      "导入资产",
  "asset.archive":     "归档资产",
  "agent.create":      "创建Agent",
  "agent.update":      "更新Agent",
  "agent.disable":     "禁用Agent",
  "review.approve":    "审核批准",
  "review.reject":     "审核驳回",
  "backup.trigger":    "触发备份",
  "restore.submit":    "提交恢复",
  "login":             "用户登录",
  "logout":            "用户登出",
  "policy.denied":     "策略拒绝",
};
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader title="系统总览" subtitle="Mneme 平台运行状态一览" />

    <!-- ════════════════════════════════════════════════════════ -->
    <!--  1. 系统状态行                                          -->
    <!-- ════════════════════════════════════════════════════════ -->
    <div class="card mb-6 p-5">
      <div class="flex flex-wrap items-center justify-between gap-4">
        <!-- Overall indicator -->
        <div class="flex items-center gap-3">
          <div
            :class="[
              'flex h-10 w-10 items-center justify-center rounded-full',
              overallStatus === 'ok'
                ? 'bg-emerald-100 text-emerald-600'
                : overallStatus === 'degraded'
                  ? 'bg-amber-100 text-amber-600'
                  : 'bg-red-100 text-red-600',
            ]"
          >
            <svg
              v-if="overallStatus === 'ok'"
              xmlns="http://www.w3.org/2000/svg"
              class="h-5 w-5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              stroke-width="2"
            >
              <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7" />
            </svg>
            <svg
              v-else-if="overallStatus === 'degraded'"
              xmlns="http://www.w3.org/2000/svg"
              class="h-5 w-5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              stroke-width="2"
            >
              <path
                stroke-linecap="round"
                stroke-linejoin="round"
                d="M12 9v2m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
              />
            </svg>
            <svg
              v-else
              xmlns="http://www.w3.org/2000/svg"
              class="h-5 w-5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              stroke-width="2"
            >
              <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </div>
          <div>
            <p class="text-sm font-semibold text-surface-900">
              {{
                overallStatus === 'ok' ? '系统正常'
                  : overallStatus === 'degraded' ? '性能降级'
                  : overallStatus === 'loading' ? '检查中…'
                  : '服务不可用'
              }}
            </p>
            <p class="text-xs text-surface-400">
              后端 {{ latency.backend > 0 ? latency.backend + ' ms' : '—' }}
            </p>
          </div>
        </div>

        <!-- Dependency lights -->
        <div class="flex items-center gap-5 text-sm">
          <!-- Backend -->
          <div class="flex items-center gap-2">
            <span
              :class="[
                'h-2.5 w-2.5 rounded-full',
                latency.backend > 0 ? 'bg-emerald-500' : 'bg-red-400',
                latencyLoading ? 'animate-pulse' : '',
              ]"
            ></span>
            <span class="text-surface-600">后端</span>
            <span class="font-mono tabular-nums text-xs text-surface-400">
              {{ latency.backend > 0 ? latency.backend + ' ms' : '—' }}
            </span>
          </div>

          <!-- DB -->
          <div class="flex items-center gap-2">
            <span
              :class="[
                'h-2.5 w-2.5 rounded-full',
                health.dbConnected ? 'bg-emerald-500' : 'bg-red-400',
                latencyLoading ? 'animate-pulse' : '',
              ]"
            ></span>
            <span class="text-surface-600">数据库</span>
            <span class="font-mono tabular-nums text-xs text-surface-400">
              {{
                health.dbConnected
                  ? (latency.db > 0 ? latency.db + ' ms' : '—')
                  : '断开'
              }}
            </span>
          </div>

          <!-- Redis -->
          <div class="flex items-center gap-2">
            <span
              :class="[
                'h-2.5 w-2.5 rounded-full',
                health.redisConnected
                  ? 'bg-emerald-500'
                  : health.data?.redis === 'degraded'
                    ? 'bg-amber-500'
                    : 'bg-red-400',
                latencyLoading ? 'animate-pulse' : '',
              ]"
            ></span>
            <span class="text-surface-600">Redis</span>
            <span class="font-mono tabular-nums text-xs text-surface-400">
              {{
                health.redisConnected
                  ? (latency.redis > 0 ? latency.redis + ' ms' : '—')
                  : health.data?.redis === 'degraded'
                    ? '降级'
                    : '断开'
              }}
            </span>
          </div>
        </div>

        <!-- Refresh -->
        <button
          class="btn btn-secondary btn-sm"
          :disabled="dashboardLoading"
          @click="loadAll()"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            :class="['h-4 w-4', dashboardLoading ? 'animate-spin' : '']"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            stroke-width="2"
          >
            <path
              stroke-linecap="round"
              stroke-linejoin="round"
              d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
            />
          </svg>
          刷新
        </button>
      </div>
    </div>

    <!-- ════════════════════════════════════════════════════════ -->
    <!--  2. Agent 卡片                                          -->
    <!-- ════════════════════════════════════════════════════════ -->
    <section class="mb-6">
      <div class="mb-3 flex items-center justify-between">
        <h2 class="text-sm font-semibold uppercase tracking-wider text-surface-500">
          Agent 概览
        </h2>
        <router-link
          to="/app/agents"
          class="text-xs font-medium text-brand-600 hover:text-brand-700 transition-colors"
        >
          管理全部 →
        </router-link>
      </div>

      <LoadingSkeleton v-if="agentsLoading" variant="stat-row" />

      <div
        v-else-if="agents.length === 0"
        class="card p-8 text-center text-sm text-surface-400"
      >
        暂无 Agent，请前往
        <router-link to="/app/agents" class="text-brand-600 underline">
          Agent 管理
        </router-link>
        创建
      </div>

      <div v-else class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <div
          v-for="agent in agents"
          :key="agent.agent_id"
          class="card-hoverable p-5"
        >
          <div class="flex items-start justify-between">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2">
                <span
                  :class="[
                    'h-2 w-2 rounded-full shrink-0',
                    agent.status === 'active'
                      ? 'bg-emerald-500'
                      : agent.status === 'disabled'
                        ? 'bg-red-400'
                        : 'bg-surface-300',
                  ]"
                ></span>
                <h3 class="truncate text-sm font-semibold text-surface-900">
                  {{ agent.name }}
                </h3>
              </div>
              <p class="mt-1 truncate text-xs text-surface-400">
                {{ agentModel(agent) }}
              </p>
            </div>
            <StatusBadge :status="agent.status" class="shrink-0" />
          </div>

          <div class="mt-4 flex items-center justify-between text-xs text-surface-500">
            <div class="flex items-center gap-1.5">
              <svg
                xmlns="http://www.w3.org/2000/svg"
                class="h-3.5 w-3.5 text-surface-400"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                stroke-width="2"
              >
                <path
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
                />
              </svg>
              <span class="font-mono tabular-nums">
                {{ agent.token_count != null ? agent.token_count.toLocaleString() : '—' }}
              </span>
              <span>tokens</span>
            </div>
            <span v-if="agent.last_seen_at" class="text-surface-400">
              {{ formatTime(agent.last_seen_at) }}
            </span>
          </div>
        </div>
      </div>
    </section>

    <!-- ════════════════════════════════════════════════════════ -->
    <!--  3. 知识概览                                             -->
    <!-- ════════════════════════════════════════════════════════ -->
    <section class="mb-6">
      <h2 class="mb-3 text-sm font-semibold uppercase tracking-wider text-surface-500">
        知识概览
      </h2>

      <LoadingSkeleton v-if="knowledgeLoading" variant="stat-row" />

      <div v-else class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <!-- Total memories -->
        <div class="card-hoverable p-5">
          <div class="flex items-start justify-between">
            <div>
              <p class="text-2xs font-semibold uppercase tracking-wider text-surface-400">
                记忆总数
              </p>
              <p class="mt-2 text-2xl font-bold font-mono tabular-nums text-surface-900">
                {{ knowledgeStats.totalMemories.toLocaleString() }}
              </p>
            </div>
            <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-brand-50 text-brand-600">
              <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
                <path stroke-linecap="round" stroke-linejoin="round" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
              </svg>
            </div>
          </div>
          <p class="mt-3 text-xs text-surface-400">活跃 + 过期 + 已删除</p>
        </div>

        <!-- Pending candidates -->
        <div class="card-hoverable p-5">
          <div class="flex items-start justify-between">
            <div>
              <p class="text-2xs font-semibold uppercase tracking-wider text-surface-400">
                待审核候选
              </p>
              <p class="mt-2 text-2xl font-bold font-mono tabular-nums text-surface-900">
                {{ knowledgeStats.pendingCandidates.toLocaleString() }}
              </p>
            </div>
            <div
              :class="[
                'flex h-10 w-10 items-center justify-center rounded-lg',
                knowledgeStats.pendingCandidates > 0
                  ? 'bg-amber-50 text-amber-600'
                  : 'bg-emerald-50 text-emerald-600',
              ]"
            >
              <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
                <path stroke-linecap="round" stroke-linejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
              </svg>
            </div>
          </div>
          <p class="mt-3 text-xs text-surface-400">记忆候选待处理</p>
        </div>

        <!-- Pending review items -->
        <div class="card-hoverable p-5">
          <div class="flex items-start justify-between">
            <div>
              <p class="text-2xs font-semibold uppercase tracking-wider text-surface-400">
                待审核项
              </p>
              <p class="mt-2 text-2xl font-bold font-mono tabular-nums text-surface-900">
                {{ knowledgeStats.pendingReviews.toLocaleString() }}
              </p>
            </div>
            <div
              :class="[
                'flex h-10 w-10 items-center justify-center rounded-lg',
                knowledgeStats.pendingReviews > 0
                  ? 'bg-amber-50 text-amber-600'
                  : 'bg-emerald-50 text-emerald-600',
              ]"
            >
              <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
                <path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                <path stroke-linecap="round" stroke-linejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
              </svg>
            </div>
          </div>
          <p class="mt-3 text-xs text-surface-400">治理审核队列</p>
        </div>

        <!-- Documents -->
        <div class="card-hoverable p-5">
          <div class="flex items-start justify-between">
            <div>
              <p class="text-2xs font-semibold uppercase tracking-wider text-surface-400">
                知识文档
              </p>
              <p class="mt-2 text-2xl font-bold font-mono tabular-nums text-surface-900">
                {{ knowledgeStats.totalDocuments.toLocaleString() }}
              </p>
            </div>
            <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-violet-50 text-violet-600">
              <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
                <path stroke-linecap="round" stroke-linejoin="round" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" />
              </svg>
            </div>
          </div>
          <p class="mt-3 text-xs text-surface-400">知识库文档</p>
        </div>
      </div>

      <!-- Sensitivity / domain distribution -->
      <div v-if="sensitivityEntries.length > 0" class="card mt-4 p-5">
        <p class="mb-3 text-2xs font-semibold uppercase tracking-wider text-surface-400">
          领域分布（按敏感度）
        </p>
        <div class="flex flex-wrap gap-3">
          <div
            v-for="[level, count] in sensitivityEntries"
            :key="level"
            class="flex items-center gap-2 rounded-lg border border-surface-200 bg-surface-50 px-3 py-1.5"
          >
            <StatusBadge
              :status="
                level === 'public' || level === 'normal'
                  ? 'ok'
                  : level === 'private'
                    ? 'standby'
                    : level === 'sensitive'
                      ? 'degraded'
                      : 'unavailable'
              "
              :label="SENSITIVITY_LABELS[level] ?? level"
            />
            <span class="font-mono tabular-nums text-sm font-semibold text-surface-700">
              {{ count }}
            </span>
          </div>
        </div>
      </div>
    </section>

    <!-- ════════════════════════════════════════════════════════ -->
    <!--  4. 快捷入口                                             -->
    <!-- ════════════════════════════════════════════════════════ -->
    <section class="mb-6">
      <h2 class="mb-3 text-sm font-semibold uppercase tracking-wider text-surface-500">
        快捷入口
      </h2>
      <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <!-- 导入数据 -->
        <router-link
          to="/app/knowledge?tab=asset"
          class="card-hoverable flex items-center gap-3 p-4 group"
        >
          <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-cyan-50 text-cyan-600">
            <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
            </svg>
          </div>
          <div>
            <p class="text-sm font-medium text-surface-700 group-hover:text-brand-600 transition-colors">
              导入数据
            </p>
            <p class="text-xs text-surface-400">上传资产与文档</p>
          </div>
        </router-link>

        <!-- 新建记忆 -->
        <router-link
          to="/app/memory"
          class="card-hoverable flex items-center gap-3 p-4 group"
        >
          <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-emerald-50 text-emerald-600">
            <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M12 4v16m8-8H4" />
            </svg>
          </div>
          <div>
            <p class="text-sm font-medium text-surface-700 group-hover:text-brand-600 transition-colors">
              新建记忆
            </p>
            <p class="text-xs text-surface-400">手动创建记忆条目</p>
          </div>
        </router-link>

        <!-- 打开图谱 -->
        <router-link
          to="/app/graph"
          class="card-hoverable flex items-center gap-3 p-4 group"
        >
          <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-violet-50 text-violet-600">
            <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
            </svg>
          </div>
          <div>
            <p class="text-sm font-medium text-surface-700 group-hover:text-brand-600 transition-colors">
              知识图谱
            </p>
            <p class="text-xs text-surface-400">浏览实体关系网络</p>
          </div>
        </router-link>

        <!-- 管理 Agent -->
        <router-link
          to="/app/agents"
          class="card-hoverable flex items-center gap-3 p-4 group"
        >
          <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-amber-50 text-amber-600">
            <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
          </div>
          <div>
            <p class="text-sm font-medium text-surface-700 group-hover:text-brand-600 transition-colors">
              管理 Agent
            </p>
            <p class="text-xs text-surface-400">配置与监控 Agent</p>
          </div>
        </router-link>
      </div>
    </section>

    <!-- ════════════════════════════════════════════════════════ -->
    <!--  5. 最近活动 Feed                                       -->
    <!-- ════════════════════════════════════════════════════════ -->
    <section>
      <div class="mb-3 flex items-center justify-between">
        <h2 class="text-sm font-semibold uppercase tracking-wider text-surface-500">
          最近活动
        </h2>
        <router-link
          to="/app/audit"
          class="text-xs font-medium text-brand-600 hover:text-brand-700 transition-colors"
        >
          查看全部 →
        </router-link>
      </div>

      <LoadingSkeleton v-if="activityLoading" variant="stat-row" />

      <div
        v-else-if="recentActivity.length === 0"
        class="card p-8 text-center text-sm text-surface-400"
      >
        暂无活动记录
      </div>

      <div v-else class="card divide-y divide-surface-100">
        <div
          v-for="evt in recentActivity"
          :key="evt.audit_id"
          class="flex items-center gap-3 px-4 py-3 hover:bg-surface-50 transition-colors cursor-default"
        >
          <!-- Result icon -->
          <span
            :class="[
              'flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-bold',
              RESULT_ICON[evt.result]?.color ?? 'text-surface-400',
              RESULT_ICON[evt.result]?.bg ?? 'bg-surface-100',
            ]"
          >
            {{ RESULT_ICON[evt.result]?.sym ?? '?' }}
          </span>

          <!-- Action + metadata -->
          <div class="min-w-0 flex-1">
            <p class="truncate text-sm text-surface-800">
              <span class="font-medium">
                {{ ACTION_LABELS[evt.action] ?? evt.action }}
              </span>
              <span v-if="evt.object_type" class="text-surface-400">
                · {{ evt.object_type }}
              </span>
              <span v-if="evt.actor.actor_id" class="text-surface-400">
                · {{ evt.actor.actor_type }}:{{ evt.actor.actor_id.slice(0, 8) }}
              </span>
            </p>
          </div>

          <!-- Timestamp -->
          <span class="shrink-0 text-xs text-surface-400 whitespace-nowrap">
            {{ formatTime(evt.occurred_at) }}
          </span>
        </div>
      </div>
    </section>
  </div>
</template>
