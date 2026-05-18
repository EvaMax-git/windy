<script setup lang="ts">
import { useToast } from "@/composables/useToast";
import type { ToastType } from "@/composables/useToast";

const { toasts, removeToast } = useToast();

// ── Icon per type ──
const iconSvgs: Record<ToastType, string> = {
  success: `<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>`,
  error: `<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>`,
  warning: `<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L3.34 15.5c-.77.833.192 2.5 1.732 2.5z"/></svg>`,
  info: `<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>`,
};

// ── Color classes per type ──
const colorClasses: Record<ToastType, string> = {
  success: "border-emerald-400 bg-emerald-50 text-emerald-800",
  error: "border-red-400 bg-red-50 text-red-800",
  warning: "border-amber-400 bg-amber-50 text-amber-800",
  info: "border-blue-400 bg-blue-50 text-blue-800",
};

const iconColor: Record<ToastType, string> = {
  success: "text-emerald-500",
  error: "text-red-500",
  warning: "text-amber-500",
  info: "text-blue-500",
};
</script>

<template>
  <Teleport to="body">
    <!-- Toast container: fixed top-right, stacked -->
    <div
      aria-live="polite"
      aria-label="通知"
      class="fixed right-4 top-4 z-[9999] flex flex-col gap-2 w-80 max-w-[calc(100vw-2rem)] pointer-events-none"
    >
      <TransitionGroup name="toast" tag="div" class="flex flex-col gap-2">
        <div
          v-for="toast in toasts"
          :key="toast.id"
          :class="[
            'pointer-events-auto flex items-start gap-3 rounded-lg border px-4 py-3 shadow-lg',
            colorClasses[toast.type],
          ]"
          role="alert"
        >
          <!-- Icon -->
          <div
            :class="['shrink-0 mt-0.5', iconColor[toast.type]]"
            v-html="iconSvgs[toast.type]"
          ></div>

          <!-- Content -->
          <div class="flex-1 min-w-0">
            <p class="text-sm font-semibold">{{ toast.title }}</p>
            <p v-if="toast.message" class="mt-0.5 text-xs opacity-80">
              {{ toast.message }}
            </p>

            <!-- Optional action button -->
            <button
              v-if="toast.actionLabel && toast.action"
              class="mt-1.5 text-xs font-medium underline underline-offset-2 hover:opacity-80 transition-opacity"
              @click="toast.action()"
            >
              {{ toast.actionLabel }}
            </button>
          </div>

          <!-- Close button -->
          <button
            class="shrink-0 rounded p-0.5 opacity-60 hover:opacity-100 transition-opacity"
            @click="removeToast(toast.id)"
            aria-label="关闭通知"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              class="h-4 w-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              stroke-width="2"
            >
              <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
/* ── Toast enter/leave transitions ── */
.toast-enter-active {
  transition: all 0.3s ease-out;
}
.toast-leave-active {
  transition: all 0.2s ease-in;
}
.toast-enter-from {
  opacity: 0;
  transform: translateX(2rem);
}
.toast-leave-to {
  opacity: 0;
  transform: translateX(2rem);
}
</style>
