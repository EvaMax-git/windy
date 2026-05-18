<script setup lang="ts">
/**
 * ContextAssemblyTab — 上下文组装触发 UI
 *
 * 对接 POST /api/v4/context/assemble 端点
 */
import { ref, computed } from "vue";
import { useQuery } from "@tanstack/vue-query";
import { fetchAgents, assembleContext } from "@/api/client";
import type { AgentRead, AssembleRequest, AssembleResponse, InjectionStrategy } from "@/types";

const { data: agentsData } = useQuery({
  queryKey: ["agents-dropdown"],
  queryFn: () => fetchAgents({ page: 1, page_size: 200 }),
});
const agentList = computed<AgentRead[]>(() => agentsData.value?.items ?? []);

const selectedAgentId = ref("");
const queryText = ref("");
const maxTokens = ref<number | null>(4096);
const projectId = ref("");
const conversationHistory = ref("");
const expandCards = ref<string[]>([]);

const soulStrategy = ref<InjectionStrategy | "">("");
const identityStrategy = ref<InjectionStrategy | "">("");
const toolCatalogStrategy = ref<InjectionStrategy | "">("");
const userProfileStrategy = ref<InjectionStrategy | "">("");
const toolDetailStrategy = ref<InjectionStrategy | "">("");

const submitting = ref(false);
const lastResult = ref<AssembleResponse | null>(null);
const submitError = ref<string | null>(null);
const resultViewTab = ref<"text" | "sections" | "budget">("text");

const computedStrategyOverrides = computed<Record<string, InjectionStrategy> | null>(() => {
  const o: Record<string, InjectionStrategy> = {};
  if (soulStrategy.value) o.soul_card = soulStrategy.value;
  if (identityStrategy.value) o.identity_card = identityStrategy.value;
  if (toolCatalogStrategy.value) o.tool_catalog = toolCatalogStrategy.value;
  if (userProfileStrategy.value) o.user_profile = userProfileStrategy.value;
  if (toolDetailStrategy.value) o.tool_detail = toolDetailStrategy.value;
  return Object.keys(o).length > 0 ? o : null;
});

const CARD_TYPE_OPTIONS = [
  { key: "soul_card", label: "💫 灵魂卡" },
  { key: "identity_card", label: "🪪 身份卡" },
  { key: "tool_catalog", label: "🔧 工具目录" },
  { key: "user_profile", label: "👤 用户画像" },
  { key: "tool_detail", label: "🔍 工具详情" },
];

const STRATEGY_OPTIONS: { value: InjectionStrategy | ""; label: string }[] = [
  { value: "", label: "默认" },
  { value: "always", label: "始终注入" },
  { value: "moderate", label: "适量注入" },
  { value: "on_demand", label: "按需展开" },
];

async function handleAssemble() {
  if (!selectedAgentId.value || !queryText.value.trim()) return;
  submitting.value = true;
  submitError.value = null;
  lastResult.value = null;
  try {
    const payload: AssembleRequest = {
      agent_id: selectedAgentId.value,
      query_text: queryText.value.trim(),
      project_id: projectId.value || null,
      conversation_history: conversationHistory.value.trim() || null,
      max_tokens: maxTokens.value,
      strategy_overrides: computedStrategyOverrides.value,
      expand_cards: expandCards.value.length > 0 ? expandCards.value : null,
    };
    lastResult.value = await assembleContext(payload);
  } catch (e) {
    submitError.value = (e as Error)?.message || "上下文组装失败";
  } finally {
    submitting.value = false;
  }
}

function formatTokens(n: number): string { return n.toLocaleString(); }
function getStrategyLabel(s: InjectionStrategy): string {
  switch (s) { case "always": return "始终注入"; case "moderate": return "适量注入"; case "on_demand": return "按需展开"; default: return s; }
}
function getStrategyColor(s: InjectionStrategy): string {
  switch (s) { case "always": return "bg-violet-100 text-violet-700"; case "moderate": return "bg-blue-100 text-blue-700"; case "on_demand": return "bg-amber-100 text-amber-700"; default: return "bg-surface-100 text-surface-500"; }
}
function clearForm() {
  selectedAgentId.value = ""; queryText.value = ""; maxTokens.value = 4096; projectId.value = "";
  conversationHistory.value = ""; expandCards.value = [];
  soulStrategy.value = ""; identityStrategy.value = ""; toolCatalogStrategy.value = "";
  userProfileStrategy.value = ""; toolDetailStrategy.value = "";
  submitError.value = null; lastResult.value = null;
}
</script>

<template>
  <div>
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 class="text-sm font-semibold text-surface-700">上下文组装</h3>
          <p class="text-xs text-surface-400 mt-0.5">为 Agent 查询组装上下文 — 使用卡牌注入策略 + Token 预算管理</p>
        </div>
        <button v-if="lastResult" class="btn btn-secondary btn-sm" @click="clearForm">重新组装</button>
      </div>
    </div>

    <div class="card mb-6 p-6">
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div class="space-y-4">
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-semibold uppercase text-surface-400">Agent <span class="text-red-400">*</span></label>
            <select v-model="selectedAgentId" :disabled="!!lastResult" class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:opacity-50">
              <option value="">— 选择 Agent —</option>
              <option v-for="a in agentList" :key="a.agent_id" :value="a.agent_id">{{ a.name }} ({{ a.agent_id.slice(0, 8) }}…)</option>
            </select>
          </div>
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-semibold uppercase text-surface-400">查询文本 <span class="text-red-400">*</span></label>
            <textarea v-model="queryText" :disabled="!!lastResult" rows="3" placeholder="输入用户查询或任务描述..." class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:opacity-50" />
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div class="flex flex-col gap-1">
              <label class="text-2xs font-semibold uppercase text-surface-400">Max Tokens</label>
              <input v-model.number="maxTokens" :disabled="!!lastResult" type="number" min="512" max="1000000" placeholder="4096" class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:opacity-50" />
            </div>
            <div class="flex flex-col gap-1">
              <label class="text-2xs font-semibold uppercase text-surface-400">Project ID</label>
              <input v-model="projectId" :disabled="!!lastResult" type="text" placeholder="可选" class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm font-mono text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:opacity-50" />
            </div>
          </div>
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-semibold uppercase text-surface-400">对话历史</label>
            <textarea v-model="conversationHistory" :disabled="!!lastResult" rows="4" placeholder="可选，用于上下文感知..." class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:opacity-50" />
          </div>
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-semibold uppercase text-surface-400">强制展开的卡片类型</label>
            <div class="flex flex-wrap gap-1.5">
              <label v-for="card in CARD_TYPE_OPTIONS" :key="card.key" class="flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-xs cursor-pointer transition-colors" :class="expandCards.includes(card.key) ? 'bg-brand-50 border-brand-300 text-brand-700' : 'border-surface-200 text-surface-500 hover:border-surface-300'">
                <input :checked="expandCards.includes(card.key)" :disabled="!!lastResult" type="checkbox" class="sr-only" @change="(e: Event) => { const t = e.target as HTMLInputElement; if (t.checked) expandCards.push(card.key); else expandCards = expandCards.filter(c => c !== card.key); }" />
                <span>{{ card.label }}</span>
              </label>
            </div>
          </div>
        </div>

        <div class="space-y-4">
          <div class="flex items-center gap-2 mb-1">
            <span class="text-2xs font-semibold uppercase text-surface-400">策略覆盖</span>
            <span class="text-2xs text-surface-300">(留空使用默认策略)</span>
          </div>
          <div class="space-y-3 rounded-lg border border-surface-200 bg-surface-50 p-4">
            <!-- Soul Card Strategy -->
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="text-sm">💫</span>
                <span class="text-xs font-medium text-surface-700">灵魂卡 (soul_card)</span>
                <span class="badge text-2xs bg-surface-100 text-surface-400">默认: always</span>
              </div>
              <select v-model="soulStrategy" :disabled="!!lastResult" class="rounded-lg border border-surface-200 bg-white px-2.5 py-1.5 text-xs text-surface-600 focus:border-brand-500 focus:outline-none disabled:opacity-50">
                <option v-for="opt in STRATEGY_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
            </div>
            <!-- Identity Card Strategy -->
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="text-sm">🪪</span>
                <span class="text-xs font-medium text-surface-700">身份卡 (identity_card)</span>
                <span class="badge text-2xs bg-surface-100 text-surface-400">默认: always</span>
              </div>
              <select v-model="identityStrategy" :disabled="!!lastResult" class="rounded-lg border border-surface-200 bg-white px-2.5 py-1.5 text-xs text-surface-600 focus:border-brand-500 focus:outline-none disabled:opacity-50">
                <option v-for="opt in STRATEGY_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
            </div>
            <!-- Tool Catalog Strategy -->
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="text-sm">🔧</span>
                <span class="text-xs font-medium text-surface-700">工具目录 (tool_catalog)</span>
                <span class="badge text-2xs bg-surface-100 text-surface-400">默认: always</span>
              </div>
              <select v-model="toolCatalogStrategy" :disabled="!!lastResult" class="rounded-lg border border-surface-200 bg-white px-2.5 py-1.5 text-xs text-surface-600 focus:border-brand-500 focus:outline-none disabled:opacity-50">
                <option v-for="opt in STRATEGY_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
            </div>
            <!-- User Profile Strategy -->
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="text-sm">👤</span>
                <span class="text-xs font-medium text-surface-700">用户画像 (user_profile)</span>
                <span class="badge text-2xs bg-surface-100 text-surface-400">默认: moderate</span>
              </div>
              <select v-model="userProfileStrategy" :disabled="!!lastResult" class="rounded-lg border border-surface-200 bg-white px-2.5 py-1.5 text-xs text-surface-600 focus:border-brand-500 focus:outline-none disabled:opacity-50">
                <option v-for="opt in STRATEGY_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
            </div>
            <!-- Tool Detail Strategy -->
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="text-sm">🔍</span>
                <span class="text-xs font-medium text-surface-700">工具详情 (tool_detail)</span>
                <span class="badge text-2xs bg-surface-100 text-surface-400">默认: on_demand</span>
              </div>
              <select v-model="toolDetailStrategy" :disabled="!!lastResult" class="rounded-lg border border-surface-200 bg-white px-2.5 py-1.5 text-xs text-surface-600 focus:border-brand-500 focus:outline-none disabled:opacity-50">
                <option v-for="opt in STRATEGY_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
            </div>
          </div>
        </div>
      </div>

      <div class="mt-6 flex items-center gap-3 pt-4 border-t border-surface-200">
        <button class="btn btn-primary" :disabled="submitting || !selectedAgentId || !queryText.trim() || !!lastResult" @click="handleAssemble">
          {{ submitting ? "组装中..." : "🧠 开始上下文组装" }}
        </button>
        <span v-if="!selectedAgentId || !queryText.trim()" class="text-xs text-surface-400">请先选择 Agent 并填写查询文本</span>
      </div>

      <div v-if="submitError" class="mt-4 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">{{ submitError }}</div>
    </div>

    <div v-if="lastResult" class="space-y-6">
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div class="card p-4 text-center"><p class="text-2xs font-semibold uppercase text-surface-400 mb-1">总计 Tokens</p><p class="text-2xl font-bold text-surface-800 font-mono tabular-nums">{{ formatTokens(lastResult.total_tokens) }}</p></div>
        <div class="card p-4 text-center"><p class="text-2xs font-semibold uppercase text-surface-400 mb-1">卡片分段数</p><p class="text-2xl font-bold text-surface-800 font-mono tabular-nums">{{ lastResult.sections.length }}</p></div>
        <div class="card p-4 text-center"><p class="text-2xs font-semibold uppercase text-surface-400 mb-1">可用预算</p><p class="text-2xl font-bold text-surface-800 font-mono tabular-nums">{{ formatTokens(lastResult.budget.usable) }}</p></div>
        <div class="card p-4 text-center"><p class="text-2xs font-semibold uppercase text-surface-400 mb-1">剩余</p><p class="text-2xl font-bold font-mono tabular-nums" :class="lastResult.budget.remaining > 0 ? 'text-emerald-600' : 'text-amber-600'">{{ formatTokens(lastResult.budget.remaining) }}</p></div>
      </div>

      <div v-if="lastResult.degradation_reason" class="card border-amber-200 bg-amber-50 p-4">
        <div class="flex items-center gap-2"><span class="text-amber-600">⚠️</span><span class="text-sm font-medium text-amber-700">降级警告</span></div>
        <p class="text-xs text-amber-600 mt-1">{{ lastResult.degradation_reason }}</p>
      </div>

      <div class="card">
        <div class="flex items-center gap-1 border-b border-surface-200 px-4">
          <button v-for="t in (['text','sections','budget'] as const)" :key="t" :class="['px-3 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px', resultViewTab === t ? 'border-brand-500 text-brand-600' : 'border-transparent text-surface-500 hover:text-surface-700']" @click="resultViewTab = t">
            {{ t === 'text' ? '📝 组装文本' : t === 'sections' ? '🃏 卡片分段' : '💰 Token 预算' }}
          </button>
        </div>

        <div v-if="resultViewTab === 'text'" class="p-4">
          <div class="rounded-lg border border-surface-200 bg-surface-50 p-4 max-h-[500px] overflow-y-auto">
            <pre class="text-xs font-mono text-surface-700 whitespace-pre-wrap break-words">{{ lastResult.assembled_text }}</pre>
          </div>
          <p class="text-2xs text-surface-400 mt-2">{{ lastResult.assembled_text.length.toLocaleString() }} 字符 · {{ lastResult.total_tokens.toLocaleString() }} tokens</p>
        </div>

        <div v-if="resultViewTab === 'sections'" class="divide-y divide-surface-100">
          <div v-for="(s, idx) in lastResult.sections" :key="idx" class="px-4 py-4 hover:bg-surface-50 transition-colors">
            <div class="flex items-start gap-3">
              <span class="shrink-0 mt-0.5">{{ s.card_type === 'soul_card' ? '💫' : s.card_type === 'identity_card' ? '🪪' : s.card_type === 'tool_catalog' ? '🔧' : s.card_type === 'user_profile' ? '👤' : '🔍' }}</span>
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2 flex-wrap">
                  <p class="text-sm font-semibold text-surface-700">{{ s.card_type }}</p>
                  <span :class="['badge text-2xs', getStrategyColor(s.strategy)]">{{ getStrategyLabel(s.strategy) }}</span>
                  <span class="text-2xs text-surface-400 font-mono">{{ s.token_count }} tokens</span>
                  <span v-if="s.truncated" class="badge text-2xs bg-red-50 text-red-600">已截断</span>
                </div>
                <p v-if="s.store_name" class="text-2xs text-surface-400 mt-0.5">来源: {{ s.store_name }}</p>
                <div class="mt-2 rounded border border-surface-200 bg-surface-50 p-3 max-h-[160px] overflow-y-auto">
                  <pre class="text-2xs font-mono text-surface-600 whitespace-pre-wrap break-words">{{ s.content }}</pre>
                </div>
                <div v-if="s.memory_ids.length > 0" class="flex flex-wrap gap-1 mt-1.5">
                  <span v-for="mid in s.memory_ids" :key="mid" class="badge text-2xs bg-violet-50 text-violet-600 font-mono">{{ String(mid).slice(0, 8) }}…</span>
                </div>
              </div>
            </div>
          </div>
          <div v-if="lastResult.sections.length === 0" class="flex flex-col items-center justify-center py-12 text-sm text-surface-400"><span class="text-3xl mb-2">📦</span><span>无卡片分段</span></div>
        </div>

        <div v-if="resultViewTab === 'budget'" class="p-6">
          <div class="max-w-md mx-auto space-y-4">
            <div><div class="flex items-center justify-between mb-1"><span class="text-xs text-surface-500">总可用预算</span><span class="font-mono text-xs font-semibold text-surface-700">{{ formatTokens(lastResult.budget.total_available) }}</span></div><div class="h-2 bg-surface-100 rounded-full overflow-hidden"><div class="h-full bg-surface-300 rounded-full" style="width:100%"></div></div></div>
            <div><div class="flex items-center justify-between mb-1"><span class="text-xs text-surface-500">系统开销</span><span class="font-mono text-xs text-surface-600">{{ formatTokens(lastResult.budget.system_overhead) }}</span></div><div class="h-2 bg-surface-100 rounded-full overflow-hidden"><div class="h-full bg-slate-400 rounded-full" :style="{ width: (lastResult.budget.total_available > 0 ? (lastResult.budget.system_overhead / lastResult.budget.total_available * 100) : 0) + '%' }"></div></div></div>
            <div><div class="flex items-center justify-between mb-1"><span class="text-xs text-surface-500">输出预留</span><span class="font-mono text-xs text-surface-600">{{ formatTokens(lastResult.budget.output_reserve) }}</span></div><div class="h-2 bg-surface-100 rounded-full overflow-hidden"><div class="h-full bg-slate-300 rounded-full" :style="{ width: (lastResult.budget.total_available > 0 ? (lastResult.budget.output_reserve / lastResult.budget.total_available * 100) : 0) + '%' }"></div></div></div>
            <div class="pt-2 border-t border-surface-200"><div class="flex items-center justify-between mb-1"><span class="text-xs font-semibold text-surface-700">可用预算</span><span class="font-mono text-xs font-semibold text-brand-600">{{ formatTokens(lastResult.budget.usable) }}</span></div><div class="h-3 bg-surface-100 rounded-full overflow-hidden"><div class="h-full bg-brand-400 rounded-full" :style="{ width: (lastResult.budget.total_available > 0 ? (lastResult.budget.usable / lastResult.budget.total_available * 100) : 0) + '%' }"></div></div></div>
            <div class="pt-3 space-y-2">
              <p class="text-2xs font-semibold uppercase text-surface-400">策略消耗明细</p>
              <div><div class="flex items-center justify-between mb-0.5"><span class="text-xs text-surface-500">🔒 Always</span><span class="font-mono text-xs text-violet-600">{{ formatTokens(lastResult.budget.always_used) }}</span></div><div class="h-1.5 bg-surface-100 rounded-full overflow-hidden"><div class="h-full bg-violet-400 rounded-full" :style="{ width: (lastResult.budget.usable > 0 ? (lastResult.budget.always_used / lastResult.budget.usable * 100) : 0) + '%' }"></div></div></div>
              <div><div class="flex items-center justify-between mb-0.5"><span class="text-xs text-surface-500">📊 Moderate</span><span class="font-mono text-xs text-blue-600">{{ formatTokens(lastResult.budget.moderate_used) }}</span></div><div class="h-1.5 bg-surface-100 rounded-full overflow-hidden"><div class="h-full bg-blue-400 rounded-full" :style="{ width: (lastResult.budget.usable > 0 ? (lastResult.budget.moderate_used / lastResult.budget.usable * 100) : 0) + '%' }"></div></div></div>
              <div><div class="flex items-center justify-between mb-0.5"><span class="text-xs text-surface-500">🔍 On-Demand</span><span class="font-mono text-xs text-amber-600">{{ formatTokens(lastResult.budget.on_demand_used) }}</span></div><div class="h-1.5 bg-surface-100 rounded-full overflow-hidden"><div class="h-full bg-amber-400 rounded-full" :style="{ width: (lastResult.budget.usable > 0 ? (lastResult.budget.on_demand_used / lastResult.budget.usable * 100) : 0) + '%' }"></div></div></div>
              <div class="pt-2 border-t border-surface-100"><div class="flex items-center justify-between"><span class="text-xs font-semibold text-surface-700">✅ 剩余</span><span class="font-mono text-xs font-semibold" :class="lastResult.budget.remaining > 0 ? 'text-emerald-600' : 'text-red-600'">{{ formatTokens(lastResult.budget.remaining) }}</span></div></div>
            </div>
          </div>
        </div>
      </div>

      <div class="card p-4">
        <p class="text-2xs font-semibold uppercase text-surface-400 mb-2">策略摘要</p>
        <div class="flex flex-wrap gap-2">
          <span v-for="(strategy, cardType) in lastResult.strategy_summary" :key="cardType" class="badge text-xs" :class="getStrategyColor(strategy as InjectionStrategy)">{{ cardType }}: {{ getStrategyLabel(strategy as InjectionStrategy) }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
