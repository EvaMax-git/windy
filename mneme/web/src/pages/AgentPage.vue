<script setup lang="ts">
import { ref, computed } from "vue";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import PageHeader from "@/components/PageHeader.vue";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import CardTab from "@/pages/tabs/CardTab.vue";
import ContextAssemblyTab from "@/pages/tabs/ContextAssemblyTab.vue";
import LogsTab from "@/pages/tabs/LogsTab.vue";
import {
  fetchAgents,
  fetchAgent,
  fetchAgentTokens,
  createAgent,
  createAgentToken,
  revokeAgentToken,
  updateAgent,
  disableAgent,
  archiveAgent,
  fetchAuditEvents,
  fetchContextPacks,
  fetchContextPack,
  fetchMemoryStores,
  fetchMemoryStoreByAgent,
  fetchProjects,
} from "@/api/client";
import type {
  AgentRead,
  AgentCreateRequest,
  AgentTokenRead,
  AuditEvent,
  ContextPackSummary,
  ContextPackDetail,
  MemoryStoreRead,
  ProjectRead,
} from "@/types";

type Tab = "list" | "cards" | "context" | "logs";
const activeTab = ref<Tab>("list");

const tabs: { key: Tab; label: string; icon: string }[] = [
  { key: "list", label: "Agent 列表", icon: "🤖" },
  { key: "cards", label: "卡牌管理", icon: "🃏" },
  { key: "context", label: "上下文组装", icon: "🧠" },
  { key: "logs", label: "日志", icon: "📋" },
];

// ── View toggle (list tab) ──
const activeView = ref<"table" | "cards">("table");

// ── List state ──
const page = ref(1);
const pageSize = ref(50);
const filterStatus = ref("");

const listKey = computed(() => ["agents", page.value, pageSize.value, filterStatus.value] as const);

const { data, isLoading, isError, error } = useQuery({
  queryKey: listKey,
  queryFn: () => fetchAgents({ page: page.value, page_size: pageSize.value, status: filterStatus.value || undefined }),
  placeholderData: (prev) => prev,
});

// ── Create Agent form ──
const showCreateForm = ref(false);
const createForm = ref({
  agent_code: "",
  name: "",
  description: "",
  project_id: null as string | null,
  store_id: null as string | null,
  sensitivity_ceiling: "normal" as string,
  policy_json_str: "{}",
});
const creating = ref(false);
const createError = ref<string | null>(null);

const { data: projectsData } = useQuery({
  queryKey: ["projects-dropdown"],
  queryFn: () => fetchProjects({ page: 1, page_size: 200 }),
  enabled: computed(() => showCreateForm.value),
});

const { data: allStores } = useQuery({
  queryKey: ["memory-stores-dropdown"],
  queryFn: () => fetchMemoryStores(),
  enabled: computed(() => showCreateForm.value),
});

async function handleCreateAgent() {
  if (!createForm.value.agent_code.trim() || !createForm.value.name.trim()) return;
  creating.value = true;
  createError.value = null;

  let policyJson: Record<string, unknown>;
  try { policyJson = JSON.parse(createForm.value.policy_json_str || "{}"); } catch {
    createError.value = "Policy JSON 格式无效"; creating.value = false; return;
  }

  try {
    const payload: AgentCreateRequest = {
      agent_code: createForm.value.agent_code.trim(),
      name: createForm.value.name.trim(),
      description: createForm.value.description.trim() || null,
      project_id: createForm.value.project_id || null,
      store_id: createForm.value.store_id || null,
      sensitivity_ceiling: createForm.value.sensitivity_ceiling as AgentCreateRequest["sensitivity_ceiling"],
      policy_json: policyJson,
    };
    await createAgent(payload);
    showCreateForm.value = false;
    createForm.value = { agent_code: "", name: "", description: "", project_id: null, store_id: null, sensitivity_ceiling: "normal", policy_json_str: "{}" };
    queryClient.invalidateQueries({ queryKey: ["agents"] });
  } catch (e) {
    createError.value = (e as Error)?.message || "创建 Agent 失败";
  } finally {
    creating.value = false;
  }
}

function openCreateForm() {
  createForm.value = { agent_code: "", name: "", description: "", project_id: null, store_id: null, sensitivity_ceiling: "normal", policy_json_str: "{}" };
  createError.value = null;
  showCreateForm.value = true;
}

// ── Detail drawer ──
const selectedAgentId = ref<string | null>(null);
const drawerTab = ref<"info" | "tokens" | "activity" | "context">("info");
const drawerOpen = ref(false);
const tokenSubmitting = ref(false);
const newTokenForm = ref({ name: "", scopes: "", expires_in_days: 90 });
const newTokenRaw = ref<string | null>(null);
const agentSaving = ref(false);
const lifecycleSubmitting = ref(false);
const editMode = ref(false);
const editForm = ref({ name: "", description: "", sensitivity_ceiling: "normal", policy_json: "{}", store_id: null as string | null });

const { data: agentDetail, isLoading: detailLoading } = useQuery({
  queryKey: ["agent", selectedAgentId],
  queryFn: () => fetchAgent(selectedAgentId.value!),
  enabled: computed(() => drawerOpen.value && !!selectedAgentId.value),
});

const { data: tokenList } = useQuery({
  queryKey: ["agent-tokens", selectedAgentId],
  queryFn: () => fetchAgentTokens(selectedAgentId.value!),
  enabled: computed(() => drawerOpen.value && !!selectedAgentId.value),
});

const { data: activityData } = useQuery({
  queryKey: ["agent-activity", selectedAgentId],
  queryFn: () => fetchAuditEvents({ actor_type: "agent", page: 1, page_size: 30 }),
  enabled: computed(() => drawerOpen.value && drawerTab.value === "activity" && !!selectedAgentId.value),
});

const filteredActivity = computed(() => {
  if (!activityData.value?.items || !selectedAgentId.value) return [];
  return activityData.value.items.filter((e: AuditEvent) => e.actor?.actor_id === selectedAgentId.value);
});

const { data: contextPacksData } = useQuery({
  queryKey: ["agent-context-packs", selectedAgentId],
  queryFn: () => fetchContextPacks({ page: 1, page_size: 50 }),
  enabled: computed(() => drawerOpen.value && drawerTab.value === "context" && !!selectedAgentId.value),
});

const filteredContextPacks = computed(() => contextPacksData.value?.items ?? []);

const selectedPackId = ref<string | null>(null);
const { data: packDetail } = useQuery({
  queryKey: ["context-pack", selectedPackId],
  queryFn: () => fetchContextPack(selectedPackId.value!),
  enabled: computed(() => !!selectedPackId.value),
});

const queryClient = useQueryClient();

const { data: agentStore } = useQuery({
  queryKey: ["agent-store", selectedAgentId],
  queryFn: () => fetchMemoryStoreByAgent(selectedAgentId.value!),
  enabled: computed(() => drawerOpen.value && !!selectedAgentId.value),
});

const tokenItems = computed<AgentTokenRead[]>(() => {
  if (!tokenList.value) return [];
  return (tokenList.value as { items: AgentTokenRead[] }).items ?? [];
});

const columns = [
  { key: "name", label: "名称", width: "200px" },
  { key: "status", label: "状态", width: "100px" },
  { key: "token_count", label: "Token 数", width: "90px" },
  { key: "scopes", label: "权限范围", width: "200px" },
  { key: "description", label: "描述", width: "200px" },
  { key: "last_seen_at", label: "最后活跃", width: "160px" },
  { key: "created_at", label: "创建时间", width: "160px" },
];

function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function openDetail(agent: AgentRead) {
  selectedAgentId.value = agent.agent_id;
  drawerTab.value = "info";
  drawerOpen.value = true;
  newTokenRaw.value = null;
  editMode.value = false;
}

function startEditAgent() {
  if (!agentDetail.value) return;
  editForm.value = {
    name: agentDetail.value.name ?? "",
    description: agentDetail.value.description ?? "",
    sensitivity_ceiling: agentDetail.value.sensitivity_ceiling ?? "normal",
    policy_json: JSON.stringify(agentDetail.value.policy_json ?? {}, null, 2),
    store_id: agentDetail.value.store_id ?? null,
  };
  editMode.value = true;
}

async function handleUpdateAgent() {
  if (!selectedAgentId.value || !editForm.value.name.trim()) return;
  let policyJson: Record<string, unknown>;
  try { policyJson = JSON.parse(editForm.value.policy_json || "{}"); } catch { return; }
  agentSaving.value = true;
  try {
    await updateAgent(selectedAgentId.value, {
      name: editForm.value.name.trim(),
      description: editForm.value.description.trim() || null,
      sensitivity_ceiling: editForm.value.sensitivity_ceiling as AgentRead["sensitivity_ceiling"],
      policy_json: policyJson,
      store_id: editForm.value.store_id || null,
    });
    editMode.value = false;
    queryClient.invalidateQueries({ queryKey: ["agent", selectedAgentId.value] });
    queryClient.invalidateQueries({ queryKey: ["agents"] });
  } catch (e) { console.error("Failed to update agent", e); } finally { agentSaving.value = false; }
}

async function handleDisableAgent() {
  if (!selectedAgentId.value || !confirm("Disable this agent?")) return;
  lifecycleSubmitting.value = true;
  try {
    await disableAgent(selectedAgentId.value);
    queryClient.invalidateQueries({ queryKey: ["agent", selectedAgentId.value] });
    queryClient.invalidateQueries({ queryKey: ["agents"] });
  } catch (e) { console.error("Failed to disable agent", e); } finally { lifecycleSubmitting.value = false; }
}

async function handleArchiveAgent() {
  if (!selectedAgentId.value || !confirm("Archive this agent?")) return;
  lifecycleSubmitting.value = true;
  try {
    await archiveAgent(selectedAgentId.value);
    queryClient.invalidateQueries({ queryKey: ["agent", selectedAgentId.value] });
    queryClient.invalidateQueries({ queryKey: ["agents"] });
  } catch (e) { console.error("Failed to archive agent", e); } finally { lifecycleSubmitting.value = false; }
}

async function handleCreateToken() {
  if (!selectedAgentId.value || !newTokenForm.value.name.trim()) return;
  tokenSubmitting.value = true;
  try {
    const scopes = newTokenForm.value.scopes.split(",").map(s => s.trim()).filter(Boolean);
    const result = await createAgentToken(selectedAgentId.value, {
      name: newTokenForm.value.name.trim(),
      scopes: scopes.length > 0 ? scopes : undefined,
      expires_in_days: newTokenForm.value.expires_in_days,
    });
    newTokenRaw.value = result.token_raw;
    newTokenForm.value = { name: "", scopes: "", expires_in_days: 90 };
    queryClient.invalidateQueries({ queryKey: ["agent-tokens", selectedAgentId.value] });
    queryClient.invalidateQueries({ queryKey: ["agent", selectedAgentId.value] });
    queryClient.invalidateQueries({ queryKey: ["agents"] });
  } catch (e) { console.error("Failed to create token", e); } finally { tokenSubmitting.value = false; }
}

async function handleRevokeToken(tokenId: string) {
  if (!selectedAgentId.value || !confirm("确定要吊销此 Token？")) return;
  try {
    await revokeAgentToken(selectedAgentId.value, tokenId);
    queryClient.invalidateQueries({ queryKey: ["agent-tokens", selectedAgentId.value] });
    queryClient.invalidateQueries({ queryKey: ["agent", selectedAgentId.value] });
    queryClient.invalidateQueries({ queryKey: ["agents"] });
  } catch (e) { console.error("Failed to revoke token", e); }
}

function closeDrawer() {
  drawerOpen.value = false;
  newTokenRaw.value = null;
  selectedPackId.value = null;
}

function copyTokenToClipboard() {
  if (newTokenRaw.value) navigator.clipboard.writeText(newTokenRaw.value);
}

const STATUS_LABEL_MAP: Record<string, string> = {
  active: "活跃", disabled: "已禁用", archived: "已归档", inactive: "未激活", suspended: "已挂起", revoked: "已吊销",
};

function activityResultColor(result: string): string {
  switch (result) { case "success": return "bg-green-50 text-green-700"; case "denied": return "bg-red-50 text-red-700"; case "failed": return "bg-red-50 text-red-700"; default: return "bg-surface-100 text-surface-500"; }
}
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader title="Agent 中心" subtitle="Agent 注册与生命周期 · 卡牌管理 · 上下文组装 · API 调用日志" />

    <!-- Tab bar -->
    <div class="flex flex-wrap border-b border-surface-200 mb-6 -mx-4 px-4 gap-0">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        @click="activeTab = tab.key"
        :class="[
          'pb-3 px-3 text-sm font-medium border-b-2 transition-colors whitespace-nowrap',
          activeTab === tab.key ? 'border-brand-500 text-brand-600' : 'border-transparent text-surface-400 hover:text-surface-600',
        ]"
      >
        <span class="mr-1.5">{{ tab.icon }}</span>{{ tab.label }}
      </button>
    </div>

    <!-- ═══════════════════════════════════════════════════════════════ -->
    <!-- Tab 1: Agent List -->
    <!-- ═══════════════════════════════════════════════════════════════ -->
    <template v-if="activeTab === 'list'">
      <!-- Filters & View Toggle -->
      <div class="card mb-6 p-4">
        <div class="flex flex-wrap items-center gap-3">
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-medium text-surface-400">状态</label>
            <select v-model="filterStatus" class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500">
              <option value="">全部状态</option>
              <option value="active">活跃</option>
              <option value="disabled">已禁用</option>
              <option value="archived">已归档</option>
            </select>
          </div>

          <span class="self-end text-xs text-surface-400 ml-auto">{{ data?.page_info?.total_items ?? 0 }} 个 Agent</span>

          <!-- Create button -->
          <button class="btn btn-primary btn-sm self-end" @click="openCreateForm">+ 新建 Agent</button>

          <!-- View toggle -->
          <div class="flex rounded-lg border border-surface-200 overflow-hidden self-end">
            <button @click="activeView = 'table'" :class="['px-3 py-1.5 text-xs font-medium transition-colors', activeView === 'table' ? 'bg-brand-500 text-white' : 'bg-white text-surface-500 hover:bg-surface-100']">📋 表格</button>
            <button @click="activeView = 'cards'" :class="['px-3 py-1.5 text-xs font-medium transition-colors', activeView === 'cards' ? 'bg-brand-500 text-white' : 'bg-white text-surface-500 hover:bg-surface-100']">🃏 卡片</button>
          </div>
        </div>
      </div>

      <!-- Error -->
      <div v-if="isError" class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700">加载 Agent 失败: {{ (error as Error)?.message }}</div>

      <!-- ═══ Table View ═══ -->
      <template v-if="activeView === 'table'">
        <DataTable :items="data?.items ?? []" :columns="columns" :loading="isLoading" empty-message="暂无 Agent" clickable row-key="agent_id" @row-click="openDetail">
          <template #cell-name="{ value, item }">
            <div class="flex items-center gap-2 min-w-0">
              <span class="shrink-0 text-xs text-surface-400">🤖</span>
              <div class="min-w-0">
                <p class="truncate text-sm font-medium text-surface-800">{{ value }}</p>
                <p class="text-2xs text-surface-400 truncate font-mono">{{ (item as AgentRead).agent_id?.slice(0, 12) }}…</p>
              </div>
            </div>
          </template>
          <template #cell-status="{ value }"><StatusBadge :status="value as string" /></template>
          <template #cell-token_count="{ value }"><span class="font-mono text-xs text-surface-500">{{ value ?? 0 }}</span></template>
          <template #cell-scopes="{ value }">
            <div class="flex flex-wrap gap-1">
              <span v-for="scope in ((value as string[]) ?? [])" :key="scope" class="badge text-2xs bg-surface-100 text-surface-600">{{ scope }}</span>
              <span v-if="!value || (value as string[]).length === 0" class="text-xs text-surface-400">—</span>
            </div>
          </template>
          <template #cell-description="{ value }"><span class="text-xs text-surface-500 truncate max-w-[200px] inline-block">{{ value || '—' }}</span></template>
          <template #cell-last_seen_at="{ value }"><span class="font-mono text-xs text-surface-500">{{ formatTime(value as string | null) }}</span></template>
          <template #cell-created_at="{ value }"><span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span></template>
        </DataTable>
      </template>

      <!-- ═══ Card View ═══ -->
      <template v-else>
        <div v-if="isLoading" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 mb-6">
          <div v-for="i in 6" :key="i" class="card animate-pulse p-5"><div class="h-4 bg-surface-200 rounded w-3/4 mb-3"></div><div class="h-3 bg-surface-100 rounded w-full mb-2"></div><div class="h-3 bg-surface-100 rounded w-1/2 mb-4"></div><div class="h-5 bg-surface-200 rounded w-16"></div></div>
        </div>
        <div v-else class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 mb-6">
          <div v-for="agent in (data?.items ?? [])" :key="agent.agent_id" class="card p-5 hover:shadow-md hover:-translate-y-0.5 transition-all cursor-pointer group" @click="openDetail(agent)">
            <div class="flex items-start justify-between mb-3">
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2"><span class="shrink-0 text-lg">🤖</span><h3 class="text-sm font-semibold text-surface-800 truncate group-hover:text-brand-600 transition-colors">{{ agent.name }}</h3></div>
                <p class="font-mono text-2xs text-surface-400 mt-0.5">{{ agent.agent_id?.slice(0, 12) }}…</p>
              </div>
              <StatusBadge :status="agent.status" />
            </div>
            <p v-if="agent.description" class="text-xs text-surface-500 line-clamp-2 mb-3">{{ agent.description }}</p>
            <div class="flex items-center justify-between text-2xs text-surface-400">
              <span class="flex items-center gap-1">🔑 <span class="font-mono">{{ agent.token_count ?? 0 }}</span></span>
              <span class="flex items-center gap-1">🕐 {{ formatTime(agent.last_seen_at)?.slice(0, 10) }}</span>
            </div>
            <div v-if="(agent.scopes ?? []).length > 0" class="flex flex-wrap gap-1 mt-3 pt-3 border-t border-surface-100">
              <span v-for="scope in (agent.scopes ?? [])" :key="scope" class="badge text-2xs bg-surface-100 text-surface-600">{{ scope }}</span>
            </div>
          </div>
          <div v-if="(data?.items ?? []).length === 0" class="col-span-full py-16 text-center text-sm text-surface-400">
            <div class="flex flex-col items-center gap-2"><span class="text-4xl">🤖</span><span>暂无 Agent</span><button class="btn btn-primary btn-sm mt-2" @click="openCreateForm">创建第一个 Agent</button></div>
          </div>
        </div>
      </template>

      <Pagination v-if="data?.page_info" :page-info="data.page_info" @page-change="(p: number) => (page = p)" />
    </template>

    <!-- ═══════════════════════════════════════════════════════════════ -->
    <!-- Tab 2: Card Management -->
    <!-- ═══════════════════════════════════════════════════════════════ -->
    <CardTab v-if="activeTab === 'cards'" />

    <!-- ═══════════════════════════════════════════════════════════════ -->
    <!-- Tab 3: Context Assembly -->
    <!-- ═══════════════════════════════════════════════════════════════ -->
    <ContextAssemblyTab v-if="activeTab === 'context'" />

    <!-- ═══════════════════════════════════════════════════════════════ -->
    <!-- Tab 4: Logs -->
    <!-- ═══════════════════════════════════════════════════════════════ -->
    <LogsTab v-if="activeTab === 'logs'" />

    <!-- ═══════════════════════════════════════════════════════════════ -->
    <!-- Create Agent Modal -->
    <!-- ═══════════════════════════════════════════════════════════════ -->
    <Teleport to="body">
      <Transition name="modal">
        <div v-if="showCreateForm" class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4" @click.self="showCreateForm = false">
          <div class="bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto" @click.stop>
            <div class="px-6 py-4 border-b border-surface-200 flex items-center justify-between">
              <h3 class="text-lg font-semibold text-surface-800">新建 Agent</h3>
              <button class="rounded-lg p-1.5 text-surface-400 hover:bg-surface-100 hover:text-surface-600" @click="showCreateForm = false">
                <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
              </button>
            </div>

            <div class="px-6 py-4 space-y-4">
              <div v-if="createError" class="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">{{ createError }}</div>

              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">Agent Code <span class="text-red-400">*</span></label>
                <input v-model="createForm.agent_code" type="text" placeholder="例如: customer_service_bot" class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm font-mono text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
              </div>

              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">名称 <span class="text-red-400">*</span></label>
                <input v-model="createForm.name" type="text" placeholder="例如: 客服 Agent" class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
              </div>

              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">描述</label>
                <textarea v-model="createForm.description" rows="2" placeholder="Agent 用途说明..." class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
              </div>

              <div class="grid grid-cols-2 gap-3">
                <div class="flex flex-col gap-1">
                  <label class="text-2xs font-semibold uppercase text-surface-400">项目</label>
                  <select v-model="createForm.project_id" class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500">
                    <option :value="null">— 不绑定 —</option>
                    <option v-for="p in (projectsData?.items ?? [])" :key="p.project_id" :value="p.project_id">{{ p.name }}</option>
                  </select>
                </div>
                <div class="flex flex-col gap-1">
                  <label class="text-2xs font-semibold uppercase text-surface-400">记忆子库</label>
                  <select v-model="createForm.store_id" class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500">
                    <option :value="null">— 不绑定 —</option>
                    <option v-for="s in (allStores ?? [])" :key="s.store_id" :value="s.store_id">{{ s.name }} ({{ s.type }})</option>
                  </select>
                </div>
              </div>

              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">敏感度上限</label>
                <select v-model="createForm.sensitivity_ceiling" class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500">
                  <option value="public">public</option>
                  <option value="normal">normal</option>
                  <option value="private">private</option>
                  <option value="sensitive">sensitive</option>
                  <option value="secret">secret</option>
                </select>
              </div>

              <div class="flex flex-col gap-1">
                <label class="text-2xs font-semibold uppercase text-surface-400">Policy JSON</label>
                <textarea v-model="createForm.policy_json_str" rows="4" class="rounded-lg border border-surface-200 bg-white px-3 py-2 font-mono text-xs text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" placeholder='{"key": "value"}' />
              </div>
            </div>

            <div class="px-6 py-4 border-t border-surface-200 bg-surface-50 rounded-b-2xl flex items-center justify-end gap-2">
              <button class="btn btn-secondary btn-sm" @click="showCreateForm = false">取消</button>
              <button class="btn btn-primary btn-sm" :disabled="creating || !createForm.agent_code.trim() || !createForm.name.trim()" @click="handleCreateAgent">
                {{ creating ? "创建中..." : "创建 Agent" }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- ═══════════════════════════════════════════════════════════════ -->
    <!-- Detail Drawer -->
    <!-- ═══════════════════════════════════════════════════════════════ -->
    <DetailDrawer :open="drawerOpen" title="Agent 详情" width="w-[580px] max-w-full" @close="closeDrawer">
      <div v-if="detailLoading" class="py-4"><LoadingSkeleton variant="detail" /></div>

      <template v-else-if="agentDetail">
        <div class="flex items-center gap-1 border-b border-surface-200 mb-4 overflow-x-auto">
          <button v-for="dt in ([
            { key: 'info' as const, label: '📋 基本信息' },
            { key: 'tokens' as const, label: '🔑 Token (' + (agentDetail.token_count ?? 0) + ')' },
            { key: 'activity' as const, label: '📊 Activity' },
            { key: 'context' as const, label: '📦 Context' },
          ])" :key="dt.key" :class="['shrink-0 px-3 py-2 text-sm font-medium border-b-2 transition-colors -mb-px', drawerTab === dt.key ? 'border-brand-500 text-brand-600' : 'border-transparent text-surface-500 hover:text-surface-700']" @click="drawerTab = dt.key; if(dt.key === 'tokens') newTokenRaw = null; if(dt.key === 'context') selectedPackId = null">
            {{ dt.label }}
          </button>
        </div>

        <!-- Info Tab -->
        <div v-if="drawerTab === 'info'">
          <div class="mb-4 flex flex-wrap items-center gap-2">
            <button class="btn btn-secondary btn-sm" :disabled="agentDetail.status === 'archived'" @click="startEditAgent">编辑</button>
            <button v-if="agentDetail.status === 'active'" class="btn btn-secondary btn-sm text-amber-700" :disabled="lifecycleSubmitting" @click="handleDisableAgent">禁用</button>
            <button v-if="agentDetail.status !== 'archived'" class="btn btn-secondary btn-sm text-red-700" :disabled="lifecycleSubmitting" @click="handleArchiveAgent">归档</button>
          </div>

          <div v-if="editMode" class="mb-4 rounded-lg border border-surface-200 bg-surface-50 p-4">
            <div class="space-y-3">
              <input v-model="editForm.name" type="text" class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" placeholder="Agent 名称" />
              <textarea v-model="editForm.description" rows="2" class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" placeholder="描述" />
              <select v-model="editForm.sensitivity_ceiling" class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500">
                <option value="public">public</option><option value="normal">normal</option><option value="private">private</option><option value="sensitive">sensitive</option><option value="secret">secret</option>
              </select>
              <div class="flex flex-col gap-1">
                <label class="text-2xs font-medium text-surface-400">绑定记忆子库</label>
                <select v-model="editForm.store_id" class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500">
                  <option :value="null">— 不绑定 —</option>
                  <option v-for="s in (allStores ?? [])" :key="s.store_id" :value="s.store_id">{{ s.name }} ({{ s.type }})</option>
                </select>
              </div>
              <textarea v-model="editForm.policy_json" rows="5" class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 font-mono text-xs text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" placeholder="Policy JSON" />
              <div class="flex justify-end gap-2">
                <button class="btn btn-secondary btn-sm" @click="editMode = false">取消</button>
                <button class="btn btn-primary btn-sm" :disabled="agentSaving || !editForm.name.trim()" @click="handleUpdateAgent">{{ agentSaving ? "保存中..." : "保存" }}</button>
              </div>
            </div>
          </div>

          <dl class="space-y-4">
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">Agent ID</dt><dd class="mt-1 font-mono text-xs text-surface-700 break-all">{{ agentDetail.agent_id }}</dd></div>
            <div class="grid grid-cols-2 gap-4">
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">名称</dt><dd class="mt-1 text-sm text-surface-700">{{ agentDetail.name }}</dd></div>
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">状态</dt><dd class="mt-1"><StatusBadge :status="agentDetail.status" /><span class="ml-2 text-2xs text-surface-400">{{ STATUS_LABEL_MAP[agentDetail.status] || agentDetail.status }}</span></dd></div>
            </div>
            <div v-if="agentDetail.description"><dt class="text-2xs font-semibold uppercase text-surface-400">描述</dt><dd class="mt-1 text-sm text-surface-700">{{ agentDetail.description }}</dd></div>
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">权限范围</dt><dd class="mt-1 flex flex-wrap gap-1.5"><span v-for="scope in (agentDetail.scopes ?? [])" :key="scope" class="badge text-2xs bg-indigo-50 text-indigo-700">{{ scope }}</span><span v-if="(agentDetail.scopes ?? []).length === 0" class="text-xs text-surface-400">无</span></dd></div>
            <div class="grid grid-cols-2 gap-4">
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">Token 数</dt><dd class="mt-1 font-mono text-sm text-surface-700">{{ agentDetail.token_count ?? 0 }}</dd></div>
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">创建者</dt><dd class="mt-1 font-mono text-xs text-surface-500">{{ agentDetail.created_by_user_id ? agentDetail.created_by_user_id.slice(0, 12) + '…' : '—' }}</dd></div>
            </div>
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">绑定记忆子库</dt><dd class="mt-1 text-sm text-surface-700"><span v-if="agentStore" class="badge bg-indigo-50 text-indigo-700 text-xs">📦 {{ agentStore.name }} ({{ agentStore.type }})</span><span v-else class="text-xs text-surface-400">未绑定</span></dd></div>
            <div class="grid grid-cols-2 gap-4">
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">最后活跃</dt><dd class="mt-1 text-xs">{{ formatTime(agentDetail.last_seen_at) }}</dd></div>
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt><dd class="mt-1 text-xs">{{ formatTime(agentDetail.created_at) }}</dd></div>
            </div>
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">更新时间</dt><dd class="mt-1 text-xs">{{ formatTime(agentDetail.updated_at) }}</dd></div>
          </dl>
        </div>

        <!-- Tokens Tab -->
        <div v-if="drawerTab === 'tokens'">
          <div v-if="newTokenRaw" class="rounded-lg border border-amber-200 bg-amber-50 p-4 mb-4">
            <p class="text-xs font-semibold text-amber-800 mb-2">⚠️ 请立即复制 Token，此信息仅显示一次</p>
            <div class="flex items-center gap-2"><code class="flex-1 break-all rounded bg-amber-100 px-3 py-2 text-xs font-mono text-amber-900">{{ newTokenRaw }}</code><button class="btn btn-secondary btn-sm shrink-0" @click="copyTokenToClipboard">复制</button></div>
          </div>
          <div class="rounded-lg border border-surface-200 bg-surface-50 p-4 mb-4">
            <p class="text-xs font-semibold text-surface-700 mb-3">创建新 Token</p>
            <div class="space-y-3">
              <div class="grid grid-cols-2 gap-2">
                <input v-model="newTokenForm.name" type="text" placeholder="Token 名称" class="col-span-1 rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                <input v-model="newTokenForm.scopes" type="text" placeholder="权限 (逗号分隔)" class="col-span-1 rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
              </div>
              <div class="flex items-center gap-2">
                <div class="flex flex-col gap-1 flex-1"><label class="text-2xs font-medium text-surface-400">有效期 (天)</label><input v-model.number="newTokenForm.expires_in_days" type="number" min="1" max="3650" class="w-32 rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" /></div>
              </div>
              <button class="btn btn-primary btn-sm w-full" :disabled="tokenSubmitting || !newTokenForm.name.trim()" @click="handleCreateToken">{{ tokenSubmitting ? '创建中...' : '创建 Token' }}</button>
            </div>
          </div>
          <div v-if="tokenItems.length > 0" class="divide-y divide-surface-100 rounded-lg border border-surface-200 overflow-hidden">
            <div v-for="tok in tokenItems" :key="tok.token_id" class="flex items-start gap-3 px-4 py-3 hover:bg-surface-50 transition-colors">
              <span class="shrink-0 inline-flex items-center justify-center h-6 w-6 rounded text-2xs font-mono font-bold bg-surface-100 text-surface-500">🔑</span>
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2"><p class="text-sm font-medium text-surface-700">{{ tok.name }}</p><span class="badge text-2xs bg-surface-100 text-surface-500 font-mono">{{ tok.token_prefix }}…</span></div>
                <div class="mt-0.5 flex flex-wrap gap-1"><span v-for="scope in (tok.scopes ?? tok.capability_scope ?? [])" :key="scope" class="badge text-2xs bg-indigo-50 text-indigo-600">{{ scope }}</span></div>
                <p class="mt-1 text-2xs text-surface-400">创建于 {{ formatTime(tok.created_at) }}<span v-if="tok.expires_at"> · 过期于 {{ formatTime(tok.expires_at) }}</span><span v-if="tok.last_used_at"> · 最后使用 {{ formatTime(tok.last_used_at) }}</span></p>
              </div>
              <button class="shrink-0 rounded p-1 text-surface-400 hover:bg-red-50 hover:text-red-500 transition-colors" title="吊销 Token" @click="handleRevokeToken(tok.token_id)"><svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636"/></svg></button>
            </div>
          </div>
          <div v-else-if="tokenList" class="flex flex-col items-center justify-center py-8 text-sm text-surface-400"><span class="text-2xl mb-2">🔑</span>暂无 Token</div>
        </div>

        <!-- Activity Tab -->
        <div v-if="drawerTab === 'activity'">
          <div v-if="filteredActivity.length > 0" class="space-y-2">
            <p class="text-xs text-surface-400 mb-3">最近审计事件 ({{ filteredActivity.length }})</p>
            <div v-for="ev in filteredActivity.slice(0, 25)" :key="ev.audit_id" class="flex items-start gap-3 px-3 py-2 rounded-lg hover:bg-surface-50 transition-colors">
              <span class="shrink-0 mt-0.5 text-xs">{{ ev.action?.includes('token') ? '🔑' : ev.action?.includes('created') ? '✨' : ev.action?.includes('updated') ? '✏️' : ev.action?.includes('disabled') ? '⛔' : '📌' }}</span>
              <div class="min-w-0 flex-1"><div class="flex items-center gap-2"><span class="text-xs font-medium text-surface-700">{{ ev.action }}</span><span :class="['badge text-2xs', activityResultColor(ev.result)]">{{ ev.result }}</span></div><p class="text-2xs text-surface-400 mt-0.5">{{ formatTime(ev.occurred_at) }}<span v-if="ev.object_type" class="ml-1">· {{ ev.object_type }} {{ ev.object_id?.slice(0, 8) }}…</span></p></div>
            </div>
          </div>
          <div v-else class="flex flex-col items-center justify-center py-10 text-sm text-surface-400"><span class="text-2xl mb-2">📊</span>暂无 Activity 记录</div>
        </div>

        <!-- Context Tab -->
        <div v-if="drawerTab === 'context'">
          <template v-if="selectedPackId && packDetail">
            <button @click="selectedPackId = null" class="flex items-center gap-1 text-sm text-brand-600 hover:text-brand-700 mb-4">← 返回列表</button>
            <dl class="space-y-3">
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">Pack 名称</dt><dd class="mt-1 text-sm font-medium text-surface-800">{{ packDetail.name }}</dd></div>
              <div v-if="packDetail.description"><dt class="text-2xs font-semibold uppercase text-surface-400">描述</dt><dd class="mt-1 text-xs text-surface-600">{{ packDetail.description }}</dd></div>
              <div class="grid grid-cols-2 gap-4"><div><dt class="text-2xs font-semibold uppercase text-surface-400">记忆数</dt><dd class="mt-1 font-mono text-sm">{{ packDetail.memory_count }}</dd></div><div><dt class="text-2xs font-semibold uppercase text-surface-400">文档数</dt><dd class="mt-1 font-mono text-sm">{{ packDetail.document_count }}</dd></div></div>
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">Pack ID</dt><dd class="mt-1 font-mono text-2xs break-all">{{ packDetail.pack_id }}</dd></div>
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt><dd class="mt-1 text-xs">{{ formatTime(packDetail.created_at) }}</dd></div>
            </dl>
          </template>
          <template v-else>
            <div v-if="filteredContextPacks.length > 0" class="space-y-2">
              <p class="text-xs text-surface-400 mb-3">关联的 Context Pack ({{ filteredContextPacks.length }})</p>
              <div v-for="pack in filteredContextPacks" :key="pack.pack_id" @click="selectedPackId = pack.pack_id" class="flex items-start gap-3 px-3 py-3 rounded-lg border border-surface-200 hover:border-brand-300 hover:bg-brand-50 cursor-pointer transition-colors">
                <span class="shrink-0 text-lg">📦</span>
                <div class="min-w-0 flex-1"><p class="text-sm font-medium text-surface-700">{{ pack.name }}</p><p v-if="pack.description" class="text-2xs text-surface-400 mt-0.5 line-clamp-1">{{ pack.description }}</p><div class="flex items-center gap-3 mt-1.5 text-2xs text-surface-400"><span>📝 {{ pack.memory_count }} 记忆</span><span>📄 {{ pack.document_count }} 文档</span><span class="ml-auto">{{ formatTime(pack.created_at)?.slice(0, 10) }}</span></div></div>
                <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4 text-surface-300 shrink-0 mt-1" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7"/></svg>
              </div>
            </div>
            <div v-else class="flex flex-col items-center justify-center py-10 text-sm text-surface-400"><span class="text-2xl mb-2">📦</span>暂无 Context Pack</div>
          </template>
        </div>
      </template>

      <div v-else class="flex flex-col items-center justify-center py-12 text-sm text-surface-400">加载 Agent 详情失败</div>

      <template #footer>
        <div class="flex items-center justify-between">
          <span class="text-2xs text-surface-400">ID: {{ selectedAgentId ? selectedAgentId.slice(0, 8) + '...' : '—' }}</span>
          <button class="btn btn-secondary btn-sm" @click="closeDrawer">关闭</button>
        </div>
      </template>
    </DetailDrawer>
  </div>
</template>

<style scoped>
.modal-enter-active,
.modal-leave-active { transition: opacity 0.2s ease; }
.modal-enter-active > div,
.modal-leave-active > div { transition: transform 0.2s ease, opacity 0.2s ease; }
.modal-enter-from,
.modal-leave-to { opacity: 0; }
.modal-enter-from > div { transform: scale(0.95) translateY(10px); opacity: 0; }
.modal-leave-to > div { transform: scale(0.95) translateY(10px); opacity: 0; }
</style>
