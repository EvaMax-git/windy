<script setup lang="ts">
import { watch } from "vue";

const props = defineProps<{
  open: boolean;
  title?: string;
  width?: string;
}>();

const emit = defineEmits<{
  close: [];
}>();

// Close on Escape key
watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) {
      const handler = (e: KeyboardEvent) => {
        if (e.key === "Escape") emit("close");
      };
      document.addEventListener("keydown", handler);
      return () => document.removeEventListener("keydown", handler);
    }
  },
);
</script>

<template>
  <Teleport to="body">
    <Transition name="drawer">
      <div v-if="open" class="fixed inset-0 z-50 flex justify-end">
        <!-- Backdrop -->
        <div
          class="absolute inset-0 bg-black/30 backdrop-blur-sm"
          @click="emit('close')"
        ></div>

        <!-- Drawer panel -->
        <div
          :class="[
            'relative flex h-full flex-col bg-white shadow-drawer',
            width || 'w-96 max-w-full',
          ]"
        >
          <!-- Header -->
          <div class="flex items-center justify-between border-b border-surface-200 px-5 py-4">
            <h3 class="text-base font-semibold text-surface-900">
              {{ title || "详情" }}
            </h3>
            <button
              class="rounded-md p-1 text-surface-400 hover:bg-surface-100 hover:text-surface-600 transition-colors"
              @click="emit('close')"
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                class="h-5 w-5"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                stroke-width="2"
              >
                <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          <!-- Body -->
          <div class="flex-1 overflow-y-auto px-5 py-4">
            <slot />
          </div>

          <!-- Footer -->
          <div v-if="$slots.footer" class="border-t border-surface-200 px-5 py-4">
            <slot name="footer" />
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.drawer-enter-active,
.drawer-leave-active {
  transition: opacity 0.2s ease;
}
.drawer-enter-active > :last-child,
.drawer-leave-active > :last-child {
  transition: transform 0.2s ease;
}
.drawer-enter-from,
.drawer-leave-to {
  opacity: 0;
}
.drawer-enter-from > :last-child,
.drawer-leave-to > :last-child {
  transform: translateX(100%);
}
</style>
