<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(
  defineProps<{
    icon?: string;
    title?: string;
    description?: string;
    actionLabel?: string;
    resourceName?: string;
  }>(),
  {
    icon: "inbox",
    title: "",
    description: "",
    actionLabel: "",
    resourceName: "",
  },
);

defineEmits<{
  action: [];
}>();

// ── Default content mappings ──
const defaultTitle = "暂无数据";
const defaultDescriptions: Record<string, string> = {
  agent: "创建第一个 Agent 来开始使用平台",
  memory: "还没有记忆条目，提取或手动创建第一条记忆",
  conversation: "创建第一个对话来开始记录",
  review: "暂无待审核项",
  context: "编译 Context 来为 Agent 准备上下文",
  search: "输入关键词开始搜索",
  asset: "上传第一个资产文件",
  knowledge: "导入第一条知识条目",
  backup: "暂无备份记录",
  job: "暂无任务记录",
};

function resourceLabel(name: string): string {
  const map: Record<string, string> = {
    agent: "Agent",
    memory: "记忆",
    conversation: "对话",
    review: "审核项",
    context: "Context",
    search: "搜索",
    asset: "资产",
    knowledge: "知识条目",
    backup: "备份",
    job: "任务",
  };
  return map[name] ?? name;
}

// ── Computed values ──
const titleText = computed(() => {
  if (props.title) return props.title;
  return defaultTitle;
});

const descText = computed(() => {
  if (props.description) return props.description;
  if (props.resourceName && defaultDescriptions[props.resourceName]) {
    return defaultDescriptions[props.resourceName];
  }
  return "";
});

const btnLabel = computed(() => {
  if (props.actionLabel) return props.actionLabel;
  if (props.resourceName) return `创建第一个${resourceLabel(props.resourceName)}`;
  return "";
});

// ── Icons ──
const iconSvgs: Record<string, string> = {
  inbox: `<svg xmlns="http://www.w3.org/2000/svg" class="h-12 w-12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1"><path stroke-linecap="round" stroke-linejoin="round" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4"/></svg>`,
  document: `<svg xmlns="http://www.w3.org/2000/svg" class="h-12 w-12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z"/></svg>`,
  folder: `<svg xmlns="http://www.w3.org/2000/svg" class="h-12 w-12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1"><path stroke-linecap="round" stroke-linejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/></svg>`,
  search: `<svg xmlns="http://www.w3.org/2000/svg" class="h-12 w-12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1"><path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"/></svg>`,
  bot: `<svg xmlns="http://www.w3.org/2000/svg" class="h-12 w-12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1"><path stroke-linecap="round" stroke-linejoin="round" d="M8.5 15.5a3.5 3.5 0 007 0h-2a1.5 1.5 0 01-3 0H8.5z"/><rect x="4" y="5" width="16" height="14" rx="3"/><circle cx="9" cy="11" r="1" fill="currentColor"/><circle cx="15" cy="11" r="1" fill="currentColor"/><path stroke-linecap="round" stroke-linejoin="round" d="M12 3v2"/></svg>`,
  messages: `<svg xmlns="http://www.w3.org/2000/svg" class="h-12 w-12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1"><path stroke-linecap="round" stroke-linejoin="round" d="M7.5 8.25h9m-9 3.75h5.25M4.5 19.5l3-3h9a3 3 0 003-3v-6a3 3 0 00-3-3h-9a3 3 0 00-3 3v12z"/></svg>`,
  brain: `<svg xmlns="http://www.w3.org/2000/svg" class="h-12 w-12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1"><path stroke-linecap="round" stroke-linejoin="round" d="M12 5a3 3 0 10-5.997.125 4 4 0 00-2.526 5.77 4 4 0 00.556 6.588A4 4 0 1012 18Z"/><path stroke-linecap="round" stroke-linejoin="round" d="M12 5a3 3 0 115.997.125 4 4 0 012.526 5.77 4 4 0 01-.556 6.588A4 4 0 1112 18Z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 13a4.5 4.5 0 01-3-4 4.5 4.5 0 01-3 4"/><path stroke-linecap="round" stroke-linejoin="round" d="M17.599 6.5a3 3 0 00.399-1.375"/><path stroke-linecap="round" stroke-linejoin="round" d="M6.4 12.5a3 3 0 01-.4 1.375"/><path stroke-linecap="round" stroke-linejoin="round" d="M18 12.5a3 3 0 01.4 1.375"/><path stroke-linecap="round" stroke-linejoin="round" d="M6 12.5a3 3 0 00-.399-1.375"/></svg>`,
  clock: `<svg xmlns="http://www.w3.org/2000/svg" class="h-12 w-12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1"><path stroke-linecap="round" stroke-linejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>`,
  archive: `<svg xmlns="http://www.w3.org/2000/svg" class="h-12 w-12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1"><path stroke-linecap="round" stroke-linejoin="round" d="M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4"/></svg>`,
  "check-circle": `<svg xmlns="http://www.w3.org/2000/svg" class="h-12 w-12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>`,
  "book-open": `<svg xmlns="http://www.w3.org/2000/svg" class="h-12 w-12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1"><path stroke-linecap="round" stroke-linejoin="round" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253"/></svg>`,
};

function currentIconSvg(): string {
  return iconSvgs[props.icon ?? "inbox"] ?? iconSvgs["inbox"];
}
</script>

<template>
  <div class="flex flex-col items-center justify-center py-16 px-4">
    <!-- Icon -->
    <div class="mb-4 text-surface-300" v-html="currentIconSvg()"></div>

    <!-- Title -->
    <h3 class="text-base font-semibold text-surface-600 mb-1">
      {{ titleText }}
    </h3>

    <!-- Description -->
    <p v-if="descText" class="text-sm text-surface-400 text-center max-w-sm mb-6">
      {{ descText }}
    </p>

    <!-- Action button -->
    <button
      v-if="btnLabel"
      class="btn btn-primary"
      @click="$emit('action')"
    >
      <svg
        xmlns="http://www.w3.org/2000/svg"
        class="h-4 w-4"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        stroke-width="2"
      >
        <path stroke-linecap="round" stroke-linejoin="round" d="M12 4v16m8-8H4" />
      </svg>
      {{ btnLabel }}
    </button>

    <!-- Fallback slot for custom content -->
    <slot />
  </div>
</template>
