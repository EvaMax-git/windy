<script setup lang="ts">
import { getShortcutHelp } from "@/composables/useKeyboardShortcuts";

defineProps<{ show: boolean }>();
defineEmits<{ close: [] }>();

const sections = getShortcutHelp();
</script>

<template>
  <Teleport to="body">
    <Transition name="fade">
      <div
        v-if="show"
        class="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4"
        @click.self="$emit('close')"
      >
        <div class="bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[80vh] overflow-y-auto">
          <!-- Header -->
          <div class="flex items-center justify-between px-6 py-4 border-b border-surface-200">
            <div class="flex items-center gap-2">
              <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5 text-brand-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
                <path stroke-linecap="round" stroke-linejoin="round" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
              </svg>
              <h3 class="text-lg font-semibold text-surface-900">键盘快捷键</h3>
            </div>
            <button
              class="rounded-md p-1.5 text-surface-400 hover:bg-surface-100 hover:text-surface-600 transition-colors"
              @click="$emit('close')"
            >
              <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          <!-- Content -->
          <div class="px-6 py-5 space-y-6">
            <div v-for="section in sections" :key="section.category">
              <h4 class="text-xs font-semibold uppercase tracking-wider text-surface-400 mb-3">
                {{ section.category }}
              </h4>
              <div class="space-y-2">
                <div
                  v-for="item in section.items"
                  :key="item.keys"
                  class="flex items-center justify-between py-1.5"
                >
                  <span class="text-sm text-surface-600">{{ item.description }}</span>
                  <div class="flex items-center gap-1">
                    <kbd
                      v-for="(part, i) in item.keys.split(' + ')"
                      :key="i"
                      class="inline-flex items-center rounded-md border border-surface-200 bg-surface-50 px-2 py-0.5 text-xs font-mono font-medium text-surface-700 shadow-sm"
                    >
                      {{ part }}
                    </kbd>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- Footer hint -->
          <div class="px-6 py-3 border-t border-surface-100 text-center">
            <p class="text-xs text-surface-400">
              按 <kbd class="inline-flex items-center rounded border border-surface-200 bg-surface-50 px-1.5 py-0.5 text-2xs font-mono text-surface-500">?</kbd> 或 <kbd class="inline-flex items-center rounded border border-surface-200 bg-surface-50 px-1.5 py-0.5 text-2xs font-mono text-surface-500">Esc</kbd> 关闭
            </p>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
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
