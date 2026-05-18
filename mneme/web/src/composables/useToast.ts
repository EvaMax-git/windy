import { ref } from "vue";

// ── Types ──
export type ToastType = "success" | "error" | "warning" | "info";

export interface Toast {
  id: number;
  type: ToastType;
  title: string;
  message?: string;
  duration?: number;
  actionLabel?: string;
  action?: () => void;
}

// ── Module-level reactive state ──
const toasts = ref<Toast[]>([]);
let nextId = 0;

const MAX_TOASTS = 5;

// ── Composable ──
export function useToast() {
  function addToast(
    type: ToastType,
    title: string,
    opts?: {
      message?: string;
      duration?: number;
      actionLabel?: string;
      action?: () => void;
    },
  ): number {
    const id = nextId++;
    const defaults: Record<ToastType, number> = {
      success: 3000,
      error: 5000,
      warning: 4000,
      info: 3000,
    };

    const toast: Toast = {
      id,
      type,
      title,
      message: opts?.message,
      duration: opts?.duration ?? defaults[type],
      actionLabel: opts?.actionLabel,
      action: opts?.action,
    };

    // Enforce max toast limit — remove oldest if needed
    if (toasts.value.length >= MAX_TOASTS) {
      toasts.value.shift();
    }

    toasts.value.push(toast);

    // Auto-remove after duration
    if (toast.duration && toast.duration > 0) {
      setTimeout(() => {
        removeToast(id);
      }, toast.duration);
    }

    return id;
  }

  function removeToast(id: number): void {
    const idx = toasts.value.findIndex((t) => t.id === id);
    if (idx !== -1) {
      toasts.value.splice(idx, 1);
    }
  }

  function success(title: string, opts?: { message?: string; actionLabel?: string; action?: () => void }): number {
    return addToast("success", title, opts);
  }

  function error(title: string, opts?: { message?: string; duration?: number }): number {
    return addToast("error", title, { ...opts, duration: opts?.duration ?? 5000 });
  }

  function warning(title: string, opts?: { message?: string; duration?: number }): number {
    return addToast("warning", title, { ...opts, duration: opts?.duration ?? 4000 });
  }

  function info(title: string, opts?: { message?: string; duration?: number }): number {
    return addToast("info", title, opts);
  }

  function clearAll(): void {
    toasts.value = [];
  }

  return {
    toasts,
    addToast,
    removeToast,
    success,
    error,
    warning,
    info,
    clearAll,
  };
}
