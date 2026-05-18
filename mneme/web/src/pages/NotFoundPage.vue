<script setup lang="ts">
import { useRouter } from "vue-router";

const router = useRouter();
</script>

<template>
  <div class="flex h-full flex-col items-center justify-center py-20">
    <p class="text-7xl font-bold text-surface-200">404</p>
    <h2 class="mt-4 text-lg font-semibold text-surface-500">页面未找到</h2>
    <p class="mt-1 text-sm text-surface-400">
      您访问的页面不存在。
    </p>
    <button class="btn btn-primary mt-6" @click="router.push('/app/dashboard')">
      前往总览
    </button>
  </div>
</template>
