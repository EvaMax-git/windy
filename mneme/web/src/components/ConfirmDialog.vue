<script setup lang="ts">
import { watch } from "vue";

const props = withDefaults(
  defineProps<{
    open: boolean;
    title?: string;
    message?: string;
    confirmLabel?: string;
    cancelLabel?: string;
    variant?: "danger" | "primary";
    loading?: boolean;
  }>(),
  {
    title: "确认操作",
    message: "",
    confirmLabel: "确认",
    cancelLabel: "取消",
    variant: "primary",
    loading: false,
  },
);

const emit = defineEmits<{
  confirm: [];
  cancel: [];
  close: [];
}>();

// Close on Escape key
watch(
  () => props.open,
  (isOpen, _, onCleanup) => {
    if (isOpen) {
      const handler = (e: KeyboardEvent) => {
        if (e.key === "Escape" && !props.loading) {
          emit("cancel");
          emit("close");
        }
      };
      document.addEventListener("keydown", handler);
      onCleanup(() => document.removeEventListener("keydown", handler));
    }
  },
);

// Prevent body scroll when dialog is open
watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
  },
);

function onBackdropClick() {
  if (!props.loading) {
    emit("cancel");
    emit("close");
  }
}

function onConfirm() {
  emit("confirm");
}

function onCancel() {
  emit("cancel");
  emit("close");
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog">
      <div
        v-if="open"
        class="fixed inset-0 z-[9998] flex items-center justify-center p-4"
      >
        <!-- Backdrop -->
        <div
          class="absolute inset-0 bg-black/40 backdrop-blur-sm"
          @click="onBackdropClick"
        ></div>

        <!-- Dialog panel -->
        <div
          class="relative w-full max-w-md rounded-xl bg-white shadow-xl border border-surface-200"
          role="dialog"
          aria-modal="true"
          :aria-label="title"
        >
          <!-- Header -->
          <div class="px-6 pt-6 pb-4">
            <div class="flex items-start gap-3">
              <!-- Icon based on variant -->
              <div
                v-if="variant === 'danger'"
                class="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-red-100 text-red-600"
              >
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  class="h-5 w-5"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  stroke-width="2"
                >
                  <path
                    stroke-linecap="round"
                    stroke-linejoin="round"
                    d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L3.34 15.5c-.77.833.192 2.5 1.732 2.5z"
                  />
                </svg>
              </div>
              <div
                v-else
                class="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-brand-100 text-brand-600"
              >
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  class="h-5 w-5"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  stroke-width="2"
                >
                  <path
                    stroke-linecap="round"
                    stroke-linejoin="round"
                    d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
                  />
                </svg>
              </div>

              <div class="flex-1 min-w-0">
                <h3 class="text-base font-semibold text-surface-900">
                  {{ title }}
                </h3>
                <p v-if="message" class="mt-1 text-sm text-surface-500">
                  {{ message }}
                </p>
              </div>
            </div>
          </div>

          <!-- Slot for custom body content -->
          <div v-if="$slots.default" class="px-6 pb-4">
            <slot />
          </div>

          <!-- Footer actions -->
          <div
            class="flex items-center justify-end gap-3 rounded-b-xl border-t border-surface-100 bg-surface-50 px-6 py-4"
          >
            <button
              class="btn btn-secondary"
              :disabled="loading"
              @click="onCancel"
            >
              {{ cancelLabel }}
            </button>
            <button
              :class="[
                'btn',
                variant === 'danger' ? 'btn-danger' : 'btn-primary',
              ]"
              :disabled="loading"
              @click="onConfirm"
            >
              <!-- Loading spinner -->
              <svg
                v-if="loading"
                class="h-4 w-4 animate-spin"
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
              {{ confirmLabel }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.dialog-enter-active {
  transition: opacity 0.2s ease;
}
.dialog-leave-active {
  transition: opacity 0.15s ease;
}
.dialog-enter-active > :last-child {
  transition: transform 0.2s ease, opacity 0.2s ease;
}
.dialog-leave-active > :last-child {
  transition: transform 0.15s ease, opacity 0.15s ease;
}
.dialog-enter-from,
.dialog-leave-to {
  opacity: 0;
}
.dialog-enter-from > :last-child {
  transform: scale(0.95) translateY(0.5rem);
  opacity: 0;
}
.dialog-leave-to > :last-child {
  transform: scale(0.95) translateY(0.5rem);
  opacity: 0;
}
</style>
