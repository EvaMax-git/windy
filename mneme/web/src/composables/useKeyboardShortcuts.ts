import { onMounted, onUnmounted, ref } from "vue";
import { useRouter } from "vue-router";

/**
 * Global keyboard shortcuts composable.
 *
 * Built-in shortcuts (always active, not in inputs):
 *   Cmd/Ctrl + K   → Focus search (navigate to /app/search)
 *   Cmd/Ctrl + /   → Toggle sidebar
 *   Cmd/Ctrl + B   → Toggle sidebar (alias)
 *   Escape         → Close topmost drawer/modal (via custom event)
 *   g then h/a/m/s/r/d/j/b/k/c/e/o/u/x/p/t  → Go to page
 *   ?              → Show keyboard shortcuts help
 */
export function useKeyboardShortcuts(opts?: {
  onToggleSidebar?: () => void;
}) {
  const router = useRouter();
  const showHelp = ref(false);

  // Pending "g" keypress for vim-style navigation
  let gPending = false;
  let gTimer: ReturnType<typeof setTimeout> | null = null;

  const goMap: Record<string, string> = {
    h: "/app/health",
    a: "/app/agents",
    m: "/app/memory",
    s: "/app/search",
    r: "/app/review",
    d: "/app/dlq",
    j: "/app/jobs",
    b: "/app/backup",
    k: "/app/knowledge",
    c: "/app/conversations",
    e: "/app/eval",
    o: "/app/outbox",
    u: "/app/audit",
    x: "/app/assets",
    p: "/app/context-packs",
    t: "/app/graph",
  };

  function handler(e: KeyboardEvent) {
    const tag = (e.target as HTMLElement).tagName;
    const isInput = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (e.target as HTMLElement).isContentEditable;

    // Cmd/Ctrl + K → global command bar (handled by CommandBar.vue, this is fallback)
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      // CommandBar registers with capture:true, so it'll fire first.
      // This is a no-op fallback — don't navigate, let CommandBar handle it.
      e.preventDefault();
      return;
    }

    // Cmd/Ctrl + / or Cmd/Ctrl + B → toggle sidebar
    if ((e.metaKey || e.ctrlKey) && (e.key === "/" || e.key.toLowerCase() === "b")) {
      e.preventDefault();
      opts?.onToggleSidebar?.();
      return;
    }

    // Escape → dispatch custom event for drawers/modals
    if (e.key === "Escape") {
      // Close help overlay first
      if (showHelp.value) {
        showHelp.value = false;
        return;
      }
      document.dispatchEvent(new CustomEvent("mneme:escape"));
      return;
    }

    // Skip remaining shortcuts when focused on input
    if (isInput) return;

    // ? → show help
    if (e.key === "?" && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      showHelp.value = !showHelp.value;
      return;
    }

    // g-prefixed navigation (vim-style "go to")
    if (gPending) {
      gPending = false;
      if (gTimer) clearTimeout(gTimer);
      const dest = goMap[e.key.toLowerCase()];
      if (dest) {
        router.push(dest);
      }
      return;
    }

    if (e.key === "g" && !e.metaKey && !e.ctrlKey && !e.altKey) {
      gPending = true;
      gTimer = setTimeout(() => {
        gPending = false;
      }, 800);
      return;
    }
  }

  onMounted(() => {
    document.addEventListener("keydown", handler);
  });

  onUnmounted(() => {
    document.removeEventListener("keydown", handler);
  });

  return { showHelp, goMap };
}

/**
 * Shortcut help text for the overlay.
 */
export function getShortcutHelp(): { category: string; items: { keys: string; description: string }[] }[] {
  return [
    {
      category: "导航",
      items: [
        { keys: "⌘/Ctrl + K", description: "全局搜索 (Agent / 知识库 / 记忆)" },
        { keys: "g → h", description: "健康仪表盘" },
        { keys: "g → a", description: "Agent 管理" },
        { keys: "g → m", description: "记忆管理" },
        { keys: "g → s", description: "知识搜索" },
        { keys: "g → r", description: "审核队列" },
        { keys: "g → d", description: "死信队列" },
        { keys: "g → j", description: "任务" },
        { keys: "g → b", description: "备份" },
        { keys: "g → c", description: "对话管理" },
        { keys: "g → e", description: "评估" },
        { keys: "g → k", description: "知识库" },
        { keys: "g → x", description: "资产管理" },
      ],
    },
    {
      category: "界面",
      items: [
        { keys: "⌘/Ctrl + B", description: "展开 / 收起侧边栏" },
        { keys: "⌘/Ctrl + /", description: "展开 / 收起侧边栏" },
        { keys: "Escape", description: "关闭抽屉 / 弹窗 / 帮助" },
        { keys: "?", description: "显示 / 隐藏快捷键帮助" },
      ],
    },
  ];
}
