<script setup lang="ts">
import { ref, computed } from "vue";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import {
  fetchMemoryStores,
  fetchMemoryStore,
  createMemoryStore,
  updateMemoryStore,
  deleteMemoryStore,
  bindStoreToAgent,
  unbindStore,
  fetchAgents,
} from "@/api/client";
import type { MemoryStoreRead, MemoryStoreType, AgentRead } from "@/types";
import { MEMORY_STORE_TYPE_LABELS } from "@/types";

const queryClient = useQueryClient();

// ── List state ──
const filterAgentId = ref("");

const { data: stores, isLoading, isError, error } = useQuery({
  queryKey: ["memory-stores", filterAgentId],
  queryFn: () =>
    fetchMemoryStores({
      agent_id: filterAgentId.value || undefined,
    }),
});

// ── Agents for binding ──
const { data: agentsData } = useQuery({
  queryKey: ["agents-for-store"],
  queryFn: () => fetchAgents({ page: 1, page_size: 200 }),
});

const agents = computed<AgentRead[]>(() => (agentsData.value?.items ?? []) as unknown as AgentRead[]);

// ── Create form ──
const showCreateForm = ref(false);
const createForm = ref({
  name: "",
  type: "memory_card" as MemoryStoreType,
  description: "",
  agent_id: null as string | null,
});
const creating = ref(false);

// ── Edit form ──
const editingStoreId = ref<string | null>(null);
const editForm = ref({
  name: "",
  type: "memory_card" as MemoryStoreType,
  description: "",
  agent_id: null as string | null,
});
const saving = ref(false);

// ── Confirm delete ──
const confirmDeleteId = ref<string | null>(null);

// ── Helpers ──
const TYPE_OPTIONS: { value: MemoryStoreType; label: string }[] = [
  { value: "memory_card", label: "知识卡片" },
  { value: "identity", label: "身份" },
  { value: "skill", label: "技能" },
  { value: "rule", label: "规则" },
  { value: "tool", label: "工具" },
];

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

function getTypeLabel(type: string): string {
  return MEMORY_STORE_TYPE_LABELS[type as MemoryStoreType] || type;
}

function getTypeColor(type: string): string {
  const colors: Record<string, string> = {
    memory_card: "bg-blue-50 text-blue-700 border-blue-200",
    identity: "bg-purple-50 text-purple-700 border-purple-200",
    skill: "bg-amber-50 text-amber-700 border-amber-200",
    rule: "bg-green-50 text-green-700 border-green-200",
    tool: "bg-gray-50 text-gray-700 border-gray-200",
  };
  return colors[type] || "bg-surface-100 text-surface-600 border-surface-200";
}

function getAgentName(agentId: string | null): string {
  if (!agentId) return "未绑定";
  const agent = agents.value.find((a) => a.agent_id === agentId);
  return agent ? agent.name : agentId.slice(0, 8) + "...";
}

// ── Actions ──
async function handleCreate() {
  if (!createForm.value.name.trim()) return;
  creating.value = true;
  try {
    await createMemoryStore({
      name: createForm.value.name.trim(),
      type: createForm.value.type,
      description: createForm.value.description.trim() || null,
      agent_id: createForm.value.agent_id || null,
    });
    showCreateForm.value = false;
    createForm.value = {
      name: "",
      type: "memory_card",
      description: "",
      agent_id: null,
    };
    queryClient.invalidateQueries({ queryKey: ["memory-stores"] });
  } catch (e) {
    console.error("Failed to create store", e);
  } finally {
    creating.value = false;
  }
}

function startEdit(store: MemoryStoreRead) {
  editingStoreId.value = store.store_id;
  editForm.value = {
    name: store.name,
    type: store.type,
    description: store.description || "",
    agent_id: store.agent_id,
  };
}

function cancelEdit() {
  editingStoreId.value = null;
}

async function handleUpdate() {
  if (!editingStoreId.value || !editForm.value.name.trim()) return;
  saving.value = true;
  try {
    await updateMemoryStore(editingStoreId.value, {
      name: editForm.value.name.trim(),
      type: editForm.value.type,
      description: editForm.value.description.trim() || null,
      agent_id: editForm.value.agent_id || null,
    });
    editingStoreId.value = null;
    queryClient.invalidateQueries({ queryKey: ["memory-stores"] });
  } catch (e) {
    console.error("Failed to update store", e);
  } finally {
    saving.value = false;
  }
}

async function handleDelete(storeId: string) {
  try {
    await deleteMemoryStore(storeId);
    confirmDeleteId.value = null;
    queryClient.invalidateQueries({ queryKey: ["memory-stores"] });
  } catch (e) {
    console.error("Failed to delete store", e);
  }
}

async function handleBind(storeId: string, agentId: string) {
  if (!agentId) return;
  try {
    await bindStoreToAgent(storeId, agentId);
    queryClient.invalidateQueries({ queryKey: ["memory-stores"] });
  } catch (e) {
    console.error("Failed to bind store", e);
  }
}

async function handleUnbind(storeId: string) {
  try {
    await unbindStore(storeId);
    queryClient.invalidateQueries({ queryKey: ["memory-stores"] });
  } catch (e) {
    console.error("Failed to unbind store", e);
  }
}
</script>

<template>
  <div>
    <!-- ── Toolbar ── -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-center gap-3">
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-medium text-surface-400">绑定 Agent</label>
          <select
            v-model="filterAgentId"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部</option>
            <option v-for="a in agents" :key="a.agent_id" :value="a.agent_id">
              {{ a.name }}
            </option>
          </select>
        </div>

        <span class="self-end text-xs text-surface-400 ml-auto">
          {{ stores?.length ?? 0 }} 个子库
        </span>

        <button
          class="btn btn-primary btn-sm self-end"
          @click="showCreateForm = true"
        >
          + 新建子库
        </button>
      </div>
    </div>

    <!-- ── Error ── -->
    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载子库失败: {{ (error as Error)?.message }}
    </div>

    <!-- ── Create Form Modal ── -->
    <div v-if="showCreateForm" class="card mb-6 border-brand-200 bg-brand-50 p-5">
      <h3 class="text-sm font-semibold text-surface-700 mb-4">创建新记忆子库</h3>
      <div class="space-y-3">
        <div class="grid grid-cols-2 gap-3">
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-medium text-surface-400">名称 *</label>
            <input
              v-model="createForm.name"
              type="text"
              class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              placeholder="子库名称"
            />
          </div>
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-medium text-surface-400">类型 *</label>
            <select
              v-model="createForm.type"
              class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            >
              <option v-for="opt in TYPE_OPTIONS" :key="opt.value" :value="opt.value">
                {{ opt.label }}
              </option>
            </select>
          </div>
        </div>
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-medium text-surface-400">描述</label>
          <textarea
            v-model="createForm.description"
            rows="2"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            placeholder="可选描述"
          />
        </div>
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-medium text-surface-400">分配给 Agent (可选)</label>
          <select
            v-model="createForm.agent_id"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option :value="null">— 不绑定 —</option>
            <option v-for="a in agents" :key="a.agent_id" :value="a.agent_id">
              {{ a.name }}
            </option>
          </select>
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button class="btn btn-secondary btn-sm" @click="showCreateForm = false">取消</button>
          <button
            class="btn btn-primary btn-sm"
            :disabled="creating || !createForm.name.trim()"
            @click="handleCreate"
          >
            {{ creating ? "创建中..." : "创建" }}
          </button>
        </div>
      </div>
    </div>

    <!-- ── Loading ── -->
    <div v-if="isLoading" class="py-8">
      <LoadingSkeleton variant="detail" />
    </div>

    <!-- ── Store List ── -->
    <div v-else-if="stores && stores.length > 0" class="space-y-3">
      <div
        v-for="store in stores"
        :key="store.store_id"
        class="card p-4 hover:shadow-sm transition-shadow"
      >
        <!-- Edit mode -->
        <div v-if="editingStoreId === store.store_id" class="space-y-3">
          <div class="grid grid-cols-2 gap-3">
            <div class="flex flex-col gap-1">
              <label class="text-2xs font-medium text-surface-400">名称</label>
              <input
                v-model="editForm.name"
                type="text"
                class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              />
            </div>
            <div class="flex flex-col gap-1">
              <label class="text-2xs font-medium text-surface-400">类型</label>
              <select
                v-model="editForm.type"
                class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              >
                <option v-for="opt in TYPE_OPTIONS" :key="opt.value" :value="opt.value">
                  {{ opt.label }}
                </option>
              </select>
            </div>
          </div>
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-medium text-surface-400">描述</label>
            <textarea
              v-model="editForm.description"
              rows="2"
              class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            />
          </div>
          <div class="flex flex-col gap-1">
            <label class="text-2xs font-medium text-surface-400">Agent</label>
            <select
              v-model="editForm.agent_id"
              class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            >
              <option :value="null">— 不绑定 —</option>
              <option v-for="a in agents" :key="a.agent_id" :value="a.agent_id">
                {{ a.name }}
              </option>
            </select>
          </div>
          <div class="flex justify-end gap-2 pt-2">
            <button class="btn btn-secondary btn-sm" @click="cancelEdit">取消</button>
            <button
              class="btn btn-primary btn-sm"
              :disabled="saving || !editForm.name.trim()"
              @click="handleUpdate"
            >
              {{ saving ? "保存中..." : "保存" }}
            </button>
          </div>
        </div>

        <!-- View mode -->
        <div v-else class="flex items-start gap-4">
          <!-- Type badge -->
          <span
            :class="[
              'shrink-0 inline-flex items-center rounded-full px-2.5 py-0.5 text-2xs font-medium border',
              getTypeColor(store.type),
            ]"
          >
            {{ getTypeLabel(store.type) }}
          </span>

          <!-- Content -->
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <p class="text-sm font-medium text-surface-800">{{ store.name }}</p>
              <span class="text-2xs text-surface-400 font-mono">
                {{ store.store_id.slice(0, 8) }}...
              </span>
            </div>
            <p v-if="store.description" class="text-xs text-surface-500 mt-1 line-clamp-1">
              {{ store.description }}
            </p>
            <div class="flex items-center gap-3 mt-2">
              <!-- Agent binding -->
              <span class="text-xs text-surface-500">
                🤖
                <span :class="store.agent_id ? 'text-surface-700 font-medium' : 'text-surface-400'">
                  {{ getAgentName(store.agent_id) }}
                </span>
              </span>
              <span class="text-2xs text-surface-400">
                创建于 {{ formatTime(store.created_at) }}
              </span>
            </div>
          </div>

          <!-- Actions -->
          <div class="shrink-0 flex items-center gap-1">
            <!-- Quick bind -->
            <select
              v-if="!store.agent_id"
              class="rounded border border-surface-200 bg-white text-2xs text-surface-700 px-2 py-1 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              @change="(e: Event) => handleBind(store.store_id, (e.target as HTMLSelectElement).value)"
            >
              <option value="">绑定 Agent...</option>
              <option v-for="a in agents" :key="a.agent_id" :value="a.agent_id">
                {{ a.name }}
              </option>
            </select>
            <button
              v-if="store.agent_id"
              class="btn btn-secondary btn-xs text-xs"
              @click="handleUnbind(store.store_id)"
            >
              解绑
            </button>
            <button
              class="btn btn-ghost btn-xs text-xs"
              @click="startEdit(store)"
            >
              编辑
            </button>
            <button
              class="btn btn-ghost btn-xs text-xs text-red-500"
              @click="confirmDeleteId = store.store_id"
            >
              删除
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- ── Empty ── -->
    <div v-else-if="!isLoading" class="flex flex-col items-center justify-center py-16 text-sm text-surface-400">
      <svg xmlns="http://www.w3.org/2000/svg" class="h-10 w-10 text-surface-300 mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1">
        <path stroke-linecap="round" stroke-linejoin="round" d="M20.25 6.375c0 2.278-3.694 4.125-8.25 4.125S3.75 8.653 3.75 6.375m16.5 0c0-2.278-3.694-4.125-8.25-4.125S3.75 4.097 3.75 6.375m16.5 0v11.25c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125V6.375m16.5 0v3.75m-16.5-3.75v3.75m16.5 0v3.75C20.25 16.153 16.556 18 12 18s-8.25-1.847-8.25-4.125v-3.75m16.5 0c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125" />
      </svg>
      <span>暂无记忆子库</span>
      <span class="text-2xs text-surface-300 mt-1">点击上方"新建子库"创建</span>
    </div>

    <!-- ── Delete confirmation modal ── -->
    <div v-if="confirmDeleteId" class="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div class="card bg-white rounded-xl p-6 shadow-xl max-w-sm w-full mx-4">
        <p class="text-sm font-medium text-surface-700 mb-2">确认删除？</p>
        <p class="text-xs text-surface-500 mb-4">删除后不可恢复。该子库将从所有绑定 Agent 中移除。</p>
        <div class="flex justify-end gap-2">
          <button class="btn btn-secondary btn-sm" @click="confirmDeleteId = null">取消</button>
          <button class="btn btn-danger btn-sm" @click="handleDelete(confirmDeleteId!)">确认删除</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
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
