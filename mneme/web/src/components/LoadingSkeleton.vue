<script setup lang="ts">
/**
 * LoadingSkeleton — reusable skeleton screen component
 *
 * Variants:
 *  - "table"           : table rows with columns
 *  - "card-grid"       : grid of stat/content cards
 *  - "stat-row"        : row of stat cards (health dashboard)
 *  - "detail"          : detail drawer content
 *  - "text-block"      : paragraph text blocks
 *  - "list"            : list items
 *  - "search-results"  : search result cards
 *  - "custom"          : just animate-pulse wrapper
 */
withDefaults(
  defineProps<{
    variant?: "table" | "card-grid" | "stat-row" | "detail" | "text-block" | "list" | "search-results" | "custom";
    rows?: number;
    columns?: number;
    cards?: number;
  }>(),
  {
    variant: "table",
    rows: 5,
    columns: 5,
    cards: 4,
  },
);

// Deterministic widths for table cells to avoid hydration mismatch
const tableCellWidths = [
  ["140px", "80px", "90px", "60px", "120px"],
  ["120px", "100px", "70px", "80px", "100px"],
  ["160px", "90px", "80px", "70px", "110px"],
  ["130px", "110px", "60px", "90px", "90px"],
  ["150px", "70px", "100px", "80px", "130px"],
];

function cellWidth(row: number, col: number): string {
  const rw = tableCellWidths[(row - 1) % tableCellWidths.length];
  return rw[(col - 1) % rw.length];
}
</script>

<template>
  <div class="animate-pulse">
    <!-- TABLE variant -->
    <template v-if="variant === 'table'">
      <div class="overflow-hidden rounded-lg border border-surface-200 bg-white">
        <div class="bg-surface-50 px-4 py-3 flex gap-4">
          <div
            v-for="c in columns"
            :key="'h-' + c"
            class="h-3 rounded bg-surface-200"
            :style="{ width: cellWidth(1, c) }"
          ></div>
        </div>
        <div
          v-for="r in rows"
          :key="'r-' + r"
          class="border-t border-surface-100 px-4 py-3.5 flex gap-4 items-center"
        >
          <div
            v-for="c in columns"
            :key="'c-' + c"
            class="h-4 rounded bg-surface-100"
            :style="{ width: cellWidth(r, c) }"
          ></div>
        </div>
      </div>
    </template>

    <!-- CARD-GRID variant -->
    <template v-else-if="variant === 'card-grid'">
      <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <div
          v-for="c in cards"
          :key="c"
          class="rounded-xl border border-surface-200 bg-white p-5"
        >
          <div class="flex items-start justify-between">
            <div class="space-y-3 flex-1">
              <div class="h-3 w-20 rounded bg-surface-200"></div>
              <div class="h-7 w-28 rounded bg-surface-100"></div>
            </div>
            <div class="h-10 w-10 rounded-lg bg-surface-100"></div>
          </div>
          <div class="mt-4 h-3 w-32 rounded bg-surface-100"></div>
        </div>
      </div>
    </template>

    <!-- STAT-ROW variant (health dashboard) -->
    <template v-else-if="variant === 'stat-row'">
      <div class="rounded-xl border border-surface-200 bg-white p-6 mb-8">
        <div class="flex items-center gap-4">
          <div class="h-12 w-12 rounded-full bg-surface-100 shrink-0"></div>
          <div class="space-y-2 flex-1">
            <div class="h-5 w-40 rounded bg-surface-200"></div>
            <div class="h-3 w-28 rounded bg-surface-100"></div>
          </div>
          <div class="h-8 w-16 rounded-lg bg-surface-100 shrink-0"></div>
        </div>
      </div>
      <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div v-for="c in 4" :key="c" class="rounded-xl border border-surface-200 bg-white p-5">
          <div class="flex items-start justify-between">
            <div class="space-y-3">
              <div class="h-3 w-16 rounded bg-surface-200"></div>
              <div class="h-6 w-20 rounded bg-surface-100"></div>
            </div>
            <div class="h-10 w-10 rounded-lg bg-surface-100"></div>
          </div>
          <div class="mt-3 h-3 w-24 rounded bg-surface-100"></div>
        </div>
      </div>
    </template>

    <!-- DETAIL variant -->
    <template v-else-if="variant === 'detail'">
      <div class="space-y-5">
        <div v-for="i in 6" :key="i" class="space-y-2">
          <div class="h-3 w-20 rounded bg-surface-200"></div>
          <div class="h-4 rounded bg-surface-100" :class="i % 3 === 0 ? 'w-3/5' : i % 2 === 0 ? 'w-4/5' : 'w-full'"></div>
        </div>
        <div class="border-t border-surface-200 pt-4 space-y-3">
          <div class="h-3 w-16 rounded bg-surface-200"></div>
          <div class="h-20 rounded bg-surface-100"></div>
        </div>
      </div>
    </template>

    <!-- TEXT-BLOCK variant -->
    <template v-else-if="variant === 'text-block'">
      <div class="space-y-3">
        <div
          v-for="i in (rows || 4)"
          :key="i"
          class="h-4 rounded bg-surface-100"
          :class="i === rows ? 'w-3/5' : 'w-full'"
        ></div>
      </div>
    </template>

    <!-- LIST variant -->
    <template v-else-if="variant === 'list'">
      <div class="space-y-2">
        <div
          v-for="i in (rows || 5)"
          :key="i"
          class="flex items-center gap-3 rounded-lg border border-surface-200 bg-white px-4 py-3"
        >
          <div class="h-8 w-8 rounded-full bg-surface-100 shrink-0"></div>
          <div class="flex-1 space-y-2">
            <div class="h-3.5 w-32 rounded bg-surface-200"></div>
            <div class="h-3 w-48 rounded bg-surface-100"></div>
          </div>
          <div class="h-6 w-16 rounded bg-surface-100 shrink-0"></div>
        </div>
      </div>
    </template>

    <!-- SEARCH-RESULTS variant -->
    <template v-else-if="variant === 'search-results'">
      <div class="space-y-3">
        <div
          v-for="i in (rows || 5)"
          :key="i"
          class="rounded-xl border border-surface-200 bg-white p-4"
        >
          <div class="flex items-start justify-between mb-3">
            <div class="space-y-2 flex-1">
              <div class="h-4 w-48 rounded bg-surface-200"></div>
              <div class="flex gap-2">
                <div class="h-4 w-12 rounded-full bg-surface-100"></div>
                <div class="h-4 w-16 rounded bg-surface-100"></div>
              </div>
            </div>
            <div class="h-3 w-14 rounded bg-surface-100"></div>
          </div>
          <div class="rounded-lg bg-surface-50 p-3 space-y-2">
            <div class="h-3 w-full rounded bg-surface-100"></div>
            <div class="h-3 w-4/5 rounded bg-surface-100"></div>
            <div class="h-3 w-3/5 rounded bg-surface-100"></div>
          </div>
        </div>
      </div>
    </template>

    <!-- CUSTOM variant -->
    <template v-else>
      <slot />
    </template>
  </div>
</template>
