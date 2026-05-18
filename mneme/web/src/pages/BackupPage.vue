<script setup lang="ts">
import { ref, computed } from "vue";
import { useQuery, useMutation, useQueryClient } from "@tanstack/vue-query";
import { useRouter } from "vue-router";
import PageHeader from "@/components/PageHeader.vue";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import {
  fetchBackups,
  fetchBackupDetail,
  triggerBackup,
  previewRestore,
  submitRestore,
  fetchRestores,
  executeRestoreDrill,
} from "@/api/client";
import type { BackupSummary, BackupDetail, RestoreDetailedPreview, RestoreSummary } from "@/types";

const router = useRouter();
const queryClient = useQueryClient();

// ── Tabs ──
type Tab = "backups" | "restores";
const activeTab = ref<Tab>("backups");

// ── Backups ──
const backupPage = ref(1);
const backupPageSize = ref(25);

const backupQueryKey = computed(() => ["backups", backupPage.value, backupPageSize.value] as const);
const { data: backupData, isLoading: backupLoading, isError: backupError, error: backupErr } = useQuery({
  queryKey: backupQueryKey,
  queryFn: () => fetchBackups({ page: backupPage.value, page_size: backupPageSize.value }),
  placeholderData: (prev) => prev,
});

// ── Restores ──
const restorePage = ref(1);
const restorePageSize = ref(25);

const restoreQueryKey = computed(() => ["restores", restorePage.value, restorePageSize.value] as const);
const { data: restoreData, isLoading: restoreLoading, isError: restoreError, error: restoreErr } = useQuery({
  queryKey: restoreQueryKey,
  queryFn: () => fetchRestores({ page: restorePage.value, page_size: restorePageSize.value }),
  placeholderData: (prev) => prev,
});

// ── Trigger backup ──
const confirmBackupOpen = ref(false);
const triggerMutation = useMutation({
  mutationFn: () => triggerBackup(),
  onSuccess: () => {
    confirmBackupOpen.value = false;
    queryClient.invalidateQueries({ queryKey: ["backups"] });
    queryClient.invalidateQueries({ queryKey: ["jobs"] });
  },
});

// ── Backup detail ──
const selectedBackup = ref<BackupDetail | null>(null);
const backupDetailOpen = ref(false);
const backupDetailLoading = ref(false);

// ── Restore flow ──
const restorePreview = ref<RestoreDetailedPreview | null>(null);
const restorePreviewOpen = ref(false);
const restorePreviewLoading = ref(false);
const restoreReason = ref("");

const restoreMutation = useMutation({
  mutationFn: ({
    backupId,
    reason,
  }: {
    backupId: string;
    reason: string;
  }) => submitRestore(backupId, reason),
  onSuccess: (data) => {
    restorePreviewOpen.value = false;
    router.push(`/app/review`);
  },
});

const drillMutation = useMutation({
  mutationFn: (backupId: string) => executeRestoreDrill(backupId),
  onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ["restores"] });
  },
});

// ── Columns ──
const backupColumns = [
  { key: "created_at", label: "创建时间", width: "170px" },
  { key: "backup_id", label: "备份ID" },
  { key: "status", label: "状态", width: "110px" },
  { key: "file_size_bytes", label: "大小", width: "100px" },
  { key: "tables", label: "表数", width: "80px" },
];

const restoreColumns = [
  { key: "started_at", label: "开始时间", width: "170px" },
  { key: "backup_id", label: "备份ID" },
  { key: "restore_type", label: "类型", width: "80px" },
  { key: "status", label: "状态", width: "110px" },
  { key: "target_database", label: "目标库" },
];

// ── Helpers ──
function formatTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function formatBytes(bytes: number): string {
  if (!bytes || bytes === 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
}

async function openBackupDetail(item: BackupSummary) {
  backupDetailLoading.value = true;
  backupDetailOpen.value = true;
  selectedBackup.value = null;
  try {
    const detail = await fetchBackupDetail(item.backup_id);
    selectedBackup.value = detail;
  } catch {
    // Keep drawer open
  } finally {
    backupDetailLoading.value = false;
  }
}

async function openRestorePreview(item: BackupSummary) {
  restorePreviewLoading.value = true;
  restorePreviewOpen.value = true;
  restorePreview.value = null;
  restoreReason.value = "";
  try {
    const preview = await previewRestore(item.backup_id);
    restorePreview.value = preview;
  } catch {
    // Keep drawer open
  } finally {
    restorePreviewLoading.value = false;
  }
}

function handleRestore() {
  if (!restorePreview.value) return;
  restoreMutation.mutate({
    backupId: restorePreview.value.backup_id,
    reason: restoreReason.value || "通过治理界面发起恢复请求",
  });
}

function handleDrill(item: BackupSummary) {
  drillMutation.mutate(item.backup_id);
}

// ── will_be 中文映射 ──
const WILL_BE_LABELS: Record<string, string> = {
  overwritten: "覆盖",
  missing_in_backup: "备份缺失",
  created: "新建",
  unchanged: "无变化",
};

function willBeLabel(key: string): string {
  return WILL_BE_LABELS[key] ?? key;
}
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader
      title="备份与恢复"
      subtitle="数据库备份管理、恢复操作与灾难恢复演练"
    />

    <!-- Action bar -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="flex gap-2">
          <button
            :class="[
              'btn btn-sm',
              activeTab === 'backups' ? 'btn-primary' : 'btn-secondary',
            ]"
            @click="activeTab = 'backups'"
          >
            备份列表
          </button>
          <button
            :class="[
              'btn btn-sm',
              activeTab === 'restores' ? 'btn-primary' : 'btn-secondary',
            ]"
            @click="activeTab = 'restores'"
          >
            恢复历史
          </button>
        </div>

        <button
          v-if="activeTab === 'backups'"
          class="btn btn-primary btn-sm"
          :disabled="triggerMutation.isPending.value"
          @click="confirmBackupOpen = true"
        >
          <svg
            v-if="triggerMutation.isPending.value"
            class="h-3.5 w-3.5 animate-spin"
            xmlns="http://www.w3.org/2000/svg"
            fill="none"
            viewBox="0 0 24 24"
          >
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
          {{ triggerMutation.isPending.value ? "备份中..." : "立即备份" }}
        </button>
      </div>
    </div>

    <!-- ══════════════ BACKUPS TAB ══════════════ -->
    <template v-if="activeTab === 'backups'">
      <!-- Error -->
      <div
        v-if="backupError"
        class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
      >
        加载备份列表失败: {{ (backupErr as Error)?.message }}
      </div>

      <!-- Table -->
      <DataTable
        :items="backupData?.items ?? []"
        :columns="backupColumns"
        :loading="backupLoading"
        empty-message="未找到备份。请点击上方按钮创建首个备份。"
        clickable
        row-key="backup_id"
        @row-click="openBackupDetail"
      >
        <template #cell-created_at="{ value }">
          <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
        </template>

        <template #cell-backup_id="{ value }">
          <span class="font-mono text-xs text-surface-600">{{ (value as string).slice(0, 12) }}...</span>
        </template>

        <template #cell-status="{ value }">
          <StatusBadge :status="value as string" />
        </template>

        <template #cell-file_size_bytes="{ value }">
          <span class="font-mono text-xs">{{ formatBytes(value as number) }}</span>
        </template>

        <template #cell-tables="{ value }">
          <span class="font-mono text-xs">{{ value }}</span>
        </template>
      </DataTable>

      <Pagination
        v-if="backupData?.page_info"
        :page-info="backupData.page_info"
        @page-change="(p: number) => (backupPage = p)"
      />

      <!-- Backup Detail Drawer -->
      <DetailDrawer
        :open="backupDetailOpen"
        title="备份详情"
        width="w-[480px] max-w-full"
        @close="backupDetailOpen = false"
      >
        <div v-if="backupDetailLoading" class="py-4">
          <LoadingSkeleton variant="detail" />
        </div>

        <template v-else-if="selectedBackup">
          <dl class="space-y-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">备份ID</dt>
              <dd class="mt-1 font-mono text-xs text-surface-700 break-all">{{ selectedBackup.backup_id }}</dd>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">状态</dt>
                <dd class="mt-1"><StatusBadge :status="selectedBackup.status" /></dd>
              </div>
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">PG 版本</dt>
                <dd class="mt-1 font-mono text-xs">{{ selectedBackup.pg_version }}</dd>
              </div>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">格式</dt>
                <dd class="mt-1 text-xs">{{ selectedBackup.format }}</dd>
              </div>
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">表数</dt>
                <dd class="mt-1 font-mono text-xs">{{ selectedBackup.tables }}</dd>
              </div>
            </div>

            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">大小</dt>
              <dd class="mt-1 font-mono text-xs">{{ formatBytes(selectedBackup.file_size_bytes) }}</dd>
            </div>

            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">校验和 (SHA-256)</dt>
              <dd class="mt-1 font-mono text-2xs text-surface-500 break-all">{{ selectedBackup.checksum_sha256 }}</dd>
            </div>

            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">Alembic 版本</dt>
              <dd class="mt-1 font-mono text-xs text-surface-500">{{ selectedBackup.alembic_revision }}</dd>
            </div>

            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt>
              <dd class="mt-1 text-xs">{{ formatTime(selectedBackup.created_at) }}</dd>
            </div>

            <div v-if="selectedBackup.completed_at">
              <dt class="text-2xs font-semibold uppercase text-surface-400">完成时间</dt>
              <dd class="mt-1 text-xs">{{ formatTime(selectedBackup.completed_at) }}</dd>
            </div>

            <div v-if="selectedBackup.error_message">
              <dt class="text-2xs font-semibold uppercase text-surface-400">错误</dt>
              <dd class="mt-1 font-mono text-xs text-red-600 whitespace-pre-wrap">{{ selectedBackup.error_message }}</dd>
            </div>

            <!-- Row counts summary -->
            <div v-if="selectedBackup.table_row_counts && Object.keys(selectedBackup.table_row_counts).length > 0">
              <dt class="text-2xs font-semibold uppercase text-surface-400 mb-2">
                行数统计 ({{ Object.keys(selectedBackup.table_row_counts).length }} 个表)
              </dt>
              <dd>
                <div class="rounded-lg border border-surface-200 divide-y divide-surface-100 max-h-48 overflow-y-auto">
                  <div
                    v-for="(count, table) in selectedBackup.table_row_counts"
                    :key="table"
                    class="flex justify-between px-3 py-1.5 text-xs"
                  >
                    <span class="font-mono text-surface-600">{{ table }}</span>
                    <span class="font-mono text-surface-500 tabular-nums">{{ count.toLocaleString() }}</span>
                  </div>
                </div>
              </dd>
            </div>
          </dl>
        </template>

        <div v-else class="flex flex-col items-center justify-center py-12 text-sm text-surface-400">
          加载备份详情失败
        </div>

        <!-- Footer actions -->
        <template v-if="selectedBackup" #footer>
          <div class="space-y-2">
            <button
              class="btn btn-primary btn-sm w-full"
              :disabled="restorePreviewLoading"
              @click="openRestorePreview(selectedBackup as unknown as BackupSummary)"
            >
              从此备份恢复
            </button>
            <button
              class="btn btn-secondary btn-sm w-full"
              :disabled="drillMutation.isPending.value"
              @click="handleDrill(selectedBackup as unknown as BackupSummary)"
            >
              {{
                drillMutation.isPending.value
                  ? "演练中..."
                  : "执行恢复演练"
              }}
            </button>
          </div>
        </template>
      </DetailDrawer>

      <!-- Restore Preview Dialog -->
      <DetailDrawer
        :open="restorePreviewOpen"
        title="恢复预览"
        width="w-[540px] max-w-full"
        @close="restorePreviewOpen = false"
      >
        <div v-if="restorePreviewLoading" class="py-4">
          <LoadingSkeleton variant="detail" />
        </div>

        <template v-else-if="restorePreview">
          <!-- Summary cards -->
          <div class="grid grid-cols-3 gap-3 mb-5">
            <div class="rounded-lg bg-surface-50 p-3 text-center">
              <p class="text-2xs font-semibold uppercase text-surface-400">备份行数</p>
              <p class="mt-1 font-mono text-lg font-bold text-surface-700 tabular-nums">
                {{ restorePreview.total_rows_backup.toLocaleString() }}
              </p>
            </div>
            <div class="rounded-lg bg-surface-50 p-3 text-center">
              <p class="text-2xs font-semibold uppercase text-surface-400">当前行数</p>
              <p class="mt-1 font-mono text-lg font-bold text-surface-700 tabular-nums">
                {{ restorePreview.total_rows_live.toLocaleString() }}
              </p>
            </div>
            <div class="rounded-lg bg-surface-50 p-3 text-center">
              <p class="text-2xs font-semibold uppercase text-surface-400">表</p>
              <p class="mt-1 font-mono text-lg font-bold text-surface-700 tabular-nums">
                {{ restorePreview.backup_tables }} / {{ restorePreview.live_tables }}
              </p>
            </div>
          </div>

          <!-- Impact summary -->
          <div class="flex flex-wrap gap-2 mb-4">
            <span class="badge bg-amber-50 text-amber-700 ring-amber-600/20 ring-1 ring-inset">
              {{ restorePreview.will_overwrite_tables }} 覆盖
            </span>
            <span v-if="restorePreview.will_create_tables > 0" class="badge bg-emerald-50 text-emerald-700 ring-emerald-600/20 ring-1 ring-inset">
              {{ restorePreview.will_create_tables }} 新建
            </span>
            <span v-if="restorePreview.will_drop_tables > 0" class="badge bg-red-50 text-red-700 ring-red-600/20 ring-1 ring-inset">
              {{ restorePreview.will_drop_tables }} 删除
            </span>
          </div>

          <!-- Warnings -->
          <div v-if="restorePreview.warnings && restorePreview.warnings.length > 0" class="mb-4">
            <div
              v-for="(w, i) in restorePreview.warnings"
              :key="i"
              class="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700"
            >
              {{ w }}
            </div>
          </div>

          <!-- Table comparisons -->
          <div v-if="restorePreview.table_comparisons && restorePreview.table_comparisons.length > 0">
            <dt class="text-2xs font-semibold uppercase text-surface-400 mb-2">表对比</dt>
            <dd>
              <div class="rounded-lg border border-surface-200 divide-y divide-surface-100 max-h-48 overflow-y-auto">
                <div
                  v-for="tc in restorePreview.table_comparisons"
                  :key="tc.table_name"
                  class="grid grid-cols-4 gap-2 px-3 py-1.5 text-xs items-center"
                >
                  <span class="font-mono text-surface-600 truncate">{{ tc.table_name }}</span>
                  <span class="font-mono text-surface-500 text-right tabular-nums">{{ tc.backup_rows.toLocaleString() }}</span>
                  <span class="font-mono text-surface-400 text-right tabular-nums">{{ tc.live_rows.toLocaleString() }}</span>
                  <span
                    class="font-mono text-right tabular-nums"
                    :class="{
                      'text-red-600 font-medium': tc.will_be === 'overwritten' || tc.will_be === 'missing_in_backup',
                      'text-emerald-600': tc.will_be === 'created',
                      'text-surface-400': tc.will_be === 'unchanged',
                    }"
                  >
                    {{ willBeLabel(tc.will_be) }}
                  </span>
                </div>
              </div>
            </dd>
          </div>
        </template>

        <div v-else class="flex flex-col items-center justify-center py-12 text-sm text-surface-400">
          加载恢复预览失败
        </div>

        <!-- Footer: submit restore -->
        <template v-if="restorePreview && !restorePreview.error" #footer>
          <div class="space-y-3">
            <div>
              <label class="block text-2xs font-semibold uppercase text-surface-400 mb-1">
                恢复原因
              </label>
              <textarea
                v-model="restoreReason"
                rows="2"
                placeholder="例如：数据损坏恢复、定期演练..."
                class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 resize-none"
              ></textarea>
            </div>
            <button
              class="btn btn-danger btn-sm w-full"
              :disabled="restoreMutation.isPending.value"
              @click="handleRestore"
            >
              {{
                restoreMutation.isPending.value
                  ? "提交中..."
                  : "提交恢复申请进行审核"
              }}
            </button>
            <p class="text-2xs text-surface-400 text-center">
              恢复执行前需要审核批准。
            </p>
          </div>
        </template>
      </DetailDrawer>
    </template>

    <!-- ══════════════ RESTORES TAB ══════════════ -->
    <template v-if="activeTab === 'restores'">
      <div
        v-if="restoreError"
        class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
      >
        加载恢复历史失败: {{ (restoreErr as Error)?.message }}
      </div>

      <DataTable
        :items="restoreData?.items ?? []"
        :columns="restoreColumns"
        :loading="restoreLoading"
        empty-message="暂无恢复操作记录。"
        row-key="restore_id"
      >
        <template #cell-started_at="{ value }">
          <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
        </template>

        <template #cell-backup_id="{ value }">
          <span class="font-mono text-xs text-surface-600">{{ (value as string).slice(0, 12) }}...</span>
        </template>

        <template #cell-restore_type="{ value }">
          <span class="font-mono text-xs uppercase">{{ value }}</span>
        </template>

        <template #cell-status="{ value }">
          <StatusBadge :status="value as string" />
        </template>

        <template #cell-target_database="{ value }">
          <span class="font-mono text-2xs text-surface-400 truncate max-w-[180px] block">{{ value || "—" }}</span>
        </template>
      </DataTable>

      <Pagination
        v-if="restoreData?.page_info"
        :page-info="restoreData.page_info"
        @page-change="(p: number) => (restorePage = p)"
      />
    </template>

    <!-- Confirm Backup Dialog -->
    <DetailDrawer
      :open="confirmBackupOpen"
      title="触发备份"
      width="w-[420px] max-w-full"
      @close="confirmBackupOpen = false"
    >
      <div class="space-y-4">
        <div class="rounded-lg bg-brand-50 p-4 text-sm text-brand-700">
          <p class="font-medium">将使用 pg_dump 创建完整数据库备份。</p>
          <p class="mt-1 text-xs text-brand-600">
            备份将以异步任务方式运行。您可以在
            <router-link to="/app/jobs" class="underline">任务页面</router-link> 跟踪其进度。
          </p>
        </div>
      </div>

      <template #footer>
        <div class="flex gap-2">
          <button
            class="btn btn-secondary flex-1 btn-sm"
            @click="confirmBackupOpen = false"
          >
            取消
          </button>
          <button
            class="btn btn-primary flex-1 btn-sm"
            :disabled="triggerMutation.isPending.value"
            @click="triggerMutation.mutate()"
          >
            {{ triggerMutation.isPending.value ? "启动中..." : "开始备份" }}
          </button>
        </div>
      </template>
    </DetailDrawer>
  </div>
</template>
