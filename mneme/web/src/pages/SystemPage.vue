<script setup lang="ts">
import { ref } from "vue";
import PageHeader from "@/components/PageHeader.vue";
import HealthTab from "@/pages/tabs/HealthTab.vue";
import BackupTab from "@/pages/tabs/BackupTab.vue";
import JobsTab from "@/pages/tabs/JobsTab.vue";
import DLQTab from "@/pages/tabs/DLQTab.vue";
import EvalTab from "@/pages/tabs/EvalTab.vue";
import AuditTab from "@/pages/tabs/AuditTab.vue";

type Tab = "health" | "backup" | "jobs" | "dlq" | "eval" | "audit";
const activeTab = ref<Tab>("health");

const tabs: { key: Tab; label: string }[] = [
  { key: "health",  label: "健康监控" },
  { key: "backup",  label: "备份恢复" },
  { key: "jobs",    label: "任务队列" },
  { key: "dlq",     label: "死信队列" },
  { key: "eval",    label: "评估中心" },
  { key: "audit",   label: "日志" },
];

function handleSwitchTab(tab: string) {
  activeTab.value = tab as Tab;
}
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader title="系统" subtitle="健康监控、备份恢复、任务队列、死信队列、评估中心与审计日志" />

    <!-- Tab bar -->
    <div class="flex flex-wrap border-b border-surface-200 mb-6 -mx-4 px-4 gap-0">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        @click="activeTab = tab.key"
        :class="[
          'pb-3 px-3 text-sm font-medium border-b-2 transition-colors whitespace-nowrap',
          activeTab === tab.key
            ? 'border-brand-500 text-brand-600'
            : 'border-transparent text-surface-400 hover:text-surface-600',
        ]"
      >
        {{ tab.label }}
      </button>
    </div>

    <!-- Tab content -->
    <HealthTab v-if="activeTab === 'health'" />
    <BackupTab v-if="activeTab === 'backup'" @switch-tab="handleSwitchTab" />
    <JobsTab v-if="activeTab === 'jobs'" />
    <DLQTab v-if="activeTab === 'dlq'" />
    <EvalTab v-if="activeTab === 'eval'" />
    <AuditTab v-if="activeTab === 'audit'" />
  </div>
</template>
