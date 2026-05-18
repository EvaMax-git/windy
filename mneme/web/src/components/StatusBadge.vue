<script setup lang="ts">
import { computed } from "vue";
import { statusToColor } from "@/types";

const props = defineProps<{
  status: string;
  label?: string;
  pulse?: boolean;
}>();

const color = computed(() => statusToColor(props.status));

const colorClasses = computed(() => {
  const map: Record<string, string> = {
    green: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
    yellow: "bg-amber-50 text-amber-700 ring-amber-600/20",
    red: "bg-red-50 text-red-700 ring-red-600/20",
    gray: "bg-surface-100 text-surface-600 ring-surface-500/20",
  };
  return map[color.value] ?? map.gray;
});

const dotColor = computed(() => {
  const map: Record<string, string> = {
    green: "bg-emerald-500",
    yellow: "bg-amber-500",
    red: "bg-red-500",
    gray: "bg-surface-400",
  };
  return map[color.value] ?? map.gray;
});

// ── 中文状态标签映射 ──
const STATUS_LABELS: Record<string, string> = {
  // 任务状态
  pending: "待处理",
  scheduled: "已调度",
  running: "运行中",
  succeeded: "已成功",
  failed: "失败",
  retrying: "重试中",
  cancelled: "已取消",
  dead_letter: "死信",

  // 投递/事件状态
  dispatched: "已分发",
  delivered: "已投递",

  // 审核状态
  in_review: "审核中",
  approved: "已批准",
  rejected: "被驳回",
  expired: "已过期",

  // 重放状态
  under_review: "审核中",
  replayed: "已重放",
  resolved: "已解决",

  // 备份/恢复状态
  completed: "已完成",
  in_progress: "进行中",

  // 知识/入库/资产状态 (P3-11)
  staged: "已暂存",
  importing: "导入中",
  ready: "就绪",
  not_started: "未开始",
  stale: "已过期",
  quarantined: "已隔离",
  archived: "已归档",
  deleted: "已删除",

  // 故障类型
  provider_transient_exhausted: "Provider 瞬态耗尽",
  policy_denied_terminal: "策略终端拒绝",
  payload_invalid: "载荷无效",
  code_bug: "代码缺陷",
  external_side_effect_unknown: "外部副作用未知",

  // 通用
  success: "成功",
  ok: "正常",
  connected: "已连接",
  disconnected: "已断开",
  stopped: "已停止",
  standby: "待命",
  degraded: "性能降级",
  unavailable: "不可用",
  unknown: "未知",
  denied: "被拒绝",
  active: "活跃",
  disabled: "已禁用",
  locked: "已锁定",
};

const displayLabel = computed(() => {
  if (props.label) return props.label;
  return STATUS_LABELS[props.status] ?? props.status;
});
</script>

<template>
  <span
    :class="[
      'badge ring-1 ring-inset',
      colorClasses,
    ]"
  >
    <span
      :class="[
        'h-1.5 w-1.5 rounded-full',
        dotColor,
        pulse ? 'animate-pulse' : '',
      ]"
    ></span>
    {{ displayLabel }}
  </span>
</template>
