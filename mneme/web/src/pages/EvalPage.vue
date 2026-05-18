<script setup lang="ts">
import { ref, computed } from "vue";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import PageHeader from "@/components/PageHeader.vue";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import {
  fetchEvalTasks,
  fetchEvalTask,
  fetchEvalTaskResults,
  runEvalTask,
  cancelEvalTask,
  createEvalTask,
} from "@/api/client";
import type { EvalTask, EvalTaskDetail, EvalResultItem, EvalMetricSummary } from "@/types";

// ── List state ──
const page = ref(1);
const pageSize = ref(50);
const filterStatus = ref("");
const filterTaskType = ref("");

const listKey = computed(() => [
  "eval-tasks",
  page.value,
  pageSize.value,
  filterStatus.value,
  filterTaskType.value,
] as const);

const { data, isLoading, isError, error } = useQuery({
  queryKey: listKey,
  queryFn: () =>
    fetchEvalTasks({
      page: page.value,
      page_size: pageSize.value,
      status: filterStatus.value || undefined,
      task_type: filterTaskType.value || undefined,
    }),
  placeholderData: (prev) => prev,
});

// ── Detail drawer ──
const selectedTaskId = ref<string | null>(null);
const drawerTab = ref<"info" | "metrics" | "results">("info");
const drawerOpen = ref(false);

const { data: taskDetail, isLoading: detailLoading } = useQuery({
  queryKey: ["eval-task", selectedTaskId],
  queryFn: () => fetchEvalTask(selectedTaskId.value!),
  enabled: computed(() => drawerOpen.value && !!selectedTaskId.value),
});

const { data: taskResults } = useQuery({
  queryKey: ["eval-task-results", selectedTaskId],
  queryFn: () => fetchEvalTaskResults(selectedTaskId.value!),
  enabled: computed(() => drawerOpen.value && !!selectedTaskId.value && drawerTab.value === "results"),
});

const queryClient = useQueryClient();

// ── New task form ──
const showNewForm = ref(false);
const newTask = ref({
  task_name: "",
  task_type: "precision_recall",
  description: "",
});
const creating = ref(false);

async function handleCreateTask() {
  if (!newTask.value.task_name.trim()) return;
  creating.value = true;
  try {
    await createEvalTask({
      task_name: newTask.value.task_name.trim(),
      task_type: newTask.value.task_type,
      description: newTask.value.description.trim() || undefined,
    });
    newTask.value = { task_name: "", task_type: "precision_recall", description: "" };
    showNewForm.value = false;
    queryClient.invalidateQueries({ queryKey: ["eval-tasks"] });
  } catch (e) {
    console.error("Failed to create eval task", e);
  } finally {
    creating.value = false;
  }
}

// ── Actions ──
const actionLoading = ref(false);

async function handleRunTask(taskId: string) {
  actionLoading.value = true;
  try {
    await runEvalTask(taskId);
    queryClient.invalidateQueries({ queryKey: ["eval-tasks"] });
    queryClient.invalidateQueries({ queryKey: ["eval-task", taskId] });
  } catch (e) {
    console.error("Failed to run eval task", e);
  } finally {
    actionLoading.value = false;
  }
}

async function handleCancelTask(taskId: string) {
  actionLoading.value = true;
  try {
    await cancelEvalTask(taskId);
    queryClient.invalidateQueries({ queryKey: ["eval-tasks"] });
    queryClient.invalidateQueries({ queryKey: ["eval-task", taskId] });
  } catch (e) {
    console.error("Failed to cancel eval task", e);
  } finally {
    actionLoading.value = false;
  }
}

// ── Table columns ──
const columns = [
  { key: "task_name", label: "任务名称", width: "200px" },
  { key: "task_type", label: "类型", width: "120px" },
  { key: "status", label: "状态", width: "100px" },
  { key: "progress", label: "进度", width: "120px" },
  { key: "total_items", label: "总项数", width: "80px" },
  { key: "created_at", label: "创建时间", width: "160px" },
];

// ── Helpers ──
function openDetail(task: EvalTask) {
  selectedTaskId.value = task.task_id;
  drawerTab.value = "info";
  drawerOpen.value = true;
}

function formatTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function taskTypeLabel(type: string): string {
  const map: Record<string, string> = {
    precision_recall: "精确率/召回率",
    bleu: "BLEU",
    rouge: "ROUGE",
    f1: "F1 分数",
    accuracy: "准确率",
    manual: "人工评估",
    custom: "自定义",
  };
  return map[type] || type;
}

const TASK_TYPE_STYLES: Record<string, string> = {
  precision_recall: "bg-indigo-50 text-indigo-700",
  bleu: "bg-violet-50 text-violet-700",
  rouge: "bg-pink-50 text-pink-700",
  f1: "bg-green-50 text-green-700",
  accuracy: "bg-cyan-50 text-cyan-700",
  manual: "bg-amber-50 text-amber-700",
  custom: "bg-surface-100 text-surface-600",
};

function formatProgress(task: EvalTask): string {
  if (task.total_items === 0) return "0%";
  return `${Math.round((task.processed_items / task.total_items) * 100)}%`;
}

function metricValueDisplay(value: number): string {
  if (value >= 0 && value <= 1) return `${(value * 100).toFixed(1)}%`;
  return value.toFixed(4);
}

// ── Metrics summary for overview ──
const overviewMetrics = computed(() => {
  if (!taskDetail.value?.metrics_summary?.length) return [];
  return taskDetail.value.metrics_summary;
});
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader
      title="评估中心"
      subtitle="管理评估任务、查看评估指标和结果 — 支持多种评估类型与自动化运行"
    />

    <!-- Create task toggle -->
    <div class="mb-6">
      <button
        v-if="!showNewForm"
        class="btn btn-primary btn-sm"
        @click="showNewForm = true"
      >
        <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
        </svg>
        新建评估任务
      </button>

      <!-- New task form -->
      <div v-else class="card p-4">
        <p class="text-sm font-semibold text-surface-700 mb-3">新建评估任务</p>
        <div class="space-y-3">
          <div class="grid grid-cols-2 gap-3">
            <div class="flex flex-col gap-1">
              <label class="text-2xs font-medium text-surface-400">任务名称</label>
              <input
                v-model="newTask.task_name"
                type="text"
                placeholder="例如: 记忆提取评估 v1"
                class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              />
            </div>
            <div class="flex flex-col gap-1">
              <label class="text-2xs font-medium text-surface-400">评估类型</label>
              <select
                v-model="newTask.task_type"
                class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              >
                <option value="precision_recall">精确率/召回率</option>
                <option value="bleu">BLEU</option>
                <option value="rouge">ROUGE</option>
                <option value="f1">F1 分数</option>
                <option value="accuracy">准确率</option>
                <option value="manual">人工评估</option>
                <option value="custom">自定义</option>
              </select>
            </div>
          </div>
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-medium text-surface-400">描述（可选）</label>
            <input
              v-model="newTask.description"
              type="text"
              placeholder="任务描述..."
              class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            />
          </div>
          <div class="flex items-center gap-2">
            <button
              class="btn btn-primary btn-sm"
              :disabled="creating || !newTask.task_name.trim()"
              @click="handleCreateTask"
            >
              {{ creating ? '创建中...' : '创建任务' }}
            </button>
            <button class="btn btn-ghost btn-sm" @click="showNewForm = false">取消</button>
          </div>
        </div>
      </div>
    </div>

    <!-- Filters -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-end gap-3">
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">状态</label>
          <select
            v-model="filterStatus"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部状态</option>
            <option value="pending">待执行</option>
            <option value="running">运行中</option>
            <option value="completed">已完成</option>
            <option value="failed">失败</option>
            <option value="cancelled">已取消</option>
          </select>
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">评估类型</label>
          <select
            v-model="filterTaskType"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部类型</option>
            <option value="precision_recall">精确率/召回率</option>
            <option value="bleu">BLEU</option>
            <option value="rouge">ROUGE</option>
            <option value="f1">F1 分数</option>
            <option value="accuracy">准确率</option>
            <option value="manual">人工评估</option>
            <option value="custom">自定义</option>
          </select>
        </div>

        <button
          class="btn btn-ghost btn-sm"
          @click="filterStatus = ''; filterTaskType = ''; page = 1"
        >
          清除
        </button>

        <span class="text-xs text-surface-400 ml-auto self-end pb-1">
          {{ data?.page_info?.total_items ?? 0 }} 个任务
        </span>
      </div>
    </div>

    <!-- Error -->
    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载评估任务失败: {{ (error as Error)?.message }}
    </div>

    <!-- Task Table -->
    <DataTable
      :items="data?.items ?? []"
      :columns="columns"
      :loading="isLoading"
      :empty-message="isError ? '数据加载出错' : '暂无评估任务 — 点击「新建评估任务」开始'"
      clickable
      row-key="task_id"
      @row-click="openDetail"
    >
      <!-- Task name -->
      <template #cell-task_name="{ value, item }">
        <div class="max-w-[200px]">
          <p class="truncate text-sm font-medium text-surface-800">{{ value }}</p>
          <p
            v-if="(item as EvalTask).description"
            class="text-2xs text-surface-400 truncate mt-0.5"
          >
            {{ (item as EvalTask).description }}
          </p>
        </div>
      </template>

      <!-- Task type -->
      <template #cell-task_type="{ value }">
        <span
          :class="[
            'badge text-2xs font-medium',
            TASK_TYPE_STYLES[value as string] || 'bg-surface-100 text-surface-600',
          ]"
        >
          {{ taskTypeLabel(value as string) }}
        </span>
      </template>

      <!-- Status -->
      <template #cell-status="{ value }">
        <StatusBadge :status="value as string" />
      </template>

      <!-- Progress -->
      <template #cell-progress="{ item }">
        <div class="flex items-center gap-2 min-w-[100px]">
          <div class="flex-1 h-1.5 bg-surface-200 rounded-full overflow-hidden">
            <div
              class="h-full rounded-full transition-all duration-300"
              :class="(item as EvalTask).status === 'completed' ? 'bg-emerald-500' : (item as EvalTask).status === 'failed' ? 'bg-red-500' : (item as EvalTask).status === 'running' ? 'bg-brand-500' : 'bg-surface-400'"
              :style="{ width: formatProgress(item as EvalTask) }"
            ></div>
          </div>
          <span class="text-2xs font-mono text-surface-500 shrink-0">
            {{ (item as EvalTask).processed_items }}/{{ (item as EvalTask).total_items }}
          </span>
        </div>
      </template>

      <!-- Total items -->
      <template #cell-total_items="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ value }}</span>
      </template>

      <!-- Created at -->
      <template #cell-created_at="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
      </template>
    </DataTable>

    <Pagination
      v-if="data?.page_info"
      :page-info="data.page_info"
      @page-change="(p: number) => (page = p)"
    />

    <!-- Detail Drawer -->
    <DetailDrawer
      :open="drawerOpen"
      title="评估任务详情"
      width="w-[580px] max-w-full"
      @close="drawerOpen = false; selectedTaskId = null"
    >
      <!-- Loading -->
      <div v-if="detailLoading" class="py-4">
        <LoadingSkeleton variant="detail" />
      </div>

      <template v-else-if="taskDetail">
        <!-- Tab bar -->
        <div class="flex items-center gap-1 border-b border-surface-200 mb-4 -mx-5 px-5">
          <button
            v-for="tab in [
              { key: 'info', label: '基本信息' },
              { key: 'metrics', label: '指标概览' },
              { key: 'results', label: `结果 (${taskDetail.processed_items})` },
            ]"
            :key="tab.key"
            @click="drawerTab = tab.key as 'info' | 'metrics' | 'results'"
            :class="[
              'pb-2.5 px-3 text-sm font-medium border-b-2 transition-colors',
              drawerTab === tab.key
                ? 'border-brand-500 text-brand-600'
                : 'border-transparent text-surface-400 hover:text-surface-600',
            ]"
          >
            {{ tab.label }}
          </button>
        </div>

        <!-- Tab: Info -->
        <div v-if="drawerTab === 'info'">
          <dl class="space-y-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">任务ID</dt>
              <dd class="mt-1 font-mono text-xs text-surface-700 break-all">{{ taskDetail.task_id }}</dd>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">任务名称</dt>
                <dd class="mt-1 text-sm font-medium text-surface-700">{{ taskDetail.task_name }}</dd>
              </div>
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">评估类型</dt>
                <dd class="mt-1">
                  <span
                    :class="[
                      'badge text-2xs font-medium',
                      TASK_TYPE_STYLES[taskDetail.task_type] || 'bg-surface-100 text-surface-600',
                    ]"
                  >
                    {{ taskTypeLabel(taskDetail.task_type) }}
                  </span>
                </dd>
              </div>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">状态</dt>
                <dd class="mt-1"><StatusBadge :status="taskDetail.status" /></dd>
              </div>
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">进度</dt>
                <dd class="mt-1">
                  <div class="flex-1 h-2 bg-surface-200 rounded-full overflow-hidden">
                    <div
                      class="h-full rounded-full"
                      :class="taskDetail.status === 'completed' ? 'bg-emerald-500' : taskDetail.status === 'failed' ? 'bg-red-500' : 'bg-brand-500'"
                      :style="{ width: formatProgress(taskDetail) }"
                    ></div>
                  </div>
                  <span class="text-xs font-mono text-surface-500">
                    {{ taskDetail.processed_items }}/{{ taskDetail.total_items }}
                  </span>
                </dd>
              </div>
            </div>

            <div v-if="taskDetail.description">
              <dt class="text-2xs font-semibold uppercase text-surface-400">描述</dt>
              <dd class="mt-1 text-sm text-surface-600 leading-relaxed">{{ taskDetail.description }}</dd>
            </div>

            <!-- Config -->
            <div v-if="taskDetail.config && Object.keys(taskDetail.config).length > 0">
              <dt class="text-2xs font-semibold uppercase text-surface-400 mb-2">配置参数</dt>
              <div class="rounded-lg border border-surface-200 divide-y divide-surface-100 overflow-hidden">
                <div
                  v-for="(val, key) in taskDetail.config"
                  :key="key"
                  class="flex items-start gap-3 px-3 py-2"
                >
                  <span class="text-xs font-medium text-surface-500 shrink-0">{{ key }}</span>
                  <span class="text-xs text-surface-700 font-mono break-all">{{ typeof val === 'object' ? JSON.stringify(val) : val }}</span>
                </div>
              </div>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt>
                <dd class="mt-1 text-xs">{{ formatTime(taskDetail.created_at) }}</dd>
              </div>
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">开始时间</dt>
                <dd class="mt-1 text-xs">{{ formatTime(taskDetail.started_at) }}</dd>
              </div>
            </div>

            <div v-if="taskDetail.finished_at" class="grid grid-cols-2 gap-4">
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">完成时间</dt>
                <dd class="mt-1 text-xs">{{ formatTime(taskDetail.finished_at) }}</dd>
              </div>
            </div>

            <div v-if="taskDetail.error_message">
              <dt class="text-2xs font-semibold uppercase text-surface-400">错误信息</dt>
              <dd class="mt-1 text-sm text-red-600 bg-red-50 rounded p-2 font-mono text-xs break-all">
                {{ taskDetail.error_message }}
              </dd>
            </div>
          </dl>

          <!-- Actions -->
          <div class="mt-6 pt-4 border-t border-surface-200 flex items-center gap-2">
            <button
              v-if="taskDetail.status === 'pending'"
              class="btn btn-primary btn-sm"
              :disabled="actionLoading"
              @click="handleRunTask(taskDetail.task_id)"
            >
              运行评估
            </button>
            <button
              v-if="taskDetail.status === 'running'"
              class="btn btn-danger btn-sm"
              :disabled="actionLoading"
              @click="handleCancelTask(taskDetail.task_id)"
            >
              取消运行
            </button>
          </div>
        </div>

        <!-- Tab: Metrics -->
        <div v-if="drawerTab === 'metrics'">
          <div v-if="overviewMetrics.length === 0" class="py-8 text-center text-sm text-surface-400">
            <div class="flex flex-col items-center gap-2">
              <span>暂无指标 — 请先运行评估任务</span>
            </div>
          </div>

          <!-- Metric cards -->
          <div v-else class="space-y-3">
            <div
              v-for="metric in overviewMetrics"
              :key="metric.metric_name + metric.aggregation"
              class="rounded-lg border border-surface-200 p-4 hover:border-surface-300 transition-colors"
            >
              <div class="flex items-center justify-between mb-3">
                <span class="text-sm font-semibold text-surface-800">{{ metric.metric_name }}</span>
                <span class="badge text-2xs bg-surface-100 text-surface-500">
                  {{ metric.aggregation }}
                </span>
              </div>
              <span class="text-lg font-bold text-surface-800 font-mono">
                {{ metricValueDisplay(metric.value) }}
              </span>

              <!-- Metric bar visualization -->
              <div class="h-2 bg-surface-100 rounded-full overflow-hidden mb-2">
                <div
                  class="h-full rounded-full bg-brand-500"
                  :style="{ width: `${Math.min(100, metric.value * (metric.value <= 1 ? 100 : 1))}%` }"
                ></div>
              </div>

              <div class="flex items-center gap-4 text-2xs text-surface-400 font-mono">
                <span>min: {{ metricValueDisplay(metric.min_value) }}</span>
                <span>max: {{ metricValueDisplay(metric.max_value) }}</span>
                <span>std: {{ metric.std_dev.toFixed(4) }}</span>
                <span class="ml-auto">n={{ metric.sample_count }}</span>
              </div>
            </div>
          </div>
        </div>

        <!-- Tab: Results -->
        <div v-if="drawerTab === 'results'">
          <div v-if="!taskResults || taskResults.items.length === 0" class="py-8 text-center text-sm text-surface-400">
            <div class="flex flex-col items-center gap-2">
              <span>暂无评估结果</span>
            </div>
          </div>

          <div v-else class="space-y-3">
            <div
              v-for="result in taskResults.items"
              :key="result.result_id"
              class="rounded-lg border border-surface-200 p-3 hover:border-surface-300 transition-colors"
            >
              <div class="flex items-center justify-between mb-2">
                <span class="text-2xs font-semibold uppercase text-surface-400">
                  #{{ result.item_index }}
                </span>
                <span class="font-mono text-2xs text-surface-400">
                  {{ result.result_id.slice(0, 8) }}…
                </span>
              </div>

              <!-- Input / Expected / Actual -->
              <div class="space-y-1.5 mb-2">
                <div v-if="result.input" class="text-xs">
                  <span class="text-2xs font-semibold text-surface-400 uppercase">输入:</span>
                  <span class="ml-1 text-surface-700 line-clamp-2">{{ result.input }}</span>
                </div>
                <div v-if="result.expected_output" class="text-xs">
                  <span class="text-2xs font-semibold text-emerald-600 uppercase">期望:</span>
                  <span class="ml-1 text-surface-600 line-clamp-2">{{ result.expected_output }}</span>
                </div>
                <div v-if="result.actual_output" class="text-xs">
                  <span class="text-2xs font-semibold text-brand-600 uppercase">实际:</span>
                  <span class="ml-1 text-surface-600 line-clamp-2">{{ result.actual_output }}</span>
                </div>
              </div>

              <!-- Metrics -->
              <div v-if="result.metrics && Object.keys(result.metrics).length > 0" class="flex flex-wrap gap-2">
                <span
                  v-for="(val, key) in result.metrics"
                  :key="key"
                  class="badge text-2xs font-mono bg-brand-50 text-brand-700"
                >
                  {{ key }}: {{ typeof val === 'number' ? (val >= 0 && val <= 1 ? (val * 100).toFixed(1) + '%' : val.toFixed(4)) : val }}
                </span>
              </div>
            </div>
          </div>
        </div>
      </template>

      <!-- Empty detail -->
      <div v-else class="flex flex-col items-center justify-center py-12 text-sm text-surface-400">
        加载评估详情失败
      </div>

      <!-- Footer -->
      <template #footer>
        <div class="flex items-center justify-between">
          <span class="text-2xs text-surface-400">
            ID: {{ selectedTaskId ? selectedTaskId.slice(0, 8) + '...' : '—' }}
          </span>
          <button class="btn btn-secondary btn-sm" @click="drawerOpen = false; selectedTaskId = null">
            关闭
          </button>
        </div>
      </template>
    </DetailDrawer>
  </div>
</template>
