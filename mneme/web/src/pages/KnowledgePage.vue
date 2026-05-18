<script setup lang="ts">
import { ref, watch, computed, defineAsyncComponent, type Component } from "vue";
import { useRoute } from "vue-router";
import PageHeader from "@/components/PageHeader.vue";

// ── Tab definitions ──
interface TabDef {
  key: string;
  label: string;
  icon: string;
  component: Component;
}

const tabs: TabDef[] = [
  {
    key: "asset",
    label: "导入文件",
    icon: "📦",
    component: defineAsyncComponent(() => import("./tabs/AssetTab.vue")),
  },
  {
    key: "knowledge",
    label: "文档",
    icon: "📄",
    component: defineAsyncComponent(() => import("./tabs/KnowledgeTab.vue")),
  },
  {
    key: "search",
    label: "搜索",
    icon: "🔍",
    component: defineAsyncComponent(() => import("./tabs/SearchTab.vue")),
  },
  {
    key: "index",
    label: "索引管理",
    icon: "🗂️",
    component: defineAsyncComponent(() => import("./tabs/IndexTab.vue")),
  },
  {
    key: "pipeline",
    label: "管道定义",
    icon: "⚙️",
    component: defineAsyncComponent(() => import("./tabs/PipelineTab.vue")),
  },
  {
    key: "store",
    label: "子库管理",
    icon: "📚",
    component: defineAsyncComponent(() => import("./tabs/StoreTab.vue")),
  },
  {
    key: "project",
    label: "项目管理",
    icon: "📋",
    component: defineAsyncComponent(() => import("./tabs/ProjectManageTab.vue")),
  },
];

const route = useRoute();

// Determine initial tab from query param (for Dashboard [导入数据] button)
function initialTab(): string {
  const tabParam = route.query.tab as string | undefined;
  const validKeys = tabs.map((t) => t.key);
  return tabParam && validKeys.includes(tabParam) ? tabParam : tabs[0].key;
}

const activeTab = ref<string>(initialTab());

function switchTab(key: string) {
  activeTab.value = key;
}

// Keep previously loaded tabs alive (avoid re-fetching on tab switch)
const loadedTabs = ref<Set<string>>(new Set([activeTab.value]));

// ── Tab grouping (Stage 2: main vs settings) ──
const mainTabs = computed(() => tabs.slice(0, 3));
const settingsTabs = computed(() => tabs.slice(3));
const settingsKeys = computed(() => new Set(settingsTabs.value.map((t) => t.key)));
const isInSettings = computed(() => settingsKeys.value.has(activeTab.value));
const settingsOpen = ref(isInSettings.value);

// Auto-open settings when navigating to a settings tab via query param
watch(activeTab, (key) => {
  if (settingsKeys.value.has(key)) {
    settingsOpen.value = true;
  }
});

function onTabClick(key: string) {
  switchTab(key);
  loadedTabs.value.add(key);
}

// Watch for route query changes (e.g. clicking [导入数据] again)
watch(() => route.query.tab, (newTab) => {
  if (newTab && typeof newTab === "string") {
    const validKeys = tabs.map((t) => t.key);
    if (validKeys.includes(newTab)) {
      switchTab(newTab);
      loadedTabs.value.add(newTab);
    }
  }
});
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader
      title="知识库"
      subtitle="导入文件 · 浏览文档 · 搜索知识"
    />

    <!-- Tab bar -->
    <div class="mb-6 border-b border-surface-200">
      <nav class="flex gap-1 -mb-px overflow-x-auto" aria-label="知识库选项卡">
        <!-- 主 Tab: 导入文件 / 文档 / 搜索 -->
        <button
          v-for="tab in mainTabs"
          :key="tab.key"
          @click="onTabClick(tab.key)"
          :class="[
            'group inline-flex items-center gap-2 whitespace-nowrap px-4 py-3 text-sm font-medium border-b-2 transition-colors',
            activeTab === tab.key
              ? 'border-brand-500 text-brand-600'
              : 'border-transparent text-surface-500 hover:border-surface-300 hover:text-surface-700',
          ]"
          :aria-selected="activeTab === tab.key"
          role="tab"
        >
          <span class="text-base leading-none">{{ tab.icon }}</span>
          {{ tab.label }}
        </button>

        <!-- 分隔符 + 设置 toggle -->
        <span class="mx-1.5 self-center text-surface-300 select-none text-lg font-light">|</span>
        <button
          @click="settingsOpen = !settingsOpen"
          :class="[
            'group inline-flex items-center gap-2 whitespace-nowrap px-4 py-3 text-sm font-medium border-b-2 transition-colors',
            isInSettings
              ? 'border-brand-500 text-brand-600'
              : 'border-transparent text-surface-500 hover:border-surface-300 hover:text-surface-700',
          ]"
          aria-label="切换设置选项卡"
        >
          <span class="text-base leading-none">⚙️</span>
          设置
        </button>

        <!-- 设置区 Tab: 仅在展开时显示 -->
        <template v-if="settingsOpen">
          <button
            v-for="tab in settingsTabs"
            :key="tab.key"
            @click="onTabClick(tab.key)"
            :class="[
              'group inline-flex items-center gap-2 whitespace-nowrap px-4 py-3 text-sm font-medium border-b-2 transition-colors',
              activeTab === tab.key
                ? 'border-brand-500 text-brand-600'
                : 'border-transparent text-surface-500 hover:border-surface-300 hover:text-surface-700',
            ]"
            :aria-selected="activeTab === tab.key"
            role="tab"
          >
            <span class="text-base leading-none">{{ tab.icon }}</span>
            {{ tab.label }}
          </button>
        </template>
      </nav>
    </div>

    <!-- Tab panels: use v-show for loaded tabs to keep state alive -->
    <div class="relative min-h-[200px]">
      <template v-for="tab in tabs" :key="tab.key">
        <Suspense v-if="loadedTabs.has(tab.key)">
          <div v-show="activeTab === tab.key" role="tabpanel">
            <component :is="tab.component" />
          </div>

          <template #fallback>
            <div class="flex items-center justify-center py-16">
              <div class="flex flex-col items-center gap-3">
                <svg class="h-8 w-8 animate-spin text-brand-500" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                <span class="text-sm text-surface-400">加载中…</span>
              </div>
            </div>
          </template>
        </Suspense>
      </template>
    </div>
  </div>
</template>
