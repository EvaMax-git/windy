<script setup lang="ts">
import { computed, ref } from "vue";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import PageHeader from "@/components/PageHeader.vue";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import {
  fetchConversation,
  fetchConversationMessages,
  fetchConversations,
} from "@/api/client";
import type { ConversationRead, MessageRead } from "@/types";

const queryClient = useQueryClient();

const page = ref(1);
const pageSize = ref(50);
const filterProjectId = ref("");
const filterType = ref("");
const filterStatus = ref("");

const selectedConversationId = ref<string | null>(null);
const selectedConversationRow = ref<ConversationRead | null>(null);
const drawerOpen = ref(false);
const messagePage = ref(1);
const messagePageSize = ref(50);

const listKey = computed(() => [
  "conversations",
  page.value,
  pageSize.value,
  filterProjectId.value,
  filterType.value,
  filterStatus.value,
] as const);

const { data, isLoading, isFetching, isError, error } = useQuery({
  queryKey: listKey,
  queryFn: () =>
    fetchConversations({
      page: page.value,
      page_size: pageSize.value,
      project_id: filterProjectId.value || undefined,
      conversation_type: filterType.value || undefined,
      conversation_status: filterStatus.value || undefined,
    }),
  placeholderData: (prev) => prev,
});

const detailKey = computed(() => [
  "conversation-detail",
  selectedConversationId.value,
] as const);

const { data: detail, isLoading: detailLoading, isError: detailError } = useQuery({
  queryKey: detailKey,
  queryFn: () => fetchConversation(selectedConversationId.value!),
  enabled: computed(() => drawerOpen.value && !!selectedConversationId.value),
});

const messageKey = computed(() => [
  "conversation-messages",
  selectedConversationId.value,
  messagePage.value,
  messagePageSize.value,
] as const);

const {
  data: messages,
  isLoading: messagesLoading,
  isFetching: messagesFetching,
  isError: messagesError,
  error: messagesErr,
} = useQuery({
  queryKey: messageKey,
  queryFn: () =>
    fetchConversationMessages(selectedConversationId.value!, {
      page: messagePage.value,
      page_size: messagePageSize.value,
    }),
  enabled: computed(() => drawerOpen.value && !!selectedConversationId.value),
  placeholderData: (prev) => prev,
});

const selectedConversation = computed(() => detail.value ?? selectedConversationRow.value);
const selectedTitle = computed(() => selectedConversation.value?.title || "对话详情");

const conversationColumns = [
  { key: "title", label: "标题", width: "260px" },
  { key: "conversation_type", label: "类型", width: "110px" },
  { key: "source_platform", label: "来源", width: "120px" },
  { key: "conversation_status", label: "状态", width: "90px" },
  { key: "sensitivity_level", label: "敏感度", width: "90px" },
  { key: "started_at", label: "开始时间", width: "160px" },
  { key: "updated_at", label: "更新时间", width: "160px" },
];

function openDetail(conversation: ConversationRead) {
  selectedConversationId.value = conversation.conversation_id;
  selectedConversationRow.value = conversation;
  messagePage.value = 1;
  drawerOpen.value = true;
}

function closeDrawer() {
  drawerOpen.value = false;
  selectedConversationId.value = null;
  selectedConversationRow.value = null;
  messagePage.value = 1;
}

function clearFilters() {
  filterProjectId.value = "";
  filterType.value = "";
  filterStatus.value = "";
  page.value = 1;
}

function refreshList() {
  queryClient.invalidateQueries({ queryKey: ["conversations"] });
}

function refreshDetail() {
  if (!selectedConversationId.value) return;
  queryClient.invalidateQueries({ queryKey: ["conversation-detail"] });
  queryClient.invalidateQueries({ queryKey: ["conversation-messages"] });
}

function setPage(nextPage: number) {
  page.value = nextPage;
}

function setMessagePage(nextPage: number) {
  messagePage.value = nextPage;
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  return new Date(iso).toLocaleString();
}

function truncate(text: string | null | undefined, max: number): string {
  if (!text) return "";
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

function typeLabel(type: string): string {
  const map: Record<string, string> = {
    chat: "聊天",
    meeting: "会议",
    email_thread: "邮件",
    system_event: "系统事件",
    agent_run: "Agent 运行",
  };
  return map[type] ?? type;
}

function roleLabel(role: string): string {
  const map: Record<string, string> = {
    user: "用户",
    assistant: "助手",
    agent: "Agent",
    system: "系统",
    tool: "工具",
    other: "其他",
  };
  return map[role] ?? role;
}

function roleClass(role: string): string {
  const map: Record<string, string> = {
    user: "bg-blue-50 text-blue-700 ring-blue-600/20",
    assistant: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
    agent: "bg-indigo-50 text-indigo-700 ring-indigo-600/20",
    system: "bg-surface-100 text-surface-600 ring-surface-500/20",
    tool: "bg-amber-50 text-amber-700 ring-amber-600/20",
  };
  return map[role] ?? "bg-surface-100 text-surface-600 ring-surface-500/20";
}

function sensitivityClass(level: string): string {
  const map: Record<string, string> = {
    public: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
    normal: "bg-blue-50 text-blue-700 ring-blue-600/20",
    private: "bg-amber-50 text-amber-700 ring-amber-600/20",
    sensitive: "bg-orange-50 text-orange-700 ring-orange-600/20",
    secret: "bg-red-50 text-red-700 ring-red-600/20",
  };
  return map[level] ?? "bg-surface-100 text-surface-600 ring-surface-500/20";
}

function messageAuthor(message: MessageRead): string {
  return message.sender_label || roleLabel(message.role_code);
}

function messageText(message: MessageRead): string {
  return message.content_markdown || message.content_text;
}
</script>

<template>
  <div class="mx-auto max-w-7xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader
      title="对话管理"
      subtitle="查看 conversations 与 messages，支持列表筛选、详情、消息分页和刷新"
    />

    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-end gap-3">
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">项目 ID</label>
          <input
            v-model="filterProjectId"
            type="text"
            placeholder="UUID..."
            class="w-64 rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm font-mono text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            @keyup.enter="page = 1"
          />
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">类型</label>
          <select
            v-model="filterType"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            @change="page = 1"
          >
            <option value="">全部类型</option>
            <option value="chat">聊天</option>
            <option value="meeting">会议</option>
            <option value="email_thread">邮件</option>
            <option value="system_event">系统事件</option>
            <option value="agent_run">Agent 运行</option>
          </select>
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">状态</label>
          <select
            v-model="filterStatus"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            @change="page = 1"
          >
            <option value="">全部状态</option>
            <option value="active">active</option>
            <option value="archived">archived</option>
            <option value="deleted">deleted</option>
          </select>
        </div>

        <div class="flex items-center gap-2">
          <button class="btn btn-secondary btn-sm" @click="clearFilters">清除</button>
          <button class="btn btn-primary btn-sm" :disabled="isFetching" @click="refreshList">
            {{ isFetching ? "刷新中..." : "刷新" }}
          </button>
        </div>

        <span class="ml-auto self-center text-xs text-surface-400">
          {{ data?.page_info?.total_items ?? 0 }} 条对话
        </span>
      </div>
    </div>

    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载对话失败：{{ (error as Error)?.message }}
    </div>

    <DataTable
      :items="data?.items ?? []"
      :columns="conversationColumns"
      :loading="isLoading"
      empty-message="暂无对话"
      clickable
      row-key="conversation_id"
      @row-click="openDetail"
    >
      <template #cell-title="{ value, item }">
        <div class="min-w-0 max-w-[320px]">
          <p class="truncate text-sm font-medium text-surface-800">
            {{ value || "(无标题)" }}
          </p>
          <p class="mt-0.5 truncate font-mono text-2xs text-surface-400">
            {{ (item as ConversationRead).conversation_id.slice(0, 12) }}...
          </p>
        </div>
      </template>

      <template #cell-conversation_type="{ value }">
        <span class="badge text-2xs bg-surface-100 text-surface-600">
          {{ typeLabel(value as string) }}
        </span>
      </template>

      <template #cell-source_platform="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ value }}</span>
      </template>

      <template #cell-conversation_status="{ value }">
        <StatusBadge :status="value as string" />
      </template>

      <template #cell-sensitivity_level="{ value }">
        <span :class="['badge ring-1 ring-inset', sensitivityClass(value as string)]">
          {{ value }}
        </span>
      </template>

      <template #cell-started_at="{ value }">
        <span class="font-mono text-xs text-surface-500">
          {{ formatTime(value as string | null) }}
        </span>
      </template>

      <template #cell-updated_at="{ value }">
        <span class="font-mono text-xs text-surface-500">
          {{ formatTime(value as string | null) }}
        </span>
      </template>
    </DataTable>

    <Pagination
      v-if="data?.page_info"
      :page-info="data.page_info"
      @page-change="setPage"
    />

    <DetailDrawer
      :open="drawerOpen"
      :title="selectedTitle"
      width="w-[760px] max-w-full"
      @close="closeDrawer"
    >
      <div class="mb-4 flex items-center justify-between gap-3 border-b border-surface-200 pb-3">
        <div class="min-w-0">
          <p class="truncate text-sm font-semibold text-surface-800">
            {{ selectedConversation?.title || "(无标题)" }}
          </p>
          <p class="mt-0.5 truncate font-mono text-2xs text-surface-400">
            {{ selectedConversation?.conversation_id }}
          </p>
        </div>
        <button
          class="btn btn-secondary btn-sm shrink-0"
          :disabled="detailLoading || messagesFetching"
          @click="refreshDetail"
        >
          {{ messagesFetching ? "刷新中..." : "刷新详情" }}
        </button>
      </div>

      <div v-if="detailLoading" class="flex items-center justify-center py-12 text-sm text-surface-400">
        加载对话详情...
      </div>

      <div
        v-else-if="detailError"
        class="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700"
      >
        加载详情失败
      </div>

      <template v-else-if="selectedConversation">
        <dl class="mb-5 grid grid-cols-2 gap-4 rounded-lg border border-surface-200 bg-surface-50 p-4">
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">项目 ID</dt>
            <dd class="mt-1 break-all font-mono text-xs text-surface-700">
              {{ selectedConversation.project_id || "-" }}
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">拥有者</dt>
            <dd class="mt-1 break-all font-mono text-xs text-surface-700">
              {{ selectedConversation.owner_user_id || "-" }}
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">类型</dt>
            <dd class="mt-1 text-sm text-surface-700">
              {{ typeLabel(selectedConversation.conversation_type) }}
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">来源</dt>
            <dd class="mt-1 font-mono text-xs text-surface-700">
              {{ selectedConversation.source_platform }}
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">状态</dt>
            <dd class="mt-1">
              <StatusBadge :status="selectedConversation.conversation_status" />
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">敏感度</dt>
            <dd class="mt-1">
              <span :class="['badge ring-1 ring-inset', sensitivityClass(selectedConversation.sensitivity_level)]">
                {{ selectedConversation.sensitivity_level }}
              </span>
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">开始时间</dt>
            <dd class="mt-1 text-xs text-surface-700">
              {{ formatTime(selectedConversation.started_at) }}
            </dd>
          </div>
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">结束时间</dt>
            <dd class="mt-1 text-xs text-surface-700">
              {{ formatTime(selectedConversation.ended_at) }}
            </dd>
          </div>
        </dl>

        <div class="mb-3 flex items-center justify-between">
          <h4 class="text-sm font-semibold text-surface-800">
            消息
            <span class="font-normal text-surface-400">
              ({{ messages?.page_info?.total_items ?? 0 }})
            </span>
          </h4>
          <span class="text-2xs text-surface-400">
            按 message_time 正序分页
          </span>
        </div>

        <div
          v-if="messagesError"
          class="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700"
        >
          加载消息失败：{{ (messagesErr as Error)?.message }}
        </div>

        <div v-else-if="messagesLoading" class="py-8 text-center text-sm text-surface-400">
          加载消息...
        </div>

        <div v-else-if="!messages?.items?.length" class="rounded-lg border border-surface-200 py-10 text-center text-sm text-surface-400">
          暂无消息
        </div>

        <div v-else class="space-y-3">
          <article
            v-for="message in messages.items"
            :key="message.message_id"
            class="rounded-lg border border-surface-200 bg-white p-4"
          >
            <div class="mb-2 flex flex-wrap items-center gap-2">
              <span :class="['badge text-2xs ring-1 ring-inset', roleClass(message.role_code)]">
                {{ roleLabel(message.role_code) }}
              </span>
              <span class="text-sm font-medium text-surface-700">
                {{ messageAuthor(message) }}
              </span>
              <span class="font-mono text-2xs text-surface-400">
                {{ formatTime(message.message_time) }}
              </span>
              <span class="ml-auto font-mono text-2xs text-surface-400">
                {{ message.message_id.slice(0, 12) }}...
              </span>
            </div>

            <div class="whitespace-pre-wrap break-words text-sm leading-relaxed text-surface-700">
              {{ messageText(message) }}
            </div>

            <div class="mt-3 flex flex-wrap items-center gap-2 border-t border-surface-100 pt-2 text-2xs text-surface-400">
              <span class="font-mono">hash {{ message.content_hash.slice(0, 12) }}...</span>
              <span>ingested {{ formatTime(message.ingested_at) }}</span>
              <span v-if="message.event_source_id" class="font-mono">
                source {{ message.event_source_id.slice(0, 12) }}...
              </span>
              <span v-if="message.parent_message_id" class="font-mono">
                parent {{ message.parent_message_id.slice(0, 12) }}...
              </span>
              <span v-if="message.pii_flags.length > 0" class="text-amber-600">
                PII {{ message.pii_flags.length }}
              </span>
            </div>
          </article>
        </div>

        <Pagination
          v-if="messages?.page_info"
          :page-info="messages.page_info"
          @page-change="setMessagePage"
        />
      </template>

      <template #footer>
        <div class="flex items-center justify-between">
          <span class="text-2xs text-surface-400">
            {{ selectedConversationId ? truncate(selectedConversationId, 18) : "-" }}
          </span>
          <button class="btn btn-secondary btn-sm" @click="closeDrawer">
            关闭
          </button>
        </div>
      </template>
    </DetailDrawer>
  </div>
</template>
