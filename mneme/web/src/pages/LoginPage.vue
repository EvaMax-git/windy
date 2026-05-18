<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { useAuthStore } from "@/stores/auth";

const router = useRouter();
const auth = useAuthStore();

const username = ref("");
const password = ref("");
const submitting = ref(false);
const error = ref<string | null>(null);

async function handleLogin() {
  if (!username.value || !password.value) {
    error.value = "用户名和密码不能为空";
    return;
  }

  submitting.value = true;
  error.value = null;

  try {
    await auth.doLogin({
      username: username.value,
      password: password.value,
      device_label: "Mneme 治理界面",
    });
    // 登录成功后跳转到应用
    router.push("/app/health");
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : "登录失败";
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="flex min-h-screen items-center justify-center bg-surface-100">
    <div class="w-full max-w-sm">
      <!-- Logo -->
      <div class="mb-8 text-center">
        <div
          class="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-xl bg-brand-600 text-white text-xl font-bold"
        >
          M
        </div>
        <h1 class="text-xl font-semibold text-surface-900">Mneme 治理平台</h1>
        <p class="mt-1 text-sm text-surface-500">请登录以继续</p>
      </div>

      <!-- Login form -->
      <div class="rounded-lg border border-surface-200 bg-white p-6 shadow-sm">
        <form @submit.prevent="handleLogin" class="space-y-4">
          <div>
            <label
              for="username"
              class="block text-sm font-medium text-surface-700 mb-1"
            >
              用户名
            </label>
            <input
              id="username"
              v-model="username"
              type="text"
              autocomplete="username"
              placeholder="请输入用户名"
              :disabled="submitting"
              class="block w-full rounded-md border border-surface-300 bg-white px-3 py-2 text-sm text-surface-900 placeholder-surface-400 shadow-xs focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:cursor-not-allowed disabled:opacity-50"
            />
          </div>

          <div>
            <label
              for="password"
              class="block text-sm font-medium text-surface-700 mb-1"
            >
              密码
            </label>
            <input
              id="password"
              v-model="password"
              type="password"
              autocomplete="current-password"
              placeholder="请输入密码"
              :disabled="submitting"
              class="block w-full rounded-md border border-surface-300 bg-white px-3 py-2 text-sm text-surface-900 placeholder-surface-400 shadow-xs focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:cursor-not-allowed disabled:opacity-50"
            />
          </div>

          <!-- Error -->
          <div
            v-if="error"
            class="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 border border-red-200"
          >
            {{ error }}
          </div>

          <button
            type="submit"
            :disabled="submitting"
            class="flex w-full items-center justify-center rounded-md bg-brand-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-brand-700 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50 transition-colors"
          >
            <svg
              v-if="submitting"
              class="mr-2 h-4 w-4 animate-spin"
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
            {{ submitting ? "正在登录..." : "登录" }}
          </button>
        </form>
      </div>

      <p class="mt-4 text-center text-2xs text-surface-400">
        Mneme 第二阶段
      </p>
    </div>
  </div>
</template>
