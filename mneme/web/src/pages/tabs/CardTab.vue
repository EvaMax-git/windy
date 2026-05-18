<script setup lang="ts">
/**
 * CardTab — Agent 卡牌管理
 *
 * 四类卡牌:
 *   🪪 身份卡 (identity)  — Agent 身份定义
 *   💫 灵魂卡 (soul)      — Agent 核心价值观/行为规则
 *   🔧 工具卡 (tool)      — Agent 能力工具 (目录层+详情层)
 *   👤 用户画像 (user_profile) — 用户偏好/画像
 *
 * 工具卡二层:
 *   目录层: 工具卡本身 (分类/分组)
 *   详情层: 工具卡下的具体工具项 (tool items)
 */
import { ref, computed } from "vue";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import {
  fetchAgentCards,
  fetchAgentCard,
  createAgentCard,
  updateAgentCard,
  deleteAgentCard,
  fetchAgentToolItems,
  createAgentToolItem,
  updateAgentToolItem,
  deleteAgentToolItem,
} from "@/api/client";
import type { AgentCardRead, AgentToolItemRead, AgentCardType } from "@/types";
import { CARD_TYPE_LABELS, TOOL_TYPE_LABELS } from "@/types";

const queryClient = useQueryClient();

// ── Type filter ──
const filterType = ref<string>("");

// ── List ──
const page = ref(1);
const pageSize = ref(100);

const listKey = computed(() => ["agent-cards", page.value, pageSize.value, filterType.value] as const);

const { data, isLoading, isError, error } = useQuery({
  queryKey: listKey,
  queryFn: () => fetchAgentCards({
    page: page.value,
    page_size: pageSize.value,
    card_type: filterType.value || undefined,
  }),
  placeholderData: (prev) => prev,
});

// ── Card form (create/edit) ──
const showCardForm = ref(false);
const editingCardId = ref<string | null>(null);
const cardForm = ref({
  card_type: "identity" as AgentCardType,
  name: "",
  description: "",
  agent_id: null as string | null,
  content_json_str: "{}",
  display_order: 0,
});
const cardFormSubmitting = ref(false);

const CARD_TYPE_OPTIONS: { value: AgentCardType; label: string }[] = [
  { value: "identity", label: "🪪 身份卡" },
  { value: "soul", label: "💫 灵魂卡" },
  { value: "tool", label: "🔧 工具卡" },
  { value: "user_profile", label: "👤 用户画像" },
];

// ── Tool Items (detail layer for tool cards) ──
const selectedToolCardId = ref<string | null>(null);
const showToolItems = ref(false);
const selectedToolCard = computed(() => {
  const d = data.value;
  if (!selectedToolCardId.value || !d?.items) return null;
  return d.items.find(c => c.card_id === selectedToolCardId.value) ?? null;
});

const { data: toolItems } = useQuery({
  queryKey: ["agent-tool-items", selectedToolCardId],
  queryFn: () => fetchAgentToolItems(selectedToolCardId.value!),
  enabled: computed(() => showToolItems.value && !!selectedToolCardId.value),
});

// ── Tool item form ──
const showToolItemForm = ref(false);
const editingToolItemId = ref<string | null>(null);
const toolItemForm = ref({
  name: "",
  description: "",
  tool_type: "function",
  config_json_str: "{}",
  input_schema_str: "",
  output_schema_str: "",
  display_order: 0,
});
const toolItemFormSubmitting = ref(false);

const TOOL_TYPE_OPTIONS = [
  { value: "api", label: "API 接口" },
  { value: "function", label: "函数调用" },
  { value: "script", label: "脚本执行" },
  { value: "builtin", label: "内置能力" },
  { value: "mcp", label: "MCP 协议" },
];

// ── Delete confirm ──
const confirmDeleteCardId = ref<string | null>(null);
const confirmDeleteItemId = ref<string | null>(null);
const deleting = ref(false);

// ── Helpers ──
function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function openCreateCard(type?: AgentCardType) {
  editingCardId.value = null;
  cardForm.value = {
    card_type: type || "identity",
    name: "",
    description: "",
    agent_id: null,
    content_json_str: "{}",
    display_order: 0,
  };
  showCardForm.value = true;
}

function openEditCard(card: AgentCardRead) {
  editingCardId.value = card.card_id;
  cardForm.value = {
    card_type: card.card_type,
    name: card.name,
    description: card.description || "",
    agent_id: card.agent_id,
    content_json_str: JSON.stringify(card.content_json || {}, null, 2),
    display_order: card.display_order,
  };
  showCardForm.value = true;
}

function closeCardForm() {
  showCardForm.value = false;
  editingCardId.value = null;
}

async function handleCardSubmit() {
  if (!cardForm.value.name.trim()) return;
  cardFormSubmitting.value = true;

  let contentJson: Record<string, unknown>;
  try {
    contentJson = JSON.parse(cardForm.value.content_json_str || "{}");
  } catch {
    alert("Content JSON 格式无效");
    cardFormSubmitting.value = false;
    return;
  }

  try {
    if (editingCardId.value) {
      await updateAgentCard(editingCardId.value, {
        name: cardForm.value.name.trim(),
        description: cardForm.value.description.trim() || null,
        agent_id: cardForm.value.agent_id || null,
        content_json: contentJson,
        display_order: cardForm.value.display_order,
      });
    } else {
      await createAgentCard({
        card_type: cardForm.value.card_type,
        name: cardForm.value.name.trim(),
        description: cardForm.value.description.trim() || null,
        agent_id: cardForm.value.agent_id || null,
        content_json: contentJson,
        display_order: cardForm.value.display_order,
      });
    }
    closeCardForm();
    queryClient.invalidateQueries({ queryKey: ["agent-cards"] });
  } catch (e) {
    console.error("Failed to save card", e);
  } finally {
    cardFormSubmitting.value = false;
  }
}

async function handleDeleteCard() {
  if (!confirmDeleteCardId.value) return;
  deleting.value = true;
  try {
    await deleteAgentCard(confirmDeleteCardId.value);
    confirmDeleteCardId.value = null;
    queryClient.invalidateQueries({ queryKey: ["agent-cards"] });
  } catch (e) {
    console.error("Failed to delete card", e);
  } finally {
    deleting.value = false;
  }
}

// ── Tool item: open detail layer ──
function openToolItems(cardId: string) {
  selectedToolCardId.value = cardId;
  showToolItems.value = true;
}

function backToCards() {
  showToolItems.value = false;
  selectedToolCardId.value = null;
  showToolItemForm.value = false;
}

function openCreateToolItem() {
  editingToolItemId.value = null;
  toolItemForm.value = {
    name: "",
    description: "",
    tool_type: "function",
    config_json_str: "{}",
    input_schema_str: "",
    output_schema_str: "",
    display_order: 0,
  };
  showToolItemForm.value = true;
}

function openEditToolItem(item: AgentToolItemRead) {
  editingToolItemId.value = item.item_id;
  toolItemForm.value = {
    name: item.name,
    description: item.description || "",
    tool_type: item.tool_type || "function",
    config_json_str: JSON.stringify(item.config_json || {}, null, 2),
    input_schema_str: item.input_schema ? JSON.stringify(item.input_schema, null, 2) : "",
    output_schema_str: item.output_schema ? JSON.stringify(item.output_schema, null, 2) : "",
    display_order: item.display_order,
  };
  showToolItemForm.value = true;
}

function closeToolItemForm() {
  showToolItemForm.value = false;
  editingToolItemId.value = null;
}

async function handleToolItemSubmit() {
  if (!toolItemForm.value.name.trim() || !selectedToolCardId.value) return;
  toolItemFormSubmitting.value = true;

  let configJson: Record<string, unknown>;
  let inputSchema: Record<string, unknown> | null = null;
  let outputSchema: Record<string, unknown> | null = null;

  try {
    configJson = JSON.parse(toolItemForm.value.config_json_str || "{}");
  } catch {
    alert("Config JSON 格式无效");
    toolItemFormSubmitting.value = false;
    return;
  }

  if (toolItemForm.value.input_schema_str.trim()) {
    try {
      inputSchema = JSON.parse(toolItemForm.value.input_schema_str);
    } catch {
      alert("Input Schema JSON 格式无效");
      toolItemFormSubmitting.value = false;
      return;
    }
  }
  if (toolItemForm.value.output_schema_str.trim()) {
    try {
      outputSchema = JSON.parse(toolItemForm.value.output_schema_str);
    } catch {
      alert("Output Schema JSON 格式无效");
      toolItemFormSubmitting.value = false;
      return;
    }
  }

  try {
    if (editingToolItemId.value) {
      await updateAgentToolItem(selectedToolCardId.value, editingToolItemId.value, {
        name: toolItemForm.value.name.trim(),
        description: toolItemForm.value.description.trim() || null,
        tool_type: toolItemForm.value.tool_type || null,
        config_json: configJson,
        input_schema: inputSchema,
        output_schema: outputSchema,
        display_order: toolItemForm.value.display_order,
      });
    } else {
      await createAgentToolItem(selectedToolCardId.value, {
        card_id: selectedToolCardId.value,
        name: toolItemForm.value.name.trim(),
        description: toolItemForm.value.description.trim() || null,
        tool_type: toolItemForm.value.tool_type || null,
        config_json: configJson,
        input_schema: inputSchema,
        output_schema: outputSchema,
        display_order: toolItemForm.value.display_order,
      });
    }
    closeToolItemForm();
    queryClient.invalidateQueries({ queryKey: ["agent-tool-items", selectedToolCardId.value] });
    queryClient.invalidateQueries({ queryKey: ["agent-cards"] });
  } catch (e) {
    console.error("Failed to save tool item", e);
  } finally {
    toolItemFormSubmitting.value = false;
  }
}

async function handleDeleteToolItem() {
  if (!confirmDeleteItemId.value || !selectedToolCardId.value) return;
  deleting.value = true;
  try {
    await deleteAgentToolItem(selectedToolCardId.value, confirmDeleteItemId.value);
    confirmDeleteItemId.value = null;
    queryClient.invalidateQueries({ queryKey: ["agent-tool-items", selectedToolCardId.value] });
    queryClient.invalidateQueries({ queryKey: ["agent-cards"] });
  } catch (e) {
    console.error("Failed to delete tool item", e);
  } finally {
    deleting.value = false;
  }
}
</script>

<template>
  <div>
    <!-- ── Toolbar ── -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 class="text-sm font-semibold text-surface-700">卡牌管理</h3>
          <p class="text-xs text-surface-400 mt-0.5">
            管理 Agent 身份卡、灵魂卡、工具卡与用户画像
          </p>
        </div>
        <div class="flex items-center gap-3">
          <!-- Filter by type -->
          <select
            v-model="filterType"
            class="rounded-lg border border-surface-200 bg-white px-2.5 py-1.5 text-xs text-surface-600 focus:border-brand-500 focus:outline-none"
          >
            <option value="">全部类型</option>
            <option value="identity">🪪 身份卡</option>
            <option value="soul">💫 灵魂卡</option>
            <option value="tool">🔧 工具卡</option>
            <option value="user_profile">👤 用户画像</option>
          </select>

          <span class="text-xs text-surface-400">
            {{ data?.page_info?.total_items ?? 0 }} 张卡牌
          </span>

          <!-- Quick create per type -->
          <div class="flex items-center gap-1">
            <button
              v-for="opt in CARD_TYPE_OPTIONS"
              :key="opt.value"
              class="btn btn-secondary btn-xs text-2xs"
              @click="openCreateCard(opt.value)"
            >
              + {{ opt.label }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- ── Error ── -->
    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载卡牌失败: {{ (error as Error)?.message }}
    </div>

    <!-- ── Loading ── -->
    <div v-if="isLoading" class="py-8">
      <LoadingSkeleton variant="detail" />
    </div>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- Card List View (Directory Layer for all card types) -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <div v-else-if="data?.items?.length" class="space-y-3">
      <div
        v-for="card in data.items"
        :key="card.card_id"
        class="card p-4 hover:shadow-sm transition-shadow group"
      >
        <div class="flex items-start gap-4">
          <!-- Type icon -->
          <span class="shrink-0 text-2xl">
            {{ card.card_type === 'identity' ? '🪪' : card.card_type === 'soul' ? '💫' : card.card_type === 'tool' ? '🔧' : '👤' }}
          </span>

          <!-- Content -->
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <p class="text-sm font-medium text-surface-800">{{ card.name }}</p>
              <span class="text-2xs font-mono text-surface-400 bg-surface-100 rounded px-1.5 py-0.5">
                {{ CARD_TYPE_LABELS[card.card_type] }}
              </span>
              <span
                :class="[
                  'badge text-2xs',
                  card.status === 'active' ? 'bg-green-50 text-green-600' :
                  card.status === 'disabled' ? 'bg-amber-50 text-amber-600' : 'bg-surface-100 text-surface-400'
                ]"
              >
                {{ card.status === 'active' ? '活跃' : card.status === 'disabled' ? '已禁用' : '已归档' }}
              </span>
            </div>

            <p v-if="card.description" class="text-xs text-surface-500 mt-1 line-clamp-2">
              {{ card.description }}
            </p>

            <!-- Content preview -->
            <div v-if="Object.keys(card.content_json || {}).length > 0" class="mt-1.5">
              <span
                v-for="(val, key) in (card.content_json as Record<string, unknown>)"
                :key="key"
                class="text-2xs rounded px-1.5 py-0.5 bg-indigo-50 text-indigo-600 mr-1"
              >
                {{ key }}
              </span>
            </div>

            <!-- Tool count badge (only for tool cards) -->
            <div v-if="card.card_type === 'tool'" class="flex items-center gap-3 mt-2">
              <span class="text-2xs text-surface-400">
                🔧 {{ card.tool_count ?? 0 }} 个工具
              </span>
              <button
                class="text-2xs text-brand-600 hover:text-brand-700 font-medium"
                @click="openToolItems(card.card_id)"
              >
                查看工具 →
              </button>
            </div>

            <!-- Meta -->
            <div class="flex items-center gap-2 mt-1.5 text-2xs text-surface-400">
              <span v-if="card.agent_id" class="font-mono">绑定: {{ card.agent_id.slice(0, 8) }}…</span>
              <span>创建: {{ formatTime(card.created_at)?.slice(0, 10) }}</span>
            </div>
          </div>

          <!-- Actions -->
          <div class="shrink-0 flex items-center gap-1">
            <button
              class="btn btn-ghost btn-xs text-xs"
              @click="openEditCard(card)"
            >
              编辑
            </button>
            <button
              class="btn btn-ghost btn-xs text-xs text-red-500"
              @click="confirmDeleteCardId = card.card_id"
            >
              删除
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- ── Empty ── -->
    <div
      v-else-if="!isLoading"
      class="flex flex-col items-center justify-center py-16 text-sm text-surface-400"
    >
      <span class="text-5xl mb-3">🃏</span>
      <span>暂无卡牌</span>
      <span class="text-2xs text-surface-300 mt-1">点击上方按钮创建第一张卡牌</span>
    </div>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- Card Create/Edit Modal -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <Teleport to="body">
      <Transition name="modal">
        <div
          v-if="showCardForm"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4"
          @click.self="closeCardForm"
        >
          <div
            class="bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto"
            @click.stop
          >
            <div class="px-6 py-4 border-b border-surface-200 flex items-center justify-between">
              <h3 class="text-lg font-semibold text-surface-800">
                {{ editingCardId ? "编辑卡牌" : "新建卡牌" }}
              </h3>
              <button
                class="rounded-lg p-1.5 text-surface-400 hover:bg-surface-100 hover:text-surface-600"
                @click="closeCardForm"
              >
                <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
              </button>
            </div>

            <div class="px-6 py-4 space-y-4">
              <!-- Card Type -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">卡牌类型</label>
                <select
                  v-model="cardForm.card_type"
                  :disabled="!!editingCardId"
                  class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:opacity-50"
                >
                  <option v-for="opt in CARD_TYPE_OPTIONS" :key="opt.value" :value="opt.value">
                    {{ opt.label }}
                  </option>
                </select>
              </div>

              <!-- Name -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">
                  名称 <span class="text-red-400">*</span>
                </label>
                <input
                  v-model="cardForm.name"
                  type="text"
                  placeholder="例如: 客服Agent身份定义"
                  class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                />
              </div>

              <!-- Description -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">描述</label>
                <textarea
                  v-model="cardForm.description"
                  rows="2"
                  placeholder="卡牌用途说明..."
                  class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                />
              </div>

              <!-- Agent binding -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">绑定 Agent</label>
                <input
                  v-model="cardForm.agent_id"
                  type="text"
                  placeholder="Agent UUID (可选)"
                  class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm font-mono text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                />
              </div>

              <!-- Content JSON -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">
                  Content JSON <span class="text-surface-300">(卡牌内容)</span>
                </label>
                <textarea
                  v-model="cardForm.content_json_str"
                  rows="6"
                  class="rounded-lg border border-surface-200 bg-white px-3 py-2 font-mono text-xs text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                  placeholder='{"key": "value"}'
                />
              </div>

              <!-- Display Order -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">排序</label>
                <input
                  v-model.number="cardForm.display_order"
                  type="number"
                  min="0"
                  class="w-24 rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                />
              </div>
            </div>

            <div class="px-6 py-4 border-t border-surface-200 bg-surface-50 rounded-b-2xl flex items-center justify-end gap-2">
              <button class="btn btn-secondary btn-sm" @click="closeCardForm">取消</button>
              <button
                class="btn btn-primary btn-sm"
                :disabled="cardFormSubmitting || !cardForm.name.trim()"
                @click="handleCardSubmit"
              >
                {{ cardFormSubmitting ? "保存中..." : editingCardId ? "保存修改" : "创建卡牌" }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- Tool Items Detail Layer -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <template v-if="showToolItems && selectedToolCard">
      <!-- Back navigation -->
      <div class="mb-4">
        <button
          @click="backToCards"
          class="flex items-center gap-1.5 text-sm text-brand-600 hover:text-brand-700 transition-colors"
        >
          <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M15 19l-7-7 7-7"/></svg>
          返回卡牌列表
        </button>
      </div>

      <!-- Tool Card Info Header -->
      <div class="card mb-6 p-4 bg-gradient-to-r from-indigo-50 to-purple-50 border-indigo-100">
        <div class="flex items-start gap-3">
          <span class="text-3xl">🔧</span>
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <h3 class="text-base font-semibold text-surface-800">{{ selectedToolCard.name }}</h3>
              <span class="badge text-2xs bg-indigo-100 text-indigo-700">工具卡</span>
            </div>
            <p v-if="selectedToolCard.description" class="text-xs text-surface-500 mt-0.5">
              {{ selectedToolCard.description }}
            </p>
            <div class="flex items-center gap-3 mt-1.5 text-2xs text-surface-400">
              <span>{{ toolItems?.length ?? 0 }} 个工具项</span>
              <span>创建: {{ formatTime(selectedToolCard.created_at)?.slice(0, 10) }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Tool items toolbar -->
      <div class="flex items-center justify-between mb-4">
        <h4 class="text-sm font-semibold text-surface-700">
          工具列表 ({{ toolItems?.length ?? 0 }})
        </h4>
        <button class="btn btn-primary btn-sm" @click="openCreateToolItem">
          + 添加工具
        </button>
      </div>

      <!-- Tool items list -->
      <div v-if="toolItems?.length" class="space-y-3">
        <div
          v-for="item in toolItems"
          :key="item.item_id"
          class="card p-4 hover:shadow-sm transition-shadow border-l-4"
          :class="item.tool_type === 'api' ? 'border-l-blue-400' :
                  item.tool_type === 'function' ? 'border-l-green-400' :
                  item.tool_type === 'script' ? 'border-l-amber-400' :
                  item.tool_type === 'mcp' ? 'border-l-purple-400' :
                  'border-l-indigo-400'"
        >
          <div class="flex items-start gap-3">
            <!-- Tool type icon -->
            <span class="shrink-0 text-lg">
              {{ item.tool_type === 'api' ? '🌐' : item.tool_type === 'function' ? '⚡' : item.tool_type === 'script' ? '📜' : item.tool_type === 'mcp' ? '🔌' : '🔧' }}
            </span>

            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2">
                <p class="text-sm font-medium text-surface-800">{{ item.name }}</p>
                <span class="text-2xs font-mono rounded px-1.5 py-0.5"
                  :class="item.tool_type === 'api' ? 'bg-blue-50 text-blue-600' :
                          item.tool_type === 'function' ? 'bg-green-50 text-green-600' :
                          item.tool_type === 'script' ? 'bg-amber-50 text-amber-600' :
                          item.tool_type === 'mcp' ? 'bg-purple-50 text-purple-600' :
                          'bg-indigo-50 text-indigo-600'"
                >
                  {{ TOOL_TYPE_LABELS[item.tool_type || ''] || item.tool_type || '内置' }}
                </span>
                <span
                  :class="[
                    'badge text-2xs',
                    item.status === 'active' ? 'bg-green-50 text-green-600' : 'bg-surface-100 text-surface-400'
                  ]"
                >
                  {{ item.status === 'active' ? '活跃' : item.status === 'disabled' ? '禁用' : '归档' }}
                </span>
              </div>

              <p v-if="item.description" class="text-xs text-surface-500 mt-1">
                {{ item.description }}
              </p>

              <!-- Schema badges -->
              <div class="flex items-center gap-2 mt-2">
                <span v-if="item.input_schema" class="text-2xs rounded px-1.5 py-0.5 bg-cyan-50 text-cyan-600">
                  📥 输入 Schema
                </span>
                <span v-if="item.output_schema" class="text-2xs rounded px-1.5 py-0.5 bg-teal-50 text-teal-600">
                  📤 输出 Schema
                </span>
              </div>

              <div class="flex items-center gap-2 mt-1.5 text-2xs text-surface-400">
                <span>创建: {{ formatTime(item.created_at)?.slice(0, 10) }}</span>
              </div>
            </div>

            <!-- Actions -->
            <div class="shrink-0 flex items-center gap-1">
              <button
                class="btn btn-ghost btn-xs text-xs"
                @click="openEditToolItem(item)"
              >
                编辑
              </button>
              <button
                class="btn btn-ghost btn-xs text-xs text-red-500"
                @click="confirmDeleteItemId = item.item_id"
              >
                删除
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- Empty tool items -->
      <div
        v-else
        class="flex flex-col items-center justify-center py-12 text-sm text-surface-400"
      >
        <span class="text-4xl mb-2">🔧</span>
        <span>暂无工具项</span>
        <span class="text-2xs text-surface-300 mt-1">点击"添加工具"添加第一个工具</span>
      </div>
    </template>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- Tool Item Create/Edit Modal -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <Teleport to="body">
      <Transition name="modal">
        <div
          v-if="showToolItemForm"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4"
          @click.self="closeToolItemForm"
        >
          <div
            class="bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto"
            @click.stop
          >
            <div class="px-6 py-4 border-b border-surface-200 flex items-center justify-between">
              <h3 class="text-lg font-semibold text-surface-800">
                {{ editingToolItemId ? "编辑工具" : "添加工具" }}
              </h3>
              <button
                class="rounded-lg p-1.5 text-surface-400 hover:bg-surface-100 hover:text-surface-600"
                @click="closeToolItemForm"
              >
                <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
              </button>
            </div>

            <div class="px-6 py-4 space-y-4">
              <!-- Name -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">
                  名称 <span class="text-red-400">*</span>
                </label>
                <input
                  v-model="toolItemForm.name"
                  type="text"
                  placeholder="例如: 总结文档"
                  class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                />
              </div>

              <!-- Description -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">描述</label>
                <textarea
                  v-model="toolItemForm.description"
                  rows="2"
                  placeholder="工具功能描述..."
                  class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                />
              </div>

              <!-- Tool Type -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">工具类型</label>
                <select
                  v-model="toolItemForm.tool_type"
                  class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                >
                  <option v-for="opt in TOOL_TYPE_OPTIONS" :key="opt.value" :value="opt.value">
                    {{ opt.label }}
                  </option>
                </select>
              </div>

              <!-- Config JSON -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">Config JSON</label>
                <textarea
                  v-model="toolItemForm.config_json_str"
                  rows="4"
                  class="rounded-lg border border-surface-200 bg-white px-3 py-2 font-mono text-xs text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                  placeholder='{"endpoint": "https://..."}'
                />
              </div>

              <!-- Input Schema -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">
                  Input Schema <span class="text-surface-300">(JSON Schema, 可选)</span>
                </label>
                <textarea
                  v-model="toolItemForm.input_schema_str"
                  rows="4"
                  class="rounded-lg border border-surface-200 bg-white px-3 py-2 font-mono text-xs text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                  placeholder='{"type": "object", "properties": {...}}'
                />
              </div>

              <!-- Output Schema -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">
                  Output Schema <span class="text-surface-300">(JSON Schema, 可选)</span>
                </label>
                <textarea
                  v-model="toolItemForm.output_schema_str"
                  rows="4"
                  class="rounded-lg border border-surface-200 bg-white px-3 py-2 font-mono text-xs text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                  placeholder='{"type": "object", "properties": {...}}'
                />
              </div>

              <!-- Display Order -->
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">排序</label>
                <input
                  v-model.number="toolItemForm.display_order"
                  type="number"
                  min="0"
                  class="w-24 rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                />
              </div>
            </div>

            <div class="px-6 py-4 border-t border-surface-200 bg-surface-50 rounded-b-2xl flex items-center justify-end gap-2">
              <button class="btn btn-secondary btn-sm" @click="closeToolItemForm">取消</button>
              <button
                class="btn btn-primary btn-sm"
                :disabled="toolItemFormSubmitting || !toolItemForm.name.trim()"
                @click="handleToolItemSubmit"
              >
                {{ toolItemFormSubmitting ? "保存中..." : editingToolItemId ? "保存修改" : "添加工具" }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- Delete Card Confirmation Modal -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <div
      v-if="confirmDeleteCardId"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      @click.self="confirmDeleteCardId = null"
    >
      <div class="card bg-white rounded-xl p-6 shadow-xl max-w-sm w-full mx-4">
        <div class="flex items-center gap-3 mb-3">
          <span class="shrink-0 flex h-10 w-10 items-center justify-center rounded-full bg-red-100 text-red-600">
            <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z"/></svg>
          </span>
          <div>
            <p class="text-sm font-medium text-surface-700">确认删除卡牌？</p>
            <p class="text-xs text-surface-500 mt-0.5 font-mono">{{ confirmDeleteCardId.slice(0, 12) }}…</p>
          </div>
        </div>
        <p class="text-xs text-surface-500 mb-4">
          删除后将归档，关联的工具项也会被保留但不可用。
        </p>
        <div class="flex justify-end gap-2">
          <button class="btn btn-secondary btn-sm" @click="confirmDeleteCardId = null">取消</button>
          <button
            class="btn btn-danger btn-sm"
            :disabled="deleting"
            @click="handleDeleteCard"
          >
            {{ deleting ? "删除中..." : "确认删除" }}
          </button>
        </div>
      </div>
    </div>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- Delete Tool Item Confirmation Modal -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <div
      v-if="confirmDeleteItemId"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      @click.self="confirmDeleteItemId = null"
    >
      <div class="card bg-white rounded-xl p-6 shadow-xl max-w-sm w-full mx-4">
        <div class="flex items-center gap-3 mb-3">
          <span class="shrink-0 flex h-10 w-10 items-center justify-center rounded-full bg-red-100 text-red-600">
            <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z"/></svg>
          </span>
          <div>
            <p class="text-sm font-medium text-surface-700">确认删除工具？</p>
            <p class="text-xs text-surface-500 mt-0.5 font-mono">{{ confirmDeleteItemId.slice(0, 12) }}…</p>
          </div>
        </div>
        <p class="text-xs text-surface-500 mb-4">删除后不可恢复。</p>
        <div class="flex justify-end gap-2">
          <button class="btn btn-secondary btn-sm" @click="confirmDeleteItemId = null">取消</button>
          <button
            class="btn btn-danger btn-sm"
            :disabled="deleting"
            @click="handleDeleteToolItem"
          >
            {{ deleting ? "删除中..." : "确认删除" }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-enter-active,
.modal-leave-active {
  transition: opacity 0.2s ease;
}
.modal-enter-active > div,
.modal-leave-active > div {
  transition: transform 0.2s ease, opacity 0.2s ease;
}
.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}
.modal-enter-from > div {
  transform: scale(0.95) translateY(10px);
  opacity: 0;
}
.modal-leave-to > div {
  transform: scale(0.95) translateY(10px);
  opacity: 0;
}

.btn-danger {
  background-color: #ef4444;
  color: white;
}
.btn-danger:hover {
  background-color: #dc2626;
}
.btn-ghost {
  background: transparent;
  color: inherit;
}
.btn-ghost:hover {
  background: rgba(0, 0, 0, 0.04);
}
.btn-xs {
  padding: 0.25rem 0.5rem;
  font-size: 0.75rem;
  line-height: 1rem;
  border-radius: 0.375rem;
  border: 1px solid transparent;
  cursor: pointer;
  transition: all 0.15s;
}
.btn-xs:hover {
  border-color: #e5e7eb;
}
</style>
