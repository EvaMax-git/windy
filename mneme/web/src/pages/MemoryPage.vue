<script setup lang="ts">
import { ref, defineAsyncComponent, computed, type Component } from "vue";
import PageHeader from "@/components/PageHeader.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";

// ── Lazy-loaded tab components ──
const tabs: Record<string, Component> = {
  candidates: defineAsyncComponent({
    loader: () => import("@/pages/tabs/MemoryTabCandidates.vue"),
    loadingComponent: LoadingSkeleton,
    delay: 100,
  }),
  approved: defineAsyncComponent({
    loader: () => import("@/pages/tabs/MemoryTabApproved.vue"),
    loadingComponent: LoadingSkeleton,
    delay: 100,
  }),
  review: defineAsyncComponent({
    loader: () => import("@/pages/tabs/MemoryTabReview.vue"),
    loadingComponent: LoadingSkeleton,
    delay: 100,
  }),
  conversations: defineAsyncComponent({
    loader: () => import("@/pages/tabs/MemoryTabConversations.vue"),
    loadingComponent: LoadingSkeleton,
    delay: 100,
  }),
  stores: defineAsyncComponent({
    loader: () => import("@/pages/tabs/MemoryTabStores.vue"),
    loadingComponent: LoadingSkeleton,
    delay: 100,
  }),
};

// ── Tab definitions ──
interface TabDef {
  key: string;
  label: string;
  icon: string;
}

const tabDefs: TabDef[] = [
  { key: "candidates", label: "候选记忆", icon: "inbox" },
  { key: "approved", label: "正式记忆", icon: "database" },
  { key: "review", label: "审核", icon: "shield-check" },
  { key: "conversations", label: "对话", icon: "chat" },
  { key: "stores", label: "子库管理", icon: "layers" },
];

const activeTab = ref<string>("candidates");

const activeComponent = computed(() => tabs[activeTab.value] ?? null);

const subtitleMap: Record<string, string> = {
  candidates: "浏览和审批候选记忆 — 由 Agent 自动生成或导入",
  approved: "浏览、搜索和管理正式记忆 — 支持完整生命周期",
  review: "批准或拒绝敏感操作、DLQ重放和恢复操作",
  conversations: "查看 conversations 与 messages — 支持筛选、详情和消息分页",
  stores: "创建和管理记忆子库 — 按类型分类，绑定到 Agent 实现记忆隔离",
};

const activeSubtitle = computed(() => subtitleMap[activeTab.value] ?? "");
</script>

<template>
  <div class="mx-auto max-w-7xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader title="记忆管理" :subtitle="activeSubtitle" />

    <!-- ── Tab Navigation ── -->
    <div class="flex border-b border-surface-200 mb-6 overflow-x-auto">
      <button
        v-for="tab in tabDefs"
        :key="tab.key"
        @click="activeTab = tab.key"
        :class="[
          'flex items-center gap-2 pb-3 px-4 text-sm font-medium border-b-2 transition-colors whitespace-nowrap',
          activeTab === tab.key
            ? 'border-brand-500 text-brand-600'
            : 'border-transparent text-surface-400 hover:text-surface-600',
        ]"
      >
        <!-- Inbox icon -->
        <svg v-if="tab.icon === 'inbox'" class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M2.25 13.5h3.86a2.25 2.25 0 012.012 1.244l.256.512a2.25 2.25 0 002.013 1.244h3.218a2.25 2.25 0 002.013-1.244l.256-.512a2.25 2.25 0 012.013-1.244h3.859m-19.5.338V18a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18v-4.162c0-.224-.034-.447-.1-.661L19.24 5.338a2.25 2.25 0 00-2.15-1.588H6.911a2.25 2.25 0 00-2.15 1.588L2.35 13.177a2.25 2.25 0 00-.1.661z" />
        </svg>
        <!-- Database icon -->
        <svg v-if="tab.icon === 'database'" class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M20.25 6.375c0 2.278-3.694 4.125-8.25 4.125S3.75 8.653 3.75 6.375m16.5 0c0-2.278-3.694-4.125-8.25-4.125S3.75 4.097 3.75 6.375m16.5 0v11.25c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125V6.375m16.5 0v3.75m-16.5-3.75v3.75m16.5 0v3.75C20.25 16.153 16.556 18 12 18s-8.25-1.847-8.25-4.125v-3.75m16.5 0c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125" />
        </svg>
        <!-- Shield-check icon -->
        <svg v-if="tab.icon === 'shield-check'" class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z" />
        </svg>
        <!-- Chat icon -->
        <svg v-if="tab.icon === 'chat'" class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M20.25 8.511c.884.284 1.5 1.128 1.5 2.097v4.286c0 1.136-.847 2.1-1.98 2.193-.34.027-.68.052-1.02.072v3.091l-3-3c-1.354 0-2.694-.055-4.02-.163a2.115 2.115 0 01-.825-.242m9.345-8.334a2.126 2.126 0 00-.476-.095 48.64 48.64 0 00-8.048 0c-1.131.094-1.976 1.057-1.976 2.192v4.286c0 .837.46 1.58 1.155 1.951m9.345-8.334V6.637c0-1.621-1.152-3.026-2.76-3.235A48.455 48.455 0 0011.25 3c-2.115 0-4.198.137-6.24.402-1.608.209-2.76 1.614-2.76 3.235v6.226c0 1.621 1.152 3.026 2.76 3.235.577.075 1.157.14 1.74.194V21l4.155-4.155" />
        </svg>
        <!-- Layers icon -->
        <svg v-if="tab.icon === 'layers'" class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M16.5 8.25V6a2.25 2.25 0 00-2.25-2.25H6A2.25 2.25 0 003.75 6v8.25A2.25 2.25 0 006 16.5h2.25m8.25-8.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-7.5A2.25 2.25 0 018.25 18v-1.5m8.25-8.25h-6a2.25 2.25 0 00-2.25 2.25v6" />
        </svg>
        {{ tab.label }}
      </button>
    </div>

    <!-- ── Dynamic Tab Content (lazy-loaded) ── -->
    <Transition name="fade" mode="out-in">
      <KeepAlive include="MemoryTabCandidates,MemoryTabApproved,MemoryTabReview,MemoryTabConversations,MemoryTabStores">
        <component :is="activeComponent" :key="activeTab" />
      </KeepAlive>
    </Transition>
  </div>
</template>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.15s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
