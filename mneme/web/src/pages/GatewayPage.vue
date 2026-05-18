<script setup lang="ts">
import { ref, computed } from "vue";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import PageHeader from "@/components/PageHeader.vue";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import {
  fetchGateProviders,
  fetchGateProvider,
  createGateProvider,
  updateGateProvider,
  fetchGateProviderModels,
  fetchGateProviderModel,
  createGateProviderModel,
  updateGateProviderModel,
  fetchGateCapabilities,
  seedGateCapabilities,
  createGateCapability,
  fetchGateBindings,
  fetchGateBinding,
  createGateBinding,
  updateGateBinding,
  fetchGateLimits,
  fetchGateLimit,
  createGateLimit,
  updateGateLimit,
  deleteGateLimit,
  fetchGateLimitUsage,
} from "@/api/client";

// ── Sub-tab state ──
const activeTab = ref<"providers" | "models" | "capabilities" | "bindings" | "limits">("providers");

function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

// ═══════════════════════ Providers ═══════════════════════
const providerPage = ref(1);
const providerFilter = ref("");
const { data: providersData, isLoading: providersLoading } = useQuery({
  queryKey: ["gate-providers", providerPage, providerFilter],
  queryFn: () => fetchGateProviders({ page: providerPage.value, page_size: 20, search: providerFilter.value || undefined }),
});

const providerColumns = [
  { key: "name", label: "名称", width: "180px" },
  { key: "provider_code", label: "Provider Code", width: "160px" },
  { key: "provider_type", label: "类型", width: "100px" },
  { key: "status", label: "状态", width: "90px" },
  { key: "endpoint_base", label: "Endpoint", width: "200px" },
  { key: "created_at", label: "创建时间", width: "160px" },
];

// Provider detail drawer
const drawerOpen = ref(false);
const selectedProviderId = ref<string | null>(null);
const { data: providerDetail } = useQuery({
  queryKey: ["gate-provider", selectedProviderId],
  queryFn: () => fetchGateProvider(selectedProviderId.value!),
  enabled: computed(() => drawerOpen.value && !!selectedProviderId.value),
});

const providerForm = ref({ provider_code: "", name: "", provider_type: "llm", endpoint_base: "", config_json: "{}" });
const providerCreateMode = ref(false);
const providerSubmitting = ref(false);

function openProviderCreate() {
  providerCreateMode.value = true;
  providerForm.value = { provider_code: "", name: "", provider_type: "llm", endpoint_base: "", config_json: "{}" };
  selectedProviderId.value = null;
  drawerOpen.value = true;
}

function openProviderDetail(id: string) {
  providerCreateMode.value = false;
  selectedProviderId.value = id;
  drawerOpen.value = true;
}

async function handleProviderSave() {
  providerSubmitting.value = true;
  try {
    if (providerCreateMode.value) {
      await createGateProvider({
        provider_code: providerForm.value.provider_code,
        name: providerForm.value.name,
        provider_type: providerForm.value.provider_type,
        endpoint_base: providerForm.value.endpoint_base || null,
        config_json: providerForm.value.config_json ? JSON.parse(providerForm.value.config_json) : null,
      });
    } else if (selectedProviderId.value) {
      await updateGateProvider(selectedProviderId.value, {
        name: providerForm.value.name || undefined,
        endpoint_base: providerForm.value.endpoint_base || undefined,
        config_json: providerForm.value.config_json ? JSON.parse(providerForm.value.config_json) : undefined,
      });
    }
    drawerOpen.value = false;
    queryClient.invalidateQueries({ queryKey: ["gate-providers"] });
  } catch (e) { console.error(e); }
  finally { providerSubmitting.value = false; }
}

// ═══════════════════════ Models ═══════════════════════
const modelProviderId = ref<string | null>(null);
const modelPage = ref(1);
const { data: modelsData, isLoading: modelsLoading } = useQuery({
  queryKey: ["gate-models", modelProviderId, modelPage],
  queryFn: () => fetchGateProviderModels(modelProviderId.value!, { page: modelPage.value, page_size: 20 }),
  enabled: computed(() => activeTab.value === "models" && !!modelProviderId.value),
});

const modelColumns = [
  { key: "model_code", label: "Model Code", width: "160px" },
  { key: "display_name", label: "显示名", width: "160px" },
  { key: "model_type", label: "类型", width: "100px" },
  { key: "status", label: "状态", width: "90px" },
  { key: "context_window_tokens", label: "上下文窗口", width: "100px" },
  { key: "input_price_per_1k", label: "输入价格/1K", width: "110px" },
  { key: "output_price_per_1k", label: "输出价格/1K", width: "110px" },
  { key: "created_at", label: "创建时间", width: "160px" },
];

const modelDrawerOpen = ref(false);
const selectedModelId = ref<string | null>(null);
const modelCreateMode = ref(false);
const modelSubmitting = ref(false);
const modelForm = ref({
  model_code: "", external_model_id: "", model_type: "chat", display_name: "",
  context_window_tokens: 4096, max_input_tokens: 4000, max_output_tokens: 4000,
  input_price_per_1k: 0, output_price_per_1k: 0, currency_code: "USD",
  supports_streaming: true, supports_json_mode: false, supports_tools: false, supports_vision: false,
  sensitivity_ceiling: "normal",
});

function openModelCreate() {
  modelCreateMode.value = true;
  modelForm.value = {
    model_code: "", external_model_id: "", model_type: "chat", display_name: "",
    context_window_tokens: 4096, max_input_tokens: 4000, max_output_tokens: 4000,
    input_price_per_1k: 0, output_price_per_1k: 0, currency_code: "USD",
    supports_streaming: true, supports_json_mode: false, supports_tools: false, supports_vision: false,
    sensitivity_ceiling: "normal",
  };
  selectedModelId.value = null;
  modelDrawerOpen.value = true;
}

function openModelDetail(modelId: string) {
  modelCreateMode.value = false;
  selectedModelId.value = modelId;
  modelDrawerOpen.value = true;
}

async function handleModelSave() {
  if (!modelProviderId.value) return;
  modelSubmitting.value = true;
  try {
    if (modelCreateMode.value) {
      await createGateProviderModel(modelProviderId.value, {
        model_code: modelForm.value.model_code,
        external_model_id: modelForm.value.external_model_id,
        model_type: modelForm.value.model_type,
        display_name: modelForm.value.display_name || undefined,
        context_window_tokens: modelForm.value.context_window_tokens,
        max_input_tokens: modelForm.value.max_input_tokens,
        max_output_tokens: modelForm.value.max_output_tokens,
        input_price_per_1k: modelForm.value.input_price_per_1k,
        output_price_per_1k: modelForm.value.output_price_per_1k,
        currency_code: modelForm.value.currency_code,
        supports_streaming: modelForm.value.supports_streaming,
        supports_json_mode: modelForm.value.supports_json_mode,
        supports_tools: modelForm.value.supports_tools,
        supports_vision: modelForm.value.supports_vision,
        sensitivity_ceiling: modelForm.value.sensitivity_ceiling,
      });
    }
    modelDrawerOpen.value = false;
    queryClient.invalidateQueries({ queryKey: ["gate-models"] });
  } catch (e) { console.error(e); }
  finally { modelSubmitting.value = false; }
}

// ═══════════════════════ Capabilities ═══════════════════════
const capPage = ref(1);
const { data: capsData, isLoading: capsLoading } = useQuery({
  queryKey: ["gate-caps", capPage],
  queryFn: () => fetchGateCapabilities({ page: capPage.value, page_size: 20 }),
});

const capColumns = [
  { key: "capability_code", label: "Capability Code", width: "180px" },
  { key: "name", label: "名称", width: "180px" },
  { key: "category", label: "类别", width: "100px" },
  { key: "risk_level", label: "风险等级", width: "90px" },
  { key: "default_budget_mode", label: "默认预算模式", width: "120px" },
  { key: "created_at", label: "创建时间", width: "160px" },
];

const capCreateForm = ref({ capability_code: "", name: "", category: "chat", risk_level: "normal", default_budget_mode: "per_call" });
const capCreating = ref(false);
const capShowForm = ref(false);

async function handleSeedCaps() {
  capCreating.value = true;
  try { await seedGateCapabilities(); queryClient.invalidateQueries({ queryKey: ["gate-caps"] }); }
  catch (e) { console.error(e); }
  finally { capCreating.value = false; }
}

async function handleCreateCap() {
  capCreating.value = true;
  try {
    await createGateCapability({
      capability_code: capCreateForm.value.capability_code,
      name: capCreateForm.value.name,
      category: capCreateForm.value.category,
      risk_level: capCreateForm.value.risk_level,
      default_budget_mode: capCreateForm.value.default_budget_mode,
    });
    capShowForm.value = false;
    capCreateForm.value = { capability_code: "", name: "", category: "chat", risk_level: "normal", default_budget_mode: "per_call" };
    queryClient.invalidateQueries({ queryKey: ["gate-caps"] });
  } catch (e) { console.error(e); }
  finally { capCreating.value = false; }
}

// ═══════════════════════ Bindings ═══════════════════════
const bindingPage = ref(1);
const { data: bindingsData, isLoading: bindingsLoading } = useQuery({
  queryKey: ["gate-bindings", bindingPage],
  queryFn: () => fetchGateBindings({ page: bindingPage.value, page_size: 20 }),
});

const bindingColumns = [
  { key: "binding_id", label: "Binding ID", width: "140px" },
  { key: "capability_id", label: "Capability ID", width: "140px" },
  { key: "provider_id", label: "Provider ID", width: "140px" },
  { key: "binding_scope", label: "Scope", width: "100px" },
  { key: "status", label: "状态", width: "90px" },
  { key: "priority", label: "优先级", width: "70px" },
  { key: "created_at", label: "创建时间", width: "160px" },
];

const bindDetailOpen = ref(false);
const selectedBindingId = ref<string | null>(null);
const { data: bindingDetail } = useQuery({
  queryKey: ["gate-binding", selectedBindingId],
  queryFn: () => fetchGateBinding(selectedBindingId.value!),
  enabled: computed(() => bindDetailOpen.value && !!selectedBindingId.value),
});

// ═══════════════════════ Limits ═══════════════════════
const limitPage = ref(1);
const { data: limitsData, isLoading: limitsLoading } = useQuery({
  queryKey: ["gate-limits", limitPage],
  queryFn: () => fetchGateLimits({ page: limitPage.value, page_size: 20 }),
});

const limitColumns = [
  { key: "limit_id", label: "Limit ID", width: "140px" },
  { key: "subject_type", label: "主体类型", width: "100px" },
  { key: "subject_id", label: "主体ID", width: "140px" },
  { key: "limit_scope", label: "范围", width: "90px" },
  { key: "window_unit", label: "窗口", width: "80px" },
  { key: "max_cost", label: "最大费用", width: "90px" },
  { key: "enabled", label: "启用", width: "60px" },
  { key: "created_at", label: "创建时间", width: "160px" },
];

const limitDrawerOpen = ref(false);
const selectedLimitId = ref<string | null>(null);
const limitCreateMode = ref(false);
const limitSubmitting = ref(false);
const limitForm = ref({
  subject_type: "user", subject_id: "", limit_scope: "provider",
  window_unit: "daily", max_requests: 1000, max_cost: 10,
  enabled: true, capability_id: "", provider_id: "", project_id: "",
});

const { data: limitDetail } = useQuery({
  queryKey: ["gate-limit", selectedLimitId],
  queryFn: () => fetchGateLimit(selectedLimitId.value!),
  enabled: computed(() => limitDrawerOpen.value && !!selectedLimitId.value && !limitCreateMode.value),
});

const { data: limitUsage } = useQuery({
  queryKey: ["gate-limit-usage", selectedLimitId],
  queryFn: () => fetchGateLimitUsage(selectedLimitId.value!),
  enabled: computed(() => limitDrawerOpen.value && !!selectedLimitId.value && !limitCreateMode.value),
});

function openLimitCreate() {
  limitCreateMode.value = true;
  limitForm.value = {
    subject_type: "user", subject_id: "", limit_scope: "provider",
    window_unit: "daily", max_requests: 1000, max_cost: 10,
    enabled: true, capability_id: "", provider_id: "", project_id: "",
  };
  selectedLimitId.value = null;
  limitDrawerOpen.value = true;
}

function openLimitDetail(id: string) {
  limitCreateMode.value = false;
  selectedLimitId.value = id;
  limitDrawerOpen.value = true;
}

async function handleLimitSave() {
  limitSubmitting.value = true;
  try {
    if (limitCreateMode.value) {
      await createGateLimit({
        subject_type: limitForm.value.subject_type,
        subject_id: limitForm.value.subject_id || undefined,
        limit_scope: limitForm.value.limit_scope,
        window_unit: limitForm.value.window_unit,
        max_requests: limitForm.value.max_requests,
        max_cost: limitForm.value.max_cost,
        enabled: limitForm.value.enabled,
        capability_id: limitForm.value.capability_id || undefined,
        provider_id: limitForm.value.provider_id || undefined,
        project_id: limitForm.value.project_id || undefined,
      });
    } else if (selectedLimitId.value) {
      await updateGateLimit(selectedLimitId.value, {
        max_requests: limitForm.value.max_requests,
        max_cost: limitForm.value.max_cost,
        enabled: limitForm.value.enabled,
        window_unit: limitForm.value.window_unit,
      });
    }
    limitDrawerOpen.value = false;
    queryClient.invalidateQueries({ queryKey: ["gate-limits"] });
  } catch (e) { console.error(e); }
  finally { limitSubmitting.value = false; }
}

async function handleDeleteLimit(id: string) {
  if (!confirm("确定要删除此用量限制？")) return;
  try {
    await deleteGateLimit(id);
    queryClient.invalidateQueries({ queryKey: ["gate-limits"] });
  } catch (e) { console.error(e); }
}

const queryClient = useQueryClient();

const TAB_DEFS = [
  { key: "providers", label: "Providers", desc: "管理 Provider（LLM / Embedding 等）", count: null as string | null },
  { key: "models", label: "Models", desc: "Provider 下注册的模型", count: null },
  { key: "capabilities", label: "Capabilities", desc: "能力定义与预置种子", count: null },
  { key: "bindings", label: "Bindings", desc: "能力×Provider×凭据绑定", count: null },
  { key: "limits", label: "Limits", desc: "用量限制与预算规则", count: null },
];
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader title="API 管理" subtitle="Gateway — Provider、模型、能力、绑定、用量限制（24 端点）" />

    <!-- Tabs -->
    <div class="flex overflow-x-auto border-b border-surface-200 mb-6 -mx-4 px-4">
      <button
        v-for="tab in TAB_DEFS"
        :key="tab.key"
        @click="activeTab = tab.key as any"
        :class="[
          'shrink-0 pb-3 px-4 text-sm font-medium border-b-2 transition-colors',
          activeTab === tab.key
            ? 'border-brand-500 text-brand-600'
            : 'border-transparent text-surface-400 hover:text-surface-600',
        ]"
      >
        {{ tab.label }}
      </button>
    </div>

    <!-- ════════════════ Providers Tab ════════════════ -->
    <div v-if="activeTab === 'providers'">
      <div class="card mb-4 p-4 flex items-center justify-between">
        <p class="text-sm text-surface-500">已注册的 Provider（如 OpenAI、Anthropic）。</p>
        <div class="flex items-center gap-3">
          <input
            v-model="providerFilter"
            placeholder="搜索…"
            class="rounded-lg border border-surface-200 bg-white px-3 py-1.5 text-xs text-surface-700 focus:border-brand-500 focus:outline-none w-48"
          />
          <button class="btn btn-primary btn-sm" @click="openProviderCreate">+ 注册 Provider</button>
        </div>
      </div>

      <DataTable
        :items="providersData?.items ?? []"
        :columns="providerColumns"
        :loading="providersLoading"
        empty-message="暂无 Provider"
        clickable
        row-key="provider_id"
        @row-click="(row: any) => openProviderDetail(row.provider_id)"
      >
        <template #cell-name="{ value }">
          <span class="text-sm font-medium text-surface-800">{{ value }}</span>
        </template>
        <template #cell-provider_code="{ value }">
          <span class="font-mono text-xs text-surface-600">{{ value }}</span>
        </template>
        <template #cell-provider_type="{ value }">
          <span class="badge text-2xs bg-indigo-50 text-indigo-600">{{ value }}</span>
        </template>
        <template #cell-status="{ value }">
          <StatusBadge :status="value as string" />
        </template>
        <template #cell-endpoint_base="{ value }">
          <span class="font-mono text-2xs text-surface-400 truncate max-w-[180px] inline-block">{{ value || "—" }}</span>
        </template>
        <template #cell-created_at="{ value }">
          <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
        </template>
      </DataTable>

      <Pagination
        v-if="providersData?.page_info"
        :page-info="providersData.page_info"
        @page-change="(p: number) => (providerPage = p)"
      />

      <!-- Provider Detail Drawer -->
      <DetailDrawer :open="drawerOpen" title="Provider 详情" width="w-[560px] max-w-full" @close="drawerOpen = false">
        <div v-if="providerCreateMode || providerDetail">
          <div v-if="providerCreateMode" class="space-y-4">
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">Provider Code</label>
              <input v-model="providerForm.provider_code" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1" />
            </div>
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">名称</label>
              <input v-model="providerForm.name" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1" />
            </div>
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">类型</label>
              <select v-model="providerForm.provider_type" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1">
                <option value="llm">llm</option>
                <option value="embedding">embedding</option>
                <option value="ocr">ocr</option>
                <option value="image">image</option>
                <option value="audio">audio</option>
                <option value="rerank">rerank</option>
                <option value="search">search</option>
              </select>
            </div>
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">Endpoint Base</label>
              <input v-model="providerForm.endpoint_base" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1" />
            </div>
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">Config JSON</label>
              <textarea v-model="providerForm.config_json" rows="4" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-xs font-mono mt-1" />
            </div>
          </div>
          <dl v-else-if="providerDetail" class="space-y-3">
            <div class="grid grid-cols-2 gap-4">
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">Provider Code</dt><dd class="mt-1 font-mono text-xs">{{ providerDetail.provider_code }}</dd></div>
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">名称</dt><dd class="mt-1 text-sm">{{ providerDetail.name }}</dd></div>
            </div>
            <div class="grid grid-cols-2 gap-4">
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">类型</dt><dd class="mt-1"><span class="badge text-2xs bg-indigo-50 text-indigo-600">{{ providerDetail.provider_type }}</span></dd></div>
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">状态</dt><dd class="mt-1"><StatusBadge :status="providerDetail.status" /></dd></div>
            </div>
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">Endpoint</dt><dd class="mt-1 font-mono text-xs break-all">{{ providerDetail.endpoint_base || "—" }}</dd></div>
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">ID</dt><dd class="mt-1 font-mono text-2xs">{{ providerDetail.provider_id }}</dd></div>
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt><dd class="mt-1 text-xs">{{ formatTime(providerDetail.created_at) }}</dd></div>
            <div v-if="providerDetail.config_json"><dt class="text-2xs font-semibold uppercase text-surface-400">Config</dt><dd class="mt-1"><pre class="text-2xs font-mono bg-surface-50 p-2 rounded max-h-32 overflow-auto">{{ JSON.stringify(providerDetail.config_json, null, 2) }}</pre></dd></div>
          </dl>

          <div class="flex justify-end gap-2 mt-6">
            <button class="btn btn-secondary btn-sm" @click="drawerOpen = false">取消</button>
            <button class="btn btn-primary btn-sm" :disabled="providerSubmitting" @click="handleProviderSave">
              {{ providerSubmitting ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
        <div v-else class="py-12 text-center text-sm text-surface-400">
          <LoadingSkeleton variant="detail" />
        </div>
      </DetailDrawer>
    </div>

    <!-- ════════════════ Models Tab ════════════════ -->
    <div v-if="activeTab === 'models'">
      <div class="card mb-4 p-4">
        <div class="flex flex-wrap items-end gap-3">
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-semibold uppercase text-surface-400">选择 Provider</label>
            <select
              v-model="modelProviderId"
              class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none min-w-[260px]"
            >
              <option :value="null" disabled>请选择 Provider…</option>
              <option v-for="p in providersData?.items ?? []" :key="p.provider_id" :value="p.provider_id">
                {{ p.name }} ({{ p.provider_code }})
              </option>
            </select>
          </div>
          <button class="btn btn-primary btn-sm ml-auto" :disabled="!modelProviderId" @click="openModelCreate">+ 注册模型</button>
        </div>
      </div>

      <div v-if="!modelProviderId" class="py-16 text-center text-sm text-surface-400">
        <div class="flex flex-col items-center gap-2">
          <svg xmlns="http://www.w3.org/2000/svg" class="h-10 w-10 text-surface-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1"><path stroke-linecap="round" stroke-linejoin="round" d="M4 7v10c0 2 1 3 3 3h10c2 0 3-1 3-3V7c0-2-1-3-3-3H7c-2 0-3 1-3 3z"/></svg>
          <span>请先选择一个 Provider</span>
        </div>
      </div>

      <template v-else>
        <LoadingSkeleton v-if="modelsLoading" variant="table" />
        <DataTable
          v-else
          :items="modelsData?.items ?? []"
          :columns="modelColumns"
          :loading="false"
          empty-message="该 Provider 下暂无模型"
          clickable
          row-key="provider_model_id"
          @row-click="(row: any) => openModelDetail(row.provider_model_id)"
        >
          <template #cell-model_code="{ value }">
            <span class="font-mono text-xs text-surface-700">{{ value }}</span>
          </template>
          <template #cell-display_name="{ value }">
            <span class="text-sm font-medium text-surface-800">{{ value || "—" }}</span>
          </template>
          <template #cell-model_type="{ value }">
            <span class="badge text-2xs bg-emerald-50 text-emerald-600">{{ value }}</span>
          </template>
          <template #cell-status="{ value }">
            <StatusBadge :status="value as string" />
          </template>
          <template #cell-context_window_tokens="{ value }">
            <span class="font-mono text-xs text-surface-500">{{ (value as number)?.toLocaleString() }}</span>
          </template>
          <template #cell-input_price_per_1k="{ value }">
            <span class="font-mono text-xs text-surface-500">${{ value }}</span>
          </template>
          <template #cell-output_price_per_1k="{ value }">
            <span class="font-mono text-xs text-surface-500">${{ value }}</span>
          </template>
          <template #cell-created_at="{ value }">
            <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
          </template>
        </DataTable>
        <Pagination v-if="modelsData?.page_info" :page-info="modelsData.page_info" @page-change="(p: number) => (modelPage = p)" />
      </template>

      <!-- Model Create Drawer -->
      <DetailDrawer :open="modelDrawerOpen" title="模型详情" width="w-[580px] max-w-full" @close="modelDrawerOpen = false">
        <div v-if="modelCreateMode" class="space-y-3 max-h-[65vh] overflow-y-auto">
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">Model Code *</label>
              <input v-model="modelForm.model_code" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1" />
            </div>
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">External Model ID *</label>
              <input v-model="modelForm.external_model_id" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1" />
            </div>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">Model Type</label>
              <select v-model="modelForm.model_type" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1">
                <option value="chat">chat</option><option value="completion">completion</option>
                <option value="embedding">embedding</option><option value="image">image</option>
                <option value="audio">audio</option><option value="rerank">rerank</option>
              </select>
            </div>
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">显示名</label>
              <input v-model="modelForm.display_name" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1" />
            </div>
          </div>
          <div class="grid grid-cols-3 gap-3">
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">上下文窗口</label>
              <input v-model.number="modelForm.context_window_tokens" type="number" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1" />
            </div>
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">最大输入</label>
              <input v-model.number="modelForm.max_input_tokens" type="number" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1" />
            </div>
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">最大输出</label>
              <input v-model.number="modelForm.max_output_tokens" type="number" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1" />
            </div>
          </div>
          <div class="grid grid-cols-3 gap-3">
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">输入价格/1K</label>
              <input v-model.number="modelForm.input_price_per_1k" type="number" step="0.001" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1" />
            </div>
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">输出价格/1K</label>
              <input v-model.number="modelForm.output_price_per_1k" type="number" step="0.001" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1" />
            </div>
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">货币</label>
              <input v-model="modelForm.currency_code" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1" />
            </div>
          </div>
          <div>
            <label class="text-2xs font-semibold uppercase text-surface-400">支持特性</label>
            <div class="flex flex-wrap gap-3 mt-1">
              <label class="flex items-center gap-1 text-xs"><input v-model="modelForm.supports_streaming" type="checkbox" class="rounded" /> Streaming</label>
              <label class="flex items-center gap-1 text-xs"><input v-model="modelForm.supports_json_mode" type="checkbox" class="rounded" /> JSON Mode</label>
              <label class="flex items-center gap-1 text-xs"><input v-model="modelForm.supports_tools" type="checkbox" class="rounded" /> Tools</label>
              <label class="flex items-center gap-1 text-xs"><input v-model="modelForm.supports_vision" type="checkbox" class="rounded" /> Vision</label>
            </div>
          </div>
          <div>
            <label class="text-2xs font-semibold uppercase text-surface-400">Sensitivity Ceiling</label>
            <select v-model="modelForm.sensitivity_ceiling" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1">
              <option value="public">public</option><option value="normal">normal</option>
              <option value="private">private</option><option value="sensitive">sensitive</option><option value="secret">secret</option>
            </select>
          </div>
          <div class="flex justify-end gap-2 pt-3">
            <button class="btn btn-secondary btn-sm" @click="modelDrawerOpen = false">取消</button>
            <button class="btn btn-primary btn-sm" :disabled="modelSubmitting || !modelForm.model_code || !modelForm.external_model_id" @click="handleModelSave">
              {{ modelSubmitting ? '创建中…' : '创建' }}
            </button>
          </div>
        </div>
        <div v-else class="py-12 text-center text-sm text-surface-400">点击行查看模型详情</div>
      </DetailDrawer>
    </div>

    <!-- ════════════════ Capabilities Tab ════════════════ -->
    <div v-if="activeTab === 'capabilities'">
      <div class="card mb-4 p-4 flex items-center justify-between">
        <p class="text-sm text-surface-500">预定义能力（chat.completion / embedding.create …）。</p>
        <div class="flex items-center gap-2">
          <button class="btn btn-secondary btn-sm" :disabled="capCreating" @click="handleSeedCaps">
            {{ capCreating ? '初始化中…' : 'Seed Capabilities' }}
          </button>
          <button class="btn btn-primary btn-sm" @click="capShowForm = !capShowForm">
            {{ capShowForm ? '取消' : '+ 创建能力' }}
          </button>
        </div>
      </div>

      <!-- Create form -->
      <div v-if="capShowForm" class="card mb-4 p-4 border-brand-200 bg-brand-50">
        <div class="grid grid-cols-5 gap-3 items-end">
          <input v-model="capCreateForm.capability_code" placeholder="Capability Code" class="rounded-lg border border-surface-200 px-3 py-2 text-sm" />
          <input v-model="capCreateForm.name" placeholder="名称" class="rounded-lg border border-surface-200 px-3 py-2 text-sm" />
          <select v-model="capCreateForm.category" class="rounded-lg border border-surface-200 px-3 py-2 text-sm">
            <option value="chat">chat</option><option value="embedding">embedding</option>
            <option value="image">image</option><option value="audio">audio</option>
            <option value="rerank">rerank</option><option value="ocr">ocr</option><option value="search">search</option>
          </select>
          <select v-model="capCreateForm.risk_level" class="rounded-lg border border-surface-200 px-3 py-2 text-sm">
            <option value="low">low</option><option value="normal">normal</option><option value="high">high</option><option value="critical">critical</option>
          </select>
          <button class="btn btn-primary btn-sm" :disabled="capCreating || !capCreateForm.capability_code" @click="handleCreateCap">创建</button>
        </div>
      </div>

      <DataTable
        :items="capsData?.items ?? []"
        :columns="capColumns"
        :loading="capsLoading"
        empty-message="暂无能力定义，请先执行 Seed"
        row-key="capability_id"
      >
        <template #cell-capability_code="{ value }">
          <span class="font-mono text-xs text-surface-700 font-medium">{{ value }}</span>
        </template>
        <template #cell-name="{ value }">
          <span class="text-sm text-surface-800">{{ value }}</span>
        </template>
        <template #cell-category="{ value }">
          <span class="badge text-2xs bg-purple-50 text-purple-600">{{ value }}</span>
        </template>
        <template #cell-risk_level="{ value }">
          <span :class="[
            'badge text-2xs',
            value === 'critical' ? 'bg-red-50 text-red-700' :
            value === 'high' ? 'bg-amber-50 text-amber-700' :
            'bg-green-50 text-green-700'
          ]">{{ value }}</span>
        </template>
        <template #cell-default_budget_mode="{ value }">
          <span class="font-mono text-2xs text-surface-500">{{ value }}</span>
        </template>
        <template #cell-created_at="{ value }">
          <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
        </template>
      </DataTable>
    </div>

    <!-- ════════════════ Bindings Tab ════════════════ -->
    <div v-if="activeTab === 'bindings'">
      <div class="card mb-4 p-4 flex items-center justify-between">
        <p class="text-sm text-surface-500">能力绑定 — 将能力链接到 Provider/模型/凭据，定义路由规则。</p>
        <span class="text-xs text-surface-400 font-mono">{{ bindingsData?.page_info?.total_items ?? 0 }} 条绑定</span>
      </div>

      <DataTable
        :items="bindingsData?.items ?? []"
        :columns="bindingColumns"
        :loading="bindingsLoading"
        empty-message="暂无绑定"
        clickable
        row-key="capability_binding_id"
        @row-click="(row: any) => { selectedBindingId = row.capability_binding_id; bindDetailOpen = true }"
      >
        <template #cell-binding_id="{ value }">
          <span class="font-mono text-xs text-surface-600">{{ (value as string)?.slice(0, 12) }}…</span>
        </template>
        <template #cell-capability_id="{ value }">
          <span class="font-mono text-2xs text-surface-500">{{ (value as string)?.slice(0, 12) }}…</span>
        </template>
        <template #cell-provider_id="{ value }">
          <span class="font-mono text-2xs text-surface-500">{{ (value as string)?.slice(0, 12) }}…</span>
        </template>
        <template #cell-binding_scope="{ value }">
          <span class="badge text-2xs bg-sky-50 text-sky-600">{{ value }}</span>
        </template>
        <template #cell-status="{ value }">
          <StatusBadge :status="value as string" />
        </template>
        <template #cell-priority="{ value }">
          <span class="font-mono text-xs text-surface-600">{{ value }}</span>
        </template>
        <template #cell-created_at="{ value }">
          <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
        </template>
      </DataTable>

      <Pagination v-if="bindingsData?.page_info" :page-info="bindingsData.page_info" @page-change="(p: number) => (bindingPage = p)" />

      <!-- Binding Detail Drawer -->
      <DetailDrawer :open="bindDetailOpen" title="Binding 详情" width="w-[520px] max-w-full" @close="bindDetailOpen = false">
        <dl v-if="bindingDetail" class="space-y-3">
          <div><dt class="text-2xs font-semibold uppercase text-surface-400">Binding ID</dt><dd class="mt-1 font-mono text-xs break-all">{{ bindingDetail.capability_binding_id }}</dd></div>
          <div class="grid grid-cols-2 gap-4">
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">Capability ID</dt><dd class="mt-1 font-mono text-2xs">{{ bindingDetail.capability_id }}</dd></div>
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">Provider ID</dt><dd class="mt-1 font-mono text-2xs">{{ bindingDetail.provider_id }}</dd></div>
          </div>
          <div class="grid grid-cols-3 gap-4">
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">Scope</dt><dd class="mt-1"><span class="badge text-2xs bg-sky-50 text-sky-600">{{ bindingDetail.binding_scope }}</span></dd></div>
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">Status</dt><dd class="mt-1"><StatusBadge :status="bindingDetail.status" /></dd></div>
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">Priority</dt><dd class="mt-1 font-mono text-sm">{{ bindingDetail.priority }}</dd></div>
          </div>
          <div><dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt><dd class="mt-1 text-xs">{{ formatTime(bindingDetail.created_at) }}</dd></div>
        </dl>
        <div v-else class="py-12 text-center text-sm text-surface-400"><LoadingSkeleton variant="detail" /></div>
        <template #footer>
          <div class="flex justify-end"><button class="btn btn-secondary btn-sm" @click="bindDetailOpen = false">关闭</button></div>
        </template>
      </DetailDrawer>
    </div>

    <!-- ════════════════ Limits Tab ════════════════ -->
    <div v-if="activeTab === 'limits'">
      <div class="card mb-4 p-4 flex items-center justify-between">
        <p class="text-sm text-surface-500">用量限制与预算规则（按用户/Agent/项目/Capability/Provider）。</p>
        <button class="btn btn-primary btn-sm" @click="openLimitCreate">+ 创建限制</button>
      </div>

      <DataTable
        :items="limitsData?.items ?? []"
        :columns="limitColumns"
        :loading="limitsLoading"
        empty-message="暂无用量限制"
        clickable
        row-key="usage_limit_id"
        @row-click="(row: any) => openLimitDetail(row.usage_limit_id)"
      >
        <template #cell-limit_id="{ value }">
          <span class="font-mono text-xs text-surface-600">{{ (value as string)?.slice(0, 12) }}…</span>
        </template>
        <template #cell-subject_type="{ value }">
          <span class="badge text-2xs bg-amber-50 text-amber-700">{{ value }}</span>
        </template>
        <template #cell-subject_id="{ value }">
          <span class="font-mono text-2xs text-surface-500">{{ (value as string)?.slice(0, 12) ?? '—' }}</span>
        </template>
        <template #cell-limit_scope="{ value }">
          <span class="font-mono text-2xs text-surface-500">{{ value }}</span>
        </template>
        <template #cell-window_unit="{ value }">
          <span class="badge text-2xs bg-surface-100 text-surface-600">{{ value }}</span>
        </template>
        <template #cell-max_cost="{ value }">
          <span class="font-mono text-xs text-surface-700">${{ value ?? '—' }}</span>
        </template>
        <template #cell-enabled="{ value }">
          <span :class="['badge text-2xs', value ? 'bg-green-50 text-green-600' : 'bg-red-50 text-red-500']">{{ value ? '是' : '否' }}</span>
        </template>
        <template #cell-created_at="{ value }">
          <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
        </template>
      </DataTable>

      <Pagination v-if="limitsData?.page_info" :page-info="limitsData.page_info" @page-change="(p: number) => (limitPage = p)" />

      <!-- Limit Detail/Create Drawer -->
      <DetailDrawer :open="limitDrawerOpen" :title="limitCreateMode ? '创建用量限制' : '用量限制详情'" width="w-[540px] max-w-full" @close="limitDrawerOpen = false">
        <div v-if="limitCreateMode" class="space-y-3">
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">主体类型</label>
              <select v-model="limitForm.subject_type" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1">
                <option value="user">user</option><option value="agent">agent</option>
                <option value="project">project</option><option value="capability">capability</option><option value="provider">provider</option>
              </select>
            </div>
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">主体 ID</label>
              <input v-model="limitForm.subject_id" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1" placeholder="UUID (可选)" />
            </div>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">Scope</label>
              <select v-model="limitForm.limit_scope" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1">
                <option value="global">global</option><option value="provider">provider</option>
                <option value="capability">capability</option><option value="project">project</option>
              </select>
            </div>
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">Window</label>
              <select v-model="limitForm.window_unit" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1">
                <option value="daily">daily</option><option value="weekly">weekly</option><option value="monthly">monthly</option>
                <option value="hourly">hourly</option><option value="per_call">per_call</option>
              </select>
            </div>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">最大请求数</label>
              <input v-model.number="limitForm.max_requests" type="number" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1" />
            </div>
            <div>
              <label class="text-2xs font-semibold uppercase text-surface-400">最大费用 ($)</label>
              <input v-model.number="limitForm.max_cost" type="number" step="0.01" class="w-full rounded-lg border border-surface-200 px-3 py-2 text-sm mt-1" />
            </div>
          </div>
          <label class="flex items-center gap-2 text-sm">
            <input v-model="limitForm.enabled" type="checkbox" class="rounded" /> 启用
          </label>
          <div class="flex justify-end gap-2 pt-3">
            <button class="btn btn-secondary btn-sm" @click="limitDrawerOpen = false">取消</button>
            <button class="btn btn-primary btn-sm" :disabled="limitSubmitting" @click="handleLimitSave">{{ limitSubmitting ? '创建中…' : '创建' }}</button>
          </div>
        </div>

        <div v-else-if="limitDetail" class="space-y-3">
          <dl class="space-y-3">
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">Limit ID</dt><dd class="mt-1 font-mono text-xs break-all">{{ limitDetail.usage_limit_id }}</dd></div>
            <div class="grid grid-cols-2 gap-4">
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">主体类型</dt><dd class="mt-1"><span class="badge text-2xs bg-amber-50 text-amber-700">{{ limitDetail.subject_type }}</span></dd></div>
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">Scope</dt><dd class="mt-1 font-mono text-xs">{{ limitDetail.limit_scope }}</dd></div>
            </div>
            <div class="grid grid-cols-2 gap-4">
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">Window</dt><dd class="mt-1 font-mono text-xs">{{ limitDetail.window_unit }}</dd></div>
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">启用</dt><dd class="mt-1"><span :class="['badge text-2xs', limitDetail.enabled ? 'bg-green-50 text-green-600' : 'bg-red-50 text-red-500']">{{ limitDetail.enabled ? '是' : '否' }}</span></dd></div>
            </div>
            <div class="grid grid-cols-2 gap-4">
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">最大请求</dt><dd class="mt-1 font-mono text-sm">{{ limitDetail.max_requests ?? '—' }}</dd></div>
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">最大费用</dt><dd class="mt-1 font-mono text-sm">${{ limitDetail.max_cost ?? '—' }}</dd></div>
            </div>
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt><dd class="mt-1 text-xs">{{ formatTime(limitDetail.created_at) }}</dd></div>
          </dl>

          <!-- Usage -->
          <div v-if="limitUsage" class="mt-4 rounded-lg border border-brand-200 bg-brand-50 p-4">
            <p class="text-xs font-semibold text-brand-700 mb-2">📊 当前使用量</p>
            <div class="grid grid-cols-3 gap-3 text-center">
              <div><p class="text-2xs text-surface-400">请求数</p><p class="font-mono text-sm font-semibold text-surface-700">{{ limitUsage.current_requests ?? 0 }}</p></div>
              <div><p class="text-2xs text-surface-400">Token 数</p><p class="font-mono text-sm font-semibold text-surface-700">{{ (limitUsage.current_total_tokens ?? 0)?.toLocaleString() }}</p></div>
              <div><p class="text-2xs text-surface-400">费用</p><p class="font-mono text-sm font-semibold text-surface-700">${{ limitUsage.current_cost ?? 0 }}</p></div>
            </div>
          </div>

          <div class="flex justify-between pt-3">
            <button class="btn btn-secondary btn-sm text-red-600" @click="handleDeleteLimit(limitDetail.usage_limit_id); limitDrawerOpen = false">删除</button>
            <button class="btn btn-secondary btn-sm" @click="limitDrawerOpen = false">关闭</button>
          </div>
        </div>
        <div v-else class="py-12 text-center text-sm text-surface-400"><LoadingSkeleton variant="detail" /></div>
      </DetailDrawer>
    </div>
  </div>
</template>
