<script setup lang="ts">
import { computed, watch } from "vue";
import { useRouter, useRoute } from "vue-router";
import { useAuthStore } from "@/stores/auth";

const props = defineProps<{
  collapsed: boolean;
  /** Show as mobile overlay drawer */
  mobileOpen?: boolean;
}>();

const emit = defineEmits<{
  toggle: [];
  "close-mobile": [];
}>();

// Lock body scroll when mobile drawer is open
watch(
  () => props.mobileOpen,
  (open) => {
    if (open) {
      document.body.classList.add("overflow-hidden", "lg:overflow-auto");
    } else {
      document.body.classList.remove("overflow-hidden", "lg:overflow-auto");
    }
  },
);

const router = useRouter();
const route = useRoute();
const auth = useAuthStore();

async function handleLogout() {
  await auth.doLogout();
  router.push("/login");
}

interface NavItem {
  name: string;
  path: string;
  icon: string;
}

/* ── 6 primary navigation entries ── */
const navItems: NavItem[] = [
  { name: "总览",       path: "/app/dashboard",  icon: "dashboard" },
  { name: "知识库",     path: "/app/knowledge",  icon: "book-open" },
  { name: "知识图谱",   path: "/app/graph",      icon: "graph" },
  { name: "记忆库",     path: "/app/memory",     icon: "brain" },
  { name: "知识问答",   path: "/app/chat",       icon: "chat" },
  { name: "Agent 中心", path: "/app/agents",     icon: "bot" },
  { name: "AI 接入",    path: "/app/gateway",    icon: "gateway" },
  { name: "系统",       path: "/app/system",     icon: "system" },
];

function isActive(path: string): boolean {
  return route.path === path || route.path.startsWith(path + "/");
}

function iconSvg(name: string): string {
  const icons: Record<string, string> = {
    dashboard: `<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25a2.25 2.25 0 01-2.25-2.25v-2.25z"/></svg>`,
    "book-open": `<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253"/></svg>`,
    brain: `<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path stroke-linecap="round" stroke-linejoin="round" d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/><path stroke-linecap="round" stroke-linejoin="round" d="M17.599 6.5a3 3 0 0 0 .399-1.375"/><path stroke-linecap="round" stroke-linejoin="round" d="M6.4 12.5a3 3 0 0 1-.4 1.375"/><path stroke-linecap="round" stroke-linejoin="round" d="M18 12.5a3 3 0 0 1 .4 1.375"/><path stroke-linecap="round" stroke-linejoin="round" d="M6 12.5a3 3 0 0 0-.399-1.375"/></svg>`,
    chat: `<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"/></svg>`,
    bot: `<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M8.5 15.5a3.5 3.5 0 007 0h-2a1.5 1.5 0 01-3 0H8.5z"/><rect x="4" y="5" width="16" height="14" rx="3"/><circle cx="9" cy="11" r="1" fill="currentColor"/><circle cx="15" cy="11" r="1" fill="currentColor"/><path stroke-linecap="round" stroke-linejoin="round" d="M12 3v2"/></svg>`,
    gateway: `<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5.25 14.25h13.5m-13.5 0a3 3 0 01-3-3m3 3a3 3 0 100 6h13.5a3 3 0 100-6m-16.5-3a3 3 0 013-3h13.5a3 3 0 013 3m-19.5 0a4.5 4.5 0 01.9-2.7L5.737 5.1a3.375 3.375 0 012.7-1.35h7.126c1.062 0 2.062.5 2.7 1.35l2.587 3.45a4.5 4.5 0 01.9 2.7m0 0h.375a2.625 2.625 0 010 5.25H17.25a2.625 2.625 0 010-5.25h.375"/></svg>`,
    system: `<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 011.37.49l1.296 2.247a1.125 1.125 0 01-.26 1.431l-1.003.827c-.293.241-.438.613-.43.992a7.723 7.723 0 010 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 01-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 01-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 01-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 01-1.369-.49l-1.297-2.247a1.125 1.125 0 01.26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 010-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 01-.26-1.43l1.297-2.247a1.125 1.125 0 011.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>`,
    graph: `<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5"><circle cx="7.5" cy="7.5" r="2.5"/><circle cx="16.5" cy="7.5" r="2.5"/><circle cx="12" cy="16.5" r="2.5"/><line x1="9.5" y1="8.5" x2="14.5" y2="8.5"/><line x1="8.5" y1="9.5" x2="11" y2="14.5"/><line x1="15.5" y1="9.5" x2="13" y2="14.5"/></svg>`,
  };
  return icons[name] || "";
}
</script>

<template>
  <!-- Mobile overlay backdrop -->
  <Transition name="sidebar-backdrop">
    <div
      v-if="mobileOpen"
      class="fixed inset-0 z-40 bg-black/40 backdrop-blur-sm lg:hidden"
      @click="emit('close-mobile')"
    ></div>
  </Transition>

  <!-- Sidebar: hidden on mobile unless mobileOpen, always visible on lg+ -->
  <aside
    :class="[
      'flex flex-col bg-surface-900 text-surface-300 transition-all duration-200 ease-in-out shrink-0',
      // Desktop: collapsed or expanded
      'hidden lg:flex',
      collapsed ? 'lg:w-16' : 'lg:w-56',
      // Mobile: overlay drawer
      mobileOpen
        ? '!flex fixed inset-y-0 left-0 z-50 w-64 shadow-2xl lg:relative lg:shadow-none'
        : '',
    ]"
  >
    <!-- Logo area -->
    <div class="flex h-14 items-center justify-between px-4 border-b border-surface-700/50">
      <router-link
        to="/app/dashboard"
        class="flex items-center gap-2.5 overflow-hidden"
        @click="emit('close-mobile')"
      >
        <div
          class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-600 text-white font-bold text-sm"
        >
          M
        </div>
        <span
          v-if="!collapsed || mobileOpen"
          class="text-sm font-semibold text-white whitespace-nowrap"
        >
          Mneme
        </span>
      </router-link>
      <!-- Close button on mobile -->
      <button
        class="rounded-md p-1 text-surface-400 hover:text-white lg:hidden"
        @click="emit('close-mobile')"
      >
        <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>

    <!-- Navigation -->
    <nav class="flex-1 overflow-y-auto py-3 px-2">
      <ul class="space-y-0.5">
        <li v-for="item in navItems" :key="item.path">
          <router-link
            :to="item.path"
            :class="[
              'flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors duration-150',
              isActive(item.path)
                ? 'bg-brand-600/20 text-brand-300'
                : 'text-surface-400 hover:bg-surface-800 hover:text-surface-200',
            ]"
            :title="collapsed && !mobileOpen ? item.name : undefined"
            @click="emit('close-mobile')"
          >
            <span class="shrink-0" v-html="iconSvg(item.icon)"></span>
            <span v-if="!collapsed || mobileOpen" class="whitespace-nowrap">{{ item.name }}</span>
          </router-link>
        </li>
      </ul>
    </nav>

    <!-- Footer -->
    <div
      class="border-t border-surface-700/50 px-3 py-3 space-y-2"
      :class="collapsed && !mobileOpen ? 'text-center' : ''"
    >
      <!-- User info -->
      <div v-if="auth.isAuthenticated" class="flex items-center gap-2 overflow-hidden">
        <div
          class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-brand-600/30 text-brand-300 text-xs font-semibold"
        >
          {{ (auth.displayName || "U").charAt(0).toUpperCase() }}
        </div>
        <div v-if="!collapsed || mobileOpen" class="min-w-0 flex-1">
          <p class="truncate text-xs font-medium text-surface-300">
            {{ auth.displayName }}
          </p>
          <p class="text-2xs text-surface-500">
            {{ auth.user?.role_code || "" }}
          </p>
        </div>
      </div>

      <!-- Logout button -->
      <button
        v-if="auth.isAuthenticated"
        @click="handleLogout"
        class="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs text-surface-500 hover:bg-surface-800 hover:text-surface-300 transition-colors"
        title="退出登录"
      >
        <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
        </svg>
        <span v-if="!collapsed || mobileOpen">退出登录</span>
      </button>

      <p v-if="!collapsed || mobileOpen" class="text-2xs text-surface-500">
        Mneme · v128
      </p>
      <p v-else class="text-2xs text-surface-500">v1</p>
    </div>
  </aside>
</template>

<style scoped>
.sidebar-backdrop-enter-active,
.sidebar-backdrop-leave-active {
  transition: opacity 0.2s ease;
}
.sidebar-backdrop-enter-from,
.sidebar-backdrop-leave-to {
  opacity: 0;
}
</style>
