import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { login as apiLogin, me as apiMe, logoutApi } from "@/api/client";
import type { UserRead, UserSessionRead, LoginRequest } from "@/types";

export const useAuthStore = defineStore("auth", () => {
  const user = ref<UserRead | null>(null);
  const session = ref<UserSessionRead | null>(null);
  const loading = ref(false);
  const initialCheckDone = ref(false);
  const error = ref<string | null>(null);

  const isAuthenticated = computed(() => user.value !== null && session.value !== null);
  const isOwner = computed(() => user.value?.role_code === "owner");
  const isOperator = computed(() => user.value?.role_code === "operator");
  const displayName = computed(() => user.value?.display_name || user.value?.username || "");

  async function checkAuth(): Promise<boolean> {
    loading.value = true;
    error.value = null;
    try {
      const result = await apiMe();
      user.value = result.user;
      session.value = result.session;
      initialCheckDone.value = true;
      return true;
    } catch (e) {
      user.value = null;
      session.value = null;
      initialCheckDone.value = true;
      return false;
    } finally {
      loading.value = false;
    }
  }

  async function doLogin(payload: LoginRequest): Promise<boolean> {
    loading.value = true;
    error.value = null;
    try {
      const result = await apiLogin(payload);
      user.value = result.user;
      session.value = result.session;
      return true;
    } catch (e) {
      user.value = null;
      session.value = null;
      error.value = e instanceof Error ? e.message : "登录失败";
      throw e;
    } finally {
      loading.value = false;
    }
  }

  async function doLogout(): Promise<void> {
    try {
      await logoutApi("user initiated");
    } catch {
      // logout should succeed even if server-side revocation fails
    }
    user.value = null;
    session.value = null;
  }

  return {
    user,
    session,
    loading,
    initialCheckDone,
    error,
    isAuthenticated,
    isOwner,
    isOperator,
    displayName,
    checkAuth,
    doLogin,
    doLogout,
  };
});
