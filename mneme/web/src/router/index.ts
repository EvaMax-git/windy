import { createRouter, createWebHistory } from "vue-router";
import type { RouteRecordRaw } from "vue-router";
import { useAuthStore } from "@/stores/auth";

/* ── Feature Flag: legacy redirects enabled via VITE_LEGACY_REDIRECTS ── */
const FEATURE_LEGACY_REDIRECTS = import.meta.env.VITE_LEGACY_REDIRECTS !== "false";

/* ── Old-route redirect table (valid 30 days from 2026-05-05 → 2026-06-04) ── */
const LEGACY_REDIRECT_EXPIRY = new Date("2026-06-04T00:00:00Z").getTime();

const legacyRedirects: Record<string, { path: string; query?: Record<string, string> }> = {
  "/app/health":         { path: "/app/dashboard" },
  "/app/audit":          { path: "/app/system" },
  "/app/outbox":         { path: "/app/system" },
  "/app/review":         { path: "/app/system" },
  "/app/dlq":            { path: "/app/system" },
  "/app/jobs":           { path: "/app/system" },
  "/app/backup":         { path: "/app/system" },
  "/app/search":         { path: "/app/knowledge", query: { tab: "search" } },
  "/app/assets":         { path: "/app/knowledge", query: { tab: "asset" } },
  "/app/inbox":          { path: "/app/memory" },
  "/app/conversations":  { path: "/app/agents" },
  "/app/context-packs":  { path: "/app/agents" },
  /* graph restored as standalone page — removed from legacy redirects */
  "/app/eval":           { path: "/app/system" },
};

const legacyRoutes: RouteRecordRaw[] = FEATURE_LEGACY_REDIRECTS
  ? Object.entries(legacyRedirects).map(
      ([oldPath, target]) => ({
        path: oldPath,
        redirect: target.query ? { path: target.path, query: target.query } : target.path,
        meta: { legacy: true },
      }),
    )
  : [];

const routes: RouteRecordRaw[] = [
  /* ── Entry points ── */
  {
    path: "/",
    redirect: "/app/dashboard",
  },
  {
    path: "/login",
    name: "login",
    component: () => import("@/pages/LoginPage.vue"),
    meta: { title: "登录", guest: true },
  },
  {
    path: "/app",
    redirect: "/app/dashboard",
  },

  /* ── 7 primary navigation entries ── */
  {
    path: "/app/dashboard",
    name: "dashboard",
    component: () => import("@/pages/DashboardPage.vue"),
    meta: { title: "总览", icon: "dashboard", requiresAuth: true },
  },
  {
    path: "/app/knowledge",
    name: "knowledge",
    component: () => import("@/pages/KnowledgePage.vue"),
    meta: { title: "知识库", icon: "book-open", requiresAuth: true },
  },
  {
    path: "/app/knowledge-v2",
    name: "knowledge-v2",
    component: () => import("@/pages/KnowledgeRedesign.vue"),
    meta: { title: "知识库 (原型)", icon: "book-open", requiresAuth: false, guest: true },
  },
  {
    path: "/app/graph",
    name: "graph",
    component: () => import("@/pages/GraphPage.vue"),
    meta: { title: "知识图谱", icon: "graph", requiresAuth: true },
  },
  {
    path: "/app/memory",
    name: "memory",
    component: () => import("@/pages/MemoryPage.vue"),
    meta: { title: "记忆库", icon: "brain", requiresAuth: true },
  },
  {
    path: "/app/agents",
    name: "agents",
    component: () => import("@/pages/AgentPage.vue"),
    meta: { title: "Agent 中心", icon: "bot", requiresAuth: true },
  },
  {
    path: "/app/gateway",
    name: "gateway",
    component: () => import("@/pages/GatewayPage.vue"),
    meta: { title: "API 管理", icon: "gateway", requiresAuth: true },
  },
  {
    path: "/app/system",
    name: "system",
    component: () => import("@/pages/SystemPage.vue"),
    meta: { title: "系统", icon: "system", requiresAuth: true },
  },

  /* ── Legacy redirects (30-day grace period) ── */
  ...legacyRoutes,

  /* ── Catch-all ── */
  {
    path: "/:pathMatch(.*)*",
    name: "not-found",
    component: () => import("@/pages/NotFoundPage.vue"),
    meta: { title: "404" },
  },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

/* ── Guard: enforce legacy redirect expiry ── */
router.beforeEach((to, _from, next) => {
  // Check if matched route is an expired legacy redirect
  if (to.meta?.legacy && Date.now() > LEGACY_REDIRECT_EXPIRY) {
    next({ name: "not-found" });
    return;
  }
  next();
});

/* ── Guard: auth + title ── */
router.beforeEach(async (to, _from, next) => {
  const title = to.meta?.title;
  document.title = title ? `${title} · Mneme管理后台` : "Mneme管理后台";

  // Allow guest pages (login) through immediately
  if (to.meta?.guest) {
    next();
    return;
  }

  // Skip auth check for non-auth-required pages (like 404)
  if (!to.meta?.requiresAuth) {
    next();
    return;
  }

  // Perform auth check
  const auth = useAuthStore();

  // If we haven't checked auth yet, do it now
  if (!auth.initialCheckDone) {
    const authenticated = await auth.checkAuth();
    if (!authenticated) {
      next({ name: "login", query: { redirect: to.fullPath } });
      return;
    }
  }

  // If not authenticated, redirect to login
  if (!auth.isAuthenticated) {
    next({ name: "login", query: { redirect: to.fullPath } });
    return;
  }

  next();
});

export default router;
