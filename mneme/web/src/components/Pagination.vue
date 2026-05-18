<script setup lang="ts">
import { computed } from "vue";
import type { PageInfo } from "@/types";

const props = defineProps<{
  pageInfo: PageInfo;
}>();

const emit = defineEmits<{
  "page-change": [page: number];
}>();

const pages = computed(() => {
  const { page, total_pages } = props.pageInfo;
  if (total_pages <= 7) {
    return Array.from({ length: total_pages }, (_, i) => i + 1);
  }

  const items: (number | "...")[] = [1];

  if (page > 3) items.push("...");

  const start = Math.max(2, page - 1);
  const end = Math.min(total_pages - 1, page + 1);
  for (let i = start; i <= end; i++) {
    items.push(i);
  }

  if (page < total_pages - 2) items.push("...");

  items.push(total_pages);
  return items;
});
</script>

<template>
  <div
    v-if="pageInfo.total_pages > 1"
    class="flex flex-col sm:flex-row items-center justify-between gap-3 pt-4"
  >
    <p class="text-xs sm:text-sm text-surface-500 order-2 sm:order-1">
      显示第 {{ (pageInfo.page - 1) * pageInfo.page_size + 1 }}–
      {{ Math.min(pageInfo.page * pageInfo.page_size, pageInfo.total_items) }} 条，
      共 {{ pageInfo.total_items }} 条
    </p>

    <nav class="flex items-center gap-1 order-1 sm:order-2 flex-wrap justify-center">
      <button
        :disabled="!pageInfo.has_previous"
        class="btn btn-secondary btn-sm"
        @click="emit('page-change', pageInfo.page - 1)"
      >
        上一页
      </button>

      <!-- Desktop: full pagination -->
      <div class="hidden sm:flex items-center gap-1">
        <template v-for="p in pages" :key="p">
          <span v-if="p === '...'" class="px-2 text-surface-400 text-sm">…</span>
          <button
            v-else
            :class="[
              'btn btn-sm min-w-[2rem]',
              p === pageInfo.page ? 'btn-primary' : 'btn-secondary',
            ]"
            @click="emit('page-change', p)"
          >
            {{ p }}
          </button>
        </template>
      </div>

      <!-- Mobile: just show current/total -->
      <span class="sm:hidden text-xs text-surface-500 px-2">
        {{ pageInfo.page }} / {{ pageInfo.total_pages }}
      </span>

      <button
        :disabled="!pageInfo.has_next"
        class="btn btn-secondary btn-sm"
        @click="emit('page-change', pageInfo.page + 1)"
      >
        下一页
      </button>
    </nav>
  </div>
</template>
