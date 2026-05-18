import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { fetchDashboardHealthSummary } from "@/api/client";
import type { HealthReadyData } from "@/types";

export const useHealthStore = defineStore("health", () => {
  const data = ref<HealthReadyData | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const lastUpdated = ref<Date | null>(null);
  const refreshInterval = ref(10_000); // 10 seconds

  const isHealthy = computed(() => {
    if (!data.value) return false;
    return data.value.status !== "unavailable";
  });

  const isDegraded = computed(() => {
    return data.value?.status === "degraded";
  });

  const dbConnected = computed(() => {
    return data.value?.database === "ok";
  });

  const redisConnected = computed(() => {
    return data.value?.redis === "ok";
  });

  const outboxPending = computed(() => {
    return data.value?.outbox_pending ?? -1;
  });

  async function refresh(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const result = await fetchDashboardHealthSummary();
      data.value = result;
      lastUpdated.value = new Date();
    } catch (e) {
      error.value = e instanceof Error ? e.message : "获取健康状态失败";
      // Set a minimal data object so the UI can show the error state
      data.value = {
        status: "unavailable",
        database: "unavailable",
        redis: "unavailable",
        outbox_pending: -1,
      };
    } finally {
      loading.value = false;
    }
  }

  let _timer: ReturnType<typeof setInterval> | null = null;

  function startAutoRefresh(intervalMs?: number): void {
    stopAutoRefresh();
    const ms = intervalMs ?? refreshInterval.value;
    refresh(); // immediate first fetch
    _timer = setInterval(refresh, ms);
  }

  function stopAutoRefresh(): void {
    if (_timer !== null) {
      clearInterval(_timer);
      _timer = null;
    }
  }

  return {
    data,
    loading,
    error,
    lastUpdated,
    refreshInterval,
    isHealthy,
    isDegraded,
    dbConnected,
    redisConnected,
    outboxPending,
    refresh,
    startAutoRefresh,
    stopAutoRefresh,
  };
});
