<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from "vue";
import { useRoute } from "vue-router";
import Sidebar from "@/components/Sidebar.vue";
import KeyboardHelpOverlay from "@/components/KeyboardHelpOverlay.vue";
import CommandBar from "@/components/CommandBar.vue";
import ToastNotification from "@/components/ToastNotification.vue";
import { useAuthStore } from "@/stores/auth";
import { useKeyboardShortcuts } from "@/composables/useKeyboardShortcuts";

const route = useRoute();
const auth = useAuthStore();

const sidebarCollapsed = ref(false);
const mobileMenuOpen = ref(false);

const isGuestPage = computed(() => !!route.meta?.guest);

function toggleSidebar() {
  // On mobile (<lg), toggle the mobile overlay instead
  if (window.innerWidth < 1024) {
    mobileMenuOpen.value = !mobileMenuOpen.value;
  } else {
    sidebarCollapsed.value = !sidebarCollapsed.value;
  }
}

// Close mobile menu on route change
const unwatch = ref<ReturnType<typeof import("vue").watch>>();
import { watch } from "vue";
watch(
  () => route.path,
  () => {
    mobileMenuOpen.value = false;
  },
);

// Keyboard shortcuts
const { showHelp } = useKeyboardShortcuts({
  onToggleSidebar: toggleSidebar,
});

// Close mobile menu on Escape via custom event
function onEscape() {
  if (mobileMenuOpen.value) {
    mobileMenuOpen.value = false;
  }
}
onMounted(() => {
  document.addEventListener("mneme:escape", onEscape);
});
onUnmounted(() => {
  document.removeEventListener("mneme:escape", onEscape);
});
</script>

<template>
  <!-- Loading screen while checking auth -->
  <div
    v-if="!auth.initialCheckDone && !isGuestPage"
    class="flex min-h-screen items-center justify-center bg-surface-100"
  >
    <div class="flex flex-col items-center gap-3">
      <svg
        class="h-8 w-8 animate-spin text-brand-600"
        xmlns="http://www.w3.org/2000/svg"
        fill="none"
        viewBox="0 0 24 24"
      >
        <circle
          class="opacity-25"
          cx="12"
          cy="12"
          r="10"
          stroke="currentColor"
          stroke-width="4"
        ></circle>
        <path
          class="opacity-75"
          fill="currentColor"
          d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
        ></path>
      </svg>
      <p class="text-sm text-surface-500">加载中...</p>
    </div>
  </div>

  <!-- Guest pages (login) — no sidebar -->
  <div v-else-if="isGuestPage" class="min-h-screen bg-surface-100">
    <router-view />
  </div>

  <!-- App layout with sidebar -->
  <div v-else class="flex h-screen overflow-hidden bg-surface-50">
    <!-- Sidebar (handles its own mobile overlay) -->
    <Sidebar
      :collapsed="sidebarCollapsed"
      :mobile-open="mobileMenuOpen"
      @toggle="toggleSidebar"
      @close-mobile="mobileMenuOpen = false"
    />

    <!-- Main content area -->
    <div class="flex flex-1 flex-col overflow-hidden min-w-0">
      <!-- Top bar -->
      <header
        class="flex h-14 shrink-0 items-center justify-between border-b border-surface-200 bg-white px-4 sm:px-6"
      >
        <div class="flex items-center gap-2 sm:gap-3">
          <!-- Mobile hamburger / Desktop sidebar toggle -->
          <button
            class="rounded-md p-1.5 text-surface-400 hover:bg-surface-100 hover:text-surface-600 transition-colors"
            @click="toggleSidebar"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              class="h-5 w-5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              stroke-width="2"
            >
              <path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 12h16M4 18h16" />
            </svg>
          </button>
          <nav class="hidden sm:flex items-center gap-1.5 text-sm text-surface-400">
            <span class="font-semibold text-surface-700">Mneme</span>
            <span class="text-surface-300">/</span>
            <span class="text-surface-500">管理后台</span>
          </nav>
          <!-- Mobile page title -->
          <span class="sm:hidden text-sm font-semibold text-surface-700 truncate">
            {{ (route.meta?.title as string) || 'Mneme' }}
          </span>
        </div>

        <div class="flex items-center gap-2 sm:gap-3">
          <!-- Keyboard shortcut hint (desktop only) -->
          <button
            class="hidden lg:flex items-center gap-1 rounded-md px-2 py-1 text-2xs text-surface-400 hover:bg-surface-100 hover:text-surface-600 transition-colors"
            title="键盘快捷键 (?)"
            @click="showHelp = !showHelp"
          >
            <kbd class="inline-flex items-center rounded border border-surface-200 bg-surface-50 px-1 py-0.5 text-2xs font-mono text-surface-500">?</kbd>
            <span>快捷键</span>
          </button>

          <!-- Environment badge -->
          <span
            class="hidden sm:inline-flex items-center gap-1 rounded-full bg-surface-100 px-2.5 py-0.5 text-2xs font-medium text-surface-500"
          >
            <span class="h-1.5 w-1.5 rounded-full bg-emerald-400"></span>
            第二阶段
          </span>
          <!-- API status indicator -->
          <router-link
            to="/app/dashboard"
            class="flex items-center gap-1.5 text-2xs text-surface-400 hover:text-surface-600 transition-colors"
          >
            <span class="relative flex h-2 w-2">
              <span
                class="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75"
              ></span>
              <span class="relative inline-flex h-2 w-2 rounded-full bg-emerald-500"></span>
            </span>
            <span class="hidden sm:inline">API</span>
          </router-link>
        </div>
      </header>

      <!-- Page content -->
      <main class="flex-1 overflow-y-auto">
        <router-view v-slot="{ Component }">
          <transition name="page" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </main>
    </div>
  </div>

  <!-- Keyboard shortcuts help overlay -->
  <KeyboardHelpOverlay :show="showHelp" @close="showHelp = false" />

  <!-- Global search command bar (Ctrl+K) -->
  <CommandBar />

  <!-- Global toast notifications -->
  <ToastNotification />
</template>

<style scoped>
.page-enter-active,
.page-leave-active {
  transition: opacity 0.15s ease;
}
.page-enter-from,
.page-leave-to {
  opacity: 0;
}
</style>
