<script setup lang="ts" generic="T">
import { computed } from "vue";

const props = defineProps<{
  items: T[];
  columns: {
    key: string;
    label: string;
    width?: string;
    align?: "left" | "center" | "right";
    sortable?: boolean;
  }[];
  loading?: boolean;
  emptyMessage?: string;
  rowKey?: string;
  clickable?: boolean;
}>();

defineEmits<{
  "row-click": [item: T];
}>();

const emptyMsg = computed(() => props.emptyMessage ?? "暂无数据");

/* eslint-disable @typescript-eslint/no-explicit-any */
function cellValue(item: T, colKey: string): unknown {
  return (item as Record<string, any>)[colKey];
}

function itemKey(item: T, key: string): string {
  return String((item as Record<string, any>)[key]);
}
</script>

<template>
  <div class="overflow-hidden rounded-lg border border-surface-200 bg-white">
    <div class="overflow-x-auto">
      <table class="min-w-full divide-y divide-surface-200">
        <thead class="bg-surface-50">
          <tr>
            <th
              v-for="col in columns"
              :key="col.key"
              :class="[
                'px-4 py-3 text-2xs font-semibold uppercase tracking-wider text-surface-500',
                col.align === 'right' ? 'text-right' : '',
                col.align === 'center' ? 'text-center' : '',
              ]"
              :style="col.width ? { width: col.width } : {}"
            >
              {{ col.label }}
            </th>
          </tr>
        </thead>

        <!-- Loading skeleton -->
        <tbody v-if="loading">
          <tr v-for="i in 5" :key="i" class="animate-pulse">
            <td v-for="col in columns" :key="col.key" class="px-4 py-3">
              <div class="h-4 rounded bg-surface-100"></div>
            </td>
          </tr>
        </tbody>

        <!-- Empty state -->
        <tbody v-else-if="items.length === 0">
          <tr>
            <td
              :colspan="columns.length"
              class="px-4 py-12 text-center text-sm text-surface-400"
            >
              <div class="flex flex-col items-center gap-2">
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  class="h-8 w-8 text-surface-300"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  stroke-width="1"
                >
                  <path
                    stroke-linecap="round"
                    stroke-linejoin="round"
                    d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4"
                  />
                </svg>
                <span>{{ emptyMsg }}</span>
              </div>
            </td>
          </tr>
        </tbody>

        <!-- Data rows -->
        <tbody v-else class="divide-y divide-surface-100">
          <tr
            v-for="(item, idx) in items"
            :key="rowKey ? itemKey(item, rowKey) : idx"
            :class="[
              'transition-colors',
              clickable ? 'cursor-pointer hover:bg-surface-50' : '',
            ]"
            @click="clickable ? $emit('row-click', item) : undefined"
          >
            <td
              v-for="col in columns"
              :key="col.key"
              :class="[
                'px-4 py-3 text-sm text-surface-700 whitespace-nowrap',
                col.align === 'right' ? 'text-right' : '',
                col.align === 'center' ? 'text-center' : '',
              ]"
            >
              <slot
                :name="`cell-${col.key}`"
                :item="item"
                :value="cellValue(item, col.key)"
              >
                {{ cellValue(item, col.key) }}
              </slot>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
