<script setup lang="ts">
defineProps<{
  title: string;
  subtitle?: string;
}>();
</script>

<template>
  <div class="mb-6">
    <h1 class="text-xl font-semibold text-surface-900">{{ title }}</h1>
    <p v-if="subtitle" class="mt-1 text-sm text-surface-500">{{ subtitle }}</p>
  </div>
</template>
