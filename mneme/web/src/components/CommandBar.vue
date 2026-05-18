<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick, shallowRef } from "vue";
import { useRouter } from "vue-router";
import { searchGlobal } from "@/api/client";
import type { GlobalSearchResult } from "@/types";

const router = useRouter();

// ── State ──
const visible = ref(false);
const query = ref("");
const selectedIndex = ref(0);
const results = shallowRef<GlobalSearchResult[]>([]);
const total = ref(0);
const sourceCounts = ref<Record<string, number>>({});
const isLoading = ref(false);
const error = ref<string | null>(null);
const inputRef = ref<HTMLInputElement | null>(null);

// Debounce timer
let debounceTimer: ReturnType<typeof setTimeout> | null = null;
const DEBOUNCE_MS = 200;

// ── Source icon/color helpers ──
const sourceConfig: Record<string, { icon: string; label: string; colorClass: string }> = {
  agent: { icon: "bot", label: "Agent", colorClass: "bg-violet-50 text-violet-700 ring-violet-600/20" },
  knowledge: { icon: "book-open", label: "知识库", colorClass: "bg-sky-50 text-sky-700 ring-sky-600/20" },
  memory: { icon: "brain", label: "记忆", colorClass: "bg-emerald-50 text-emerald-700 ring-emerald-600/20" },
};

// ── Filtered suggestions (for empty query) ──
const quickLinks = [
  { label: "Agent 管理", icon: "bot", route: "/app/agents", shortcut: "g a" },
  { label: "记忆管理", icon: "brain", route: "/app/memory", shortcut: "g m" },
  { label: "知识搜索", icon: "search", route: "/app/knowledge?tab=search", shortcut: "g s" },
  { label: "知识库", icon: "book-open", route: "/app/knowledge", shortcut: "g k" },
  { label: "资产", icon: "folder", route: "/app/knowledge?tab=asset", shortcut: "g x" },
  { label: "对话", icon: "messages", route: "/app/agents", shortcut: "g c" },
  { label: "审核", icon: "check-circle", route: "/app/system", shortcut: "g r" },
  { label: "任务", icon: "clock", route: "/app/system", shortcut: "g j" },
  { label: "备份", icon: "archive", route: "/app/system", shortcut: "g b" },
  { label: "审计", icon: "search", route: "/app/system", shortcut: "g u" },
  { label: "健康", icon: "heartbeat", route: "/app/dashboard", shortcut: "g h" },
  { label: "评估", icon: "bar-chart", route: "/app/system", shortcut: "g e" },
  { label: "知识图谱", icon: "graph", route: "/app/graph", shortcut: "g t" },
];

const filteredQuickLinks = computed(() => {
  if (!query.value.trim()) return quickLinks;
  const q = query.value.toLowerCase();
  return quickLinks.filter((l) => l.label.toLowerCase().includes(q));
});

// ── Search ──
function doSearch() {
  const q = query.value.trim();
  if (!q) {
    results.value = [];
    total.value = 0;
    sourceCounts.value = {};
    selectedIndex.value = 0;
    return;
  }

  if (debounceTimer) clearTimeout(debounceTimer);
  debounceTimer = setTimeout(async () => {
    isLoading.value = true;
    error.value = null;
    try {
      const data = await searchGlobal({ q, page_size: 20 });
      results.value = data.items;
      total.value = data.total;
      sourceCounts.value = data.source_counts;
      selectedIndex.value = 0;
    } catch (e: any) {
      error.value = e?.message ?? "搜索失败";
      results.value = [];
    } finally {
      isLoading.value = false;
    }
  }, DEBOUNCE_MS);
}

watch(query, doSearch);

// ── Keyboard navigation ──
const items = computed(() => {
  if (!query.value.trim()) return filteredQuickLinks.value;
  return results.value;
});

const maxIndex = computed(() => Math.max(0, items.value.length - 1));

function handleKeydown(e: KeyboardEvent) {
  if (e.key === "ArrowDown") {
    e.preventDefault();
    selectedIndex.value = Math.min(selectedIndex.value + 1, maxIndex.value);
    nextTick(() => scrollSelectedIntoView());
    return;
  }
  if (e.key === "ArrowUp") {
    e.preventDefault();
    selectedIndex.value = Math.max(selectedIndex.value - 1, 0);
    nextTick(() => scrollSelectedIntoView());
    return;
  }
  if (e.key === "Enter") {
    e.preventDefault();
    selectItem(selectedIndex.value);
    return;
  }
  if (e.key === "Escape") {
    e.preventDefault();
    close();
    return;
  }
}

function scrollSelectedIntoView() {
  const el = document.querySelector<HTMLElement>(".cmdb-item[aria-selected='true']");
  el?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function selectItem(idx: number) {
  const item = items.value[idx];
  if (!item) return;

  // Quick link (no query)
  if ("route" in item) {
    router.push(item.route);
    close();
    return;
  }

  // Search result
  const result = item as GlobalSearchResult;
  if (result.link) {
    router.push(result.link);
  }
  close();
}

function open() {
  visible.value = true;
  query.value = "";
  selectedIndex.value = 0;
  results.value = [];
  error.value = null;
  sourceCounts.value = {};
  total.value = 0;
  nextTick(() => {
    inputRef.value?.focus();
  });
}

function close() {
  visible.value = false;
}

// ── Global keyboard listener (Ctrl+K / Cmd+K) ──
function globalKeydown(e: KeyboardEvent) {
  const tag = (e.target as HTMLElement).tagName;
  const isInput = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (e.target as HTMLElement).isContentEditable;

  // Override the existing Ctrl+K shortcut — open CommandBar instead of navigating
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    e.stopPropagation();
    if (visible.value) {
      close();
    } else {
      open();
    }
    return;
  }

  // Close on Escape when focused in the command bar
  if (e.key === "Escape" && visible.value) {
    // Let the input handler deal with it
  }
}

onMounted(() => {
  document.addEventListener("keydown", globalKeydown, { capture: true });
});

onUnmounted(() => {
  document.removeEventListener("keydown", globalKeydown, { capture: true });
  if (debounceTimer) clearTimeout(debounceTimer);
});

// ── Highlight matching text ──
function highlightText(text: string): string {
  const q = query.value.trim();
  if (!q || !text) return text;
  const escaped = q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const regex = new RegExp(`(${escaped})`, "gi");
  return text.replace(regex, '<mark class="bg-yellow-200 text-yellow-900 rounded px-0.5">$1</mark>');
}

// ── SVG icon sprites ──
function svgIcon(name: string): string {
  const icons: Record<string, string> = {
    bot: '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M9.75 3.104v5.714a2.25 2.25 0 01-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 014.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0112 15a9.065 9.065 0 00-6.23.693L5 14.5m14.8.8l1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0112 21c-2.773 0-5.491-.235-8.135-.687-1.718-.293-2.3-2.379-1.067-3.61L5 14.5" /></svg>',
    brain: '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M4.26 10.147a60.438 60.438 0 00-.491 6.347A48.627 48.627 0 0112 20.904a48.627 48.627 0 018.232-4.41 60.46 60.46 0 00-.491-6.347m-15.482 0a50.57 50.57 0 00-2.658-.813A59.905 59.905 0 0112 3.493a59.902 59.902 0 0110.399 5.84c-.896.248-1.783.52-2.658.814m-15.482 0A50.697 50.697 0 0112 13.489a50.702 50.702 0 017.74-3.342M6.75 15a.75.75 0 100-1.5.75.75 0 000 1.5zm0 0v-3.675A55.378 55.378 0 0112 8.443m-7.007 11.55A5.981 5.981 0 006.75 15.75v-1.5" /></svg>',
    "book-open": '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M12 6.042A8.967 8.967 0 006 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 016 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 016-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0018 18a8.967 8.967 0 00-6 2.292m0-14.25v14.25" /></svg>',
    search: '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" /></svg>',
    folder: '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M2.25 12.75V12A2.25 2.25 0 014.5 9.75h15A2.25 2.25 0 0121.75 12v.75m-8.69-6.44l-2.12-2.12a1.5 1.5 0 00-1.061-.44H4.5A2.25 2.25 0 002.25 6v12a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9a2.25 2.25 0 00-2.25-2.25h-5.379a1.5 1.5 0 01-1.06-.44z" /></svg>',
    messages: '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M20.25 8.511c.884.284 1.5 1.128 1.5 2.097v4.286c0 1.136-.847 2.1-1.98 2.193-.34.027-.68.052-1.02.072v3.091l-3-3c-1.354 0-2.694-.055-4.02-.163a2.115 2.115 0 01-.825-.242m9.345-8.334a2.126 2.126 0 00-.476-.095 48.64 48.64 0 00-8.048 0c-1.131.094-1.976 1.057-1.976 2.192v4.286c0 .837.46 1.58 1.155 1.951m9.345-8.334V6.637c0-1.621-1.152-3.026-2.76-3.235A48.455 48.455 0 0011.25 3c-2.115 0-4.198.137-6.24.402-1.608.209-2.76 1.614-2.76 3.235v6.226c0 1.621 1.152 3.026 2.76 3.235.577.075 1.157.14 1.74.194V21l4.155-4.155" /></svg>',
    "check-circle": '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>',
    clock: '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>',
    archive: '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M20.25 7.5l-.625 10.632a2.25 2.25 0 01-2.247 2.118H6.622a2.25 2.25 0 01-2.247-2.118L3.75 7.5m8.25 3v6.75m0 0l-3-3m3 3l3-3M3.375 7.5h17.25c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125z" /></svg>',
    heartbeat: '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12z" /></svg>',
    "bar-chart": '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" /></svg>',
    graph: '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 3.75v4.5m0-4.5h4.5m-4.5 0L9 9M3.75 20.25v-4.5m0 4.5h4.5m-4.5 0L9 15M20.25 3.75h-4.5m4.5 0v4.5m0-4.5L15 9m5.25 11.25h-4.5m4.5 0v-4.5m0 4.5L15 15" /></svg>',
  };
  return icons[name] ?? icons.search;
}
</script>

<template>
  <Teleport to="body">
    <!-- Backdrop -->
    <Transition name="cmdb-fade">
      <div
        v-if="visible"
        class="fixed inset-0 z-[100] flex items-start justify-center bg-black/40 backdrop-blur-sm pt-[15vh] px-4"
        @click.self="close"
        @keydown="handleKeydown"
      >
        <!-- Panel -->
        <div class="bg-white rounded-2xl shadow-2xl w-full max-w-xl border border-surface-200 overflow-hidden">
          <!-- Search input -->
          <div class="flex items-center gap-3 px-4 py-3 border-b border-surface-200">
            <svg
              xmlns="http://www.w3.org/2000/svg"
              class="h-5 w-5 text-surface-400 shrink-0"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              stroke-width="1.5"
            >
              <path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <input
              ref="inputRef"
              v-model="query"
              type="text"
              placeholder="搜索 Agent、知识库、记忆…"
              class="flex-1 border-none bg-transparent text-sm text-surface-800 placeholder-surface-400 focus:outline-none"
              @keydown="handleKeydown"
            />
            <kbd
              class="hidden sm:inline-flex items-center rounded-md border border-surface-200 bg-surface-50 px-1.5 py-0.5 text-xs font-mono text-surface-400 shrink-0"
            >
              Esc
            </kbd>
          </div>

          <!-- Results area -->
          <div class="max-h-[360px] overflow-y-auto" v-if="!error">

            <!-- Loading spinner -->
            <div v-if="isLoading" class="flex items-center justify-center py-12">
              <svg class="h-5 w-5 animate-spin text-brand-600" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
              </svg>
            </div>

            <!-- Quick links (no query) -->
            <template v-if="!query.trim() && !isLoading">
              <div class="px-4 pt-3 pb-1">
                <p class="text-2xs font-semibold uppercase tracking-wider text-surface-400">快速导航</p>
              </div>
              <div
                v-for="(link, idx) in filteredQuickLinks"
                :key="link.route"
                :ref="(el) => { if (idx === selectedIndex) (el as HTMLElement)?.scrollIntoView?.({ block: 'nearest' }) }"
                :aria-selected="idx === selectedIndex"
                class="cmdb-item flex items-center gap-3 px-4 py-2.5 cursor-pointer transition-colors"
                :class="idx === selectedIndex ? 'bg-brand-50 text-brand-800' : 'hover:bg-surface-50 text-surface-700'"
                @click="selectItem(idx)"
                @mouseenter="selectedIndex = idx"
              >
                <span
                  class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg"
                  :class="idx === selectedIndex ? 'bg-brand-100 text-brand-600' : 'bg-surface-100 text-surface-500'"
                  v-html="svgIcon(link.icon)"
                ></span>
                <span class="flex-1 text-sm font-medium">{{ link.label }}</span>
                <kbd class="text-2xs font-mono text-surface-300">{{ link.shortcut }}</kbd>
              </div>
            </template>

            <!-- Search results -->
            <template v-if="query.trim() && !isLoading">
              <div v-if="results.length > 0" class="px-4 pt-3 pb-1 flex items-center justify-between">
                <p class="text-2xs font-semibold uppercase tracking-wider text-surface-400">
                  搜索结果 ({{ total }})
                </p>
                <div class="flex items-center gap-2">
                  <span
                    v-if="sourceCounts.agent"
                    class="text-2xs badge bg-violet-50 text-violet-700 ring-violet-600/20 ring-1 ring-inset"
                  >Agent {{ sourceCounts.agent }}</span>
                  <span
                    v-if="sourceCounts.knowledge"
                    class="text-2xs badge bg-sky-50 text-sky-700 ring-sky-600/20 ring-1 ring-inset"
                  >知识 {{ sourceCounts.knowledge }}</span>
                  <span
                    v-if="sourceCounts.memory"
                    class="text-2xs badge bg-emerald-50 text-emerald-700 ring-emerald-600/20 ring-1 ring-inset"
                  >记忆 {{ sourceCounts.memory }}</span>
                </div>
              </div>

              <!-- Result items -->
              <div
                v-for="(result, idx) in results"
                :key="result.result_id"
                :aria-selected="idx === selectedIndex"
                class="cmdb-item flex items-start gap-3 px-4 py-2.5 cursor-pointer transition-colors"
                :class="idx === selectedIndex ? 'bg-brand-50' : 'hover:bg-surface-50'"
                @click="selectItem(idx)"
                @mouseenter="selectedIndex = idx"
              >
                <span
                  class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg mt-0.5"
                  :class="idx === selectedIndex
                    ? 'bg-brand-100 text-brand-600'
                    : 'bg-surface-100 text-surface-500'"
                  v-html="svgIcon(result.icon)"
                ></span>
                <div class="flex-1 min-w-0">
                  <div class="flex items-center gap-2">
                    <span class="text-sm font-semibold text-surface-800 truncate">{{ result.title }}</span>
                    <span
                      :class="[
                        'text-2xs badge ring-1 ring-inset shrink-0',
                        (sourceConfig[result.source]?.colorClass ?? 'bg-surface-50 text-surface-500 ring-surface-500/20'),
                      ]"
                    >
                      {{ sourceConfig[result.source]?.label ?? result.source }}
                    </span>
                  </div>
                  <p
                    v-if="result.snippet"
                    class="text-xs text-surface-500 mt-0.5 line-clamp-2 leading-relaxed"
                    v-html="highlightText(result.snippet)"
                  ></p>
                  <div v-if="result.meta?.sensitivity_level || result.meta?.status" class="flex items-center gap-2 mt-1">
                    <span
                      v-if="result.meta?.sensitivity_level"
                      class="text-2xs text-surface-400"
                    >S: {{ result.meta.sensitivity_level }}</span>
                    <span
                      v-if="result.meta?.status"
                      class="text-2xs text-surface-400"
                    >{{ result.meta.status }}</span>
                  </div>
                </div>
              </div>

              <!-- No results -->
              <div v-if="results.length === 0 && !isLoading" class="flex flex-col items-center gap-2 py-12">
                <svg xmlns="http://www.w3.org/2000/svg" class="h-10 w-10 text-surface-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M9.172 16.172a4 4 0 015.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <p class="text-sm text-surface-400">未找到匹配结果</p>
                <p class="text-xs text-surface-300">尝试更换关键词</p>
              </div>
            </template>
          </div>

          <!-- Error -->
          <div v-if="error" class="flex flex-col items-center gap-2 py-12">
            <p class="text-sm text-red-600">{{ error }}</p>
          </div>

          <!-- Footer -->
          <div class="flex items-center justify-between px-4 py-2 border-t border-surface-100 bg-surface-50 text-2xs text-surface-400">
            <div class="flex items-center gap-3">
              <span class="flex items-center gap-1">
                <kbd class="inline-flex items-center rounded border border-surface-200 bg-white px-1 py-0.5 font-mono text-surface-500">↑↓</kbd> 导航
              </span>
              <span class="flex items-center gap-1">
                <kbd class="inline-flex items-center rounded border border-surface-200 bg-white px-1 py-0.5 font-mono text-surface-500">↵</kbd> 选择
              </span>
              <span class="flex items-center gap-1">
                <kbd class="inline-flex items-center rounded border border-surface-200 bg-white px-1 py-0.5 font-mono text-surface-500">Esc</kbd> 关闭
              </span>
            </div>
            <span>Ctrl+K 打开</span>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.cmdb-fade-enter-active,
.cmdb-fade-leave-active {
  transition: opacity 0.15s ease;
}
.cmdb-fade-enter-from,
.cmdb-fade-leave-to {
  opacity: 0;
}

.cmdb-item[aria-selected="true"] {
  /* handled by tailwind classes */
}

.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
