<script setup lang="ts">
import { onMounted, onUnmounted, computed } from "vue";
import { useHealthStore } from "@/stores/health";
import StatusBadge from "@/components/StatusBadge.vue";
import PageHeader from "@/components/PageHeader.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";

const health = useHealthStore();

onMounted(() => {
  health.startAutoRefresh();
});

onUnmounted(() => {
  health.stopAutoRefresh();
});

const formattedLastUpdate = computed(() => {
  if (!health.lastUpdated) return "从未";
  return health.lastUpdated.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
});

const overallStatus = computed(() => {
  if (health.loading && !health.data) return "loading";
  if (!health.data) return "unavailable";
  return health.data.status;
});

const overallLabel = computed(() => {
  if (health.loading && !health.data) return "检查中...";
  if (!health.data) return "不可用";
  if (health.data.status === "ok") return "所有系统运行正常";
  if (health.data.status === "degraded") return "性能降级";
  return "服务不可用";
});

const dbLabel = computed(() => {
  if (!health.data) return "未知";
  if (health.data.database === "ok") return "已连接";
  if (health.data.database === "degraded") return "降级";
  return "已断开";
});

const redisLabel = computed(() => {
  if (!health.data) return "未知";
  if (health.data.redis === "ok") return "已连接";
  if (health.data.redis === "degraded") return "降级";
  return "已断开";
});

const workerStatus = computed(() => {
  // Worker status is derived: if Redis is connected, a lease holder may exist
  if (!health.data) return { status: "unknown", label: "未知" };
  if (health.data.redis === "ok") return { status: "standby", label: "待命" };
  if (health.data.redis === "degraded") return { status: "unknown", label: "未知" };
  return { status: "stopped", label: "已停止" };
});

const outboxLabel = computed(() => {
  if (health.outboxPending < 0) return "不适用";
  return `${health.outboxPending} 条待处理`;
});

const outboxStatus = computed(() => {
  if (health.outboxPending < 0) return "unknown";
  if (health.outboxPending === 0) return "ok";
  if (health.outboxPending < 100) return "degraded";
  return "degraded";
});
</script>

<template>
  <div class="mx-auto max-w-5xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader title="健康仪表盘" subtitle="实时平台健康监控" />

    <!-- Loading skeleton -->
    <LoadingSkeleton v-if="health.loading && !health.data" variant="stat-row" />

    <!-- Overall status banner -->
    <div v-else-if="health.data"
      :class="[
        'card mb-8 p-6',
        overallStatus === 'ok'
          ? 'border-emerald-200 bg-emerald-50/50'
          : overallStatus === 'degraded'
            ? 'border-amber-200 bg-amber-50/50'
            : 'border-red-200 bg-red-50/50',
      ]"
    >
      <div class="flex items-center gap-4">
        <div
          :class="[
            'flex h-12 w-12 items-center justify-center rounded-full',
            overallStatus === 'ok'
              ? 'bg-emerald-100 text-emerald-600'
              : overallStatus === 'degraded'
                ? 'bg-amber-100 text-amber-600'
                : 'bg-red-100 text-red-600',
          ]"
        >
          <!-- Check icon for OK -->
          <svg
            v-if="overallStatus === 'ok'"
            xmlns="http://www.w3.org/2000/svg"
            class="h-6 w-6"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            stroke-width="2"
          >
            <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7" />
          </svg>
          <!-- Alert icon for degraded -->
          <svg
            v-else-if="overallStatus === 'degraded'"
            xmlns="http://www.w3.org/2000/svg"
            class="h-6 w-6"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            stroke-width="2"
          >
            <path
              stroke-linecap="round"
              stroke-linejoin="round"
              d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L3.34 15.5c-.77.833.192 2.5 1.732 2.5z"
            />
          </svg>
          <!-- X icon for unavailable -->
          <svg
            v-else
            xmlns="http://www.w3.org/2000/svg"
            class="h-6 w-6"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            stroke-width="2"
          >
            <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </div>
        <div>
          <h2 class="text-lg font-semibold text-surface-900">{{ overallLabel }}</h2>
          <p class="text-sm text-surface-500">
            最后更新: {{ formattedLastUpdate }}
            <span v-if="health.loading" class="ml-1 inline-block animate-pulse">⟳</span>
          </p>
        </div>
        <div class="ml-auto">
          <button
            class="btn btn-secondary btn-sm"
            :disabled="health.loading"
            @click="health.refresh()"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              :class="['h-4 w-4', health.loading ? 'animate-spin' : '']"
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
    </div>

    <!-- Dependency cards grid -->
    <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <!-- Database -->
      <div class="card-hoverable p-5">
        <div class="flex items-start justify-between">
          <div>
            <p class="text-2xs font-semibold uppercase tracking-wider text-surface-400">
              数据库
            </p>
            <p class="mt-2 text-2xl font-bold font-mono tabular-nums text-surface-900">
              <StatusBadge :status="health.data?.database || 'unavailable'" :label="dbLabel" />
            </p>
          </div>
          <div
            :class="[
              'flex h-10 w-10 items-center justify-center rounded-lg',
              health.dbConnected
                ? 'bg-emerald-50 text-emerald-600'
                : 'bg-red-50 text-red-600',
            ]"
          >
            <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4m0 5c0 2.21-3.582 4-8 4s-8-1.79-8-4" />
            </svg>
          </div>
        </div>
        <p class="mt-3 text-xs text-surface-400">
          数据库连接
        </p>
      </div>

      <!-- Redis -->
      <div class="card-hoverable p-5">
        <div class="flex items-start justify-between">
          <div>
            <p class="text-2xs font-semibold uppercase tracking-wider text-surface-400">
              Redis
            </p>
            <p class="mt-2 text-2xl font-bold font-mono tabular-nums text-surface-900">
              <StatusBadge :status="health.data?.redis || 'unavailable'" :label="redisLabel" />
            </p>
          </div>
          <div
            :class="[
              'flex h-10 w-10 items-center justify-center rounded-lg',
              health.redisConnected
                ? 'bg-emerald-50 text-emerald-600'
                : health.data?.redis === 'degraded'
                  ? 'bg-amber-50 text-amber-600'
                  : 'bg-red-50 text-red-600',
            ]"
          >
            <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
          </div>
        </div>
        <p class="mt-3 text-xs text-surface-400">
          缓存 / 租约代理
        </p>
      </div>

      <!-- Worker -->
      <div class="card-hoverable p-5">
        <div class="flex items-start justify-between">
          <div>
            <p class="text-2xs font-semibold uppercase tracking-wider text-surface-400">
              工作进程
            </p>
            <p class="mt-2 text-2xl font-bold font-mono tabular-nums text-surface-900">
              <StatusBadge :status="workerStatus.status" :label="workerStatus.label" />
            </p>
          </div>
          <div
            :class="[
              'flex h-10 w-10 items-center justify-center rounded-lg',
              workerStatus.status === 'standby'
                ? 'bg-amber-50 text-amber-600'
                : workerStatus.status === 'ok'
                  ? 'bg-emerald-50 text-emerald-600'
                  : 'bg-surface-100 text-surface-400',
            ]"
          >
            <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
            </svg>
          </div>
        </div>
        <p class="mt-3 text-xs text-surface-400">
          发件箱调度器
        </p>
      </div>

      <!-- Outbox -->
      <div class="card-hoverable p-5">
        <div class="flex items-start justify-between">
          <div>
            <p class="text-2xs font-semibold uppercase tracking-wider text-surface-400">
              待发件数
            </p>
            <p class="mt-2 text-2xl font-bold font-mono tabular-nums text-surface-900">
              {{ health.outboxPending < 0 ? "—" : health.outboxPending }}
            </p>
          </div>
          <div
            :class="[
              'flex h-10 w-10 items-center justify-center rounded-lg',
              health.outboxPending === 0
                ? 'bg-emerald-50 text-emerald-600'
                : health.outboxPending > 0
                  ? 'bg-amber-50 text-amber-600'
                  : 'bg-surface-100 text-surface-400',
            ]"
          >
            <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" />
            </svg>
          </div>
        </div>
        <p class="mt-3 text-xs text-surface-400">
          待分发事件
        </p>
      </div>
    </div>

    <!-- Auto-refresh indicator -->
    <div v-if="health.data" class="mt-8 flex items-center justify-center gap-2 text-xs text-surface-400">
      <span
        :class="['inline-block h-1.5 w-1.5 rounded-full', health.loading ? 'animate-pulse bg-brand-500' : 'bg-emerald-400']"
      ></span>
      每 {{ health.refreshInterval / 1000 }} 秒自动刷新
      <span class="text-surface-300">·</span>
      <button
        class="text-brand-600 hover:text-brand-700 underline underline-offset-2"
        @click="health.stopAutoRefresh()"
      >
        暂停
      </button>
      <span class="text-surface-300">·</span>
      <button
        class="text-brand-600 hover:text-brand-700 underline underline-offset-2"
        @click="health.startAutoRefresh()"
      >
        继续
      </button>
    </div>

    <!-- Error alert -->
    <div
      v-if="health.error"
      class="card mt-6 border-red-200 bg-red-50 p-4"
    >
      <div class="flex items-center gap-3">
        <svg
          xmlns="http://www.w3.org/2000/svg"
          class="h-5 w-5 shrink-0 text-red-500"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          stroke-width="2"
        >
          <path stroke-linecap="round" stroke-linejoin="round" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <div>
          <p class="text-sm font-medium text-red-800">健康检查失败</p>
          <p class="text-xs text-red-600 mt-0.5">{{ health.error }}</p>
        </div>
      </div>
    </div>

    <!-- Quick links -->
    <div class="mt-10">
      <h3 class="text-sm font-semibold text-surface-500 uppercase tracking-wider mb-4">
        治理
      </h3>
      <div class="grid gap-3 sm:grid-cols-3">
        <router-link
          to="/app/audit"
          class="card-hoverable flex items-center gap-3 p-4 group"
        >
          <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-50 text-brand-600">
            <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01" />
            </svg>
          </div>
          <div>
            <p class="text-sm font-medium text-surface-700 group-hover:text-brand-600 transition-colors">
              审计日志
            </p>
            <p class="text-xs text-surface-400">安全事件追踪</p>
          </div>
        </router-link>

        <router-link
          to="/app/review"
          class="card-hoverable flex items-center gap-3 p-4 group"
        >
          <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-50 text-brand-600">
            <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          </div>
          <div>
            <p class="text-sm font-medium text-surface-700 group-hover:text-brand-600 transition-colors">
              审核队列
            </p>
            <p class="text-xs text-surface-400">待审核项</p>
          </div>
        </router-link>

        <router-link
          to="/app/dlq"
          class="card-hoverable flex items-center gap-3 p-4 group"
        >
          <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-50 text-brand-600">
            <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L3.34 15.5c-.77.833.192 2.5 1.732 2.5z" />
            </svg>
          </div>
          <div>
            <p class="text-sm font-medium text-surface-700 group-hover:text-brand-600 transition-colors">
              死信队列
            </p>
            <p class="text-xs text-surface-400">失败投递记录</p>
          </div>
        </router-link>
      </div>
    </div>
  </div>
</template>
