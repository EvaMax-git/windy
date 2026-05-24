<script setup lang="ts">
import { ref, nextTick, computed } from "vue";
import { chat, ApiError } from "@/api/client";

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  citations?: Array<{ document_title: string; snippet: string }>;
  degraded?: boolean;
}

const messages = ref<ChatMessage[]>([]);
const inputText = ref("");
const isLoading = ref(false);
const conversationId = ref<string | null>(null);
const messagesContainer = ref<HTMLElement | null>(null);

async function sendMessage() {
  const text = inputText.value.trim();
  if (!text || isLoading.value) return;

  messages.value.push({ role: "user", content: text });
  inputText.value = "";
  isLoading.value = true;

  await nextTick();
  scrollToBottom();

  try {
    const resp = await chat({
      message: text,
      conversation_id: conversationId.value || undefined,
      project_id: "2769506e-38ac-4361-9113-43ff4faee81b", // Default project
      max_context_chunks: 5,
    });

    conversationId.value = resp.conversation_id;
    messages.value.push({
      role: "assistant",
      content: resp.answer,
      citations: resp.citations,
      degraded: resp.degraded,
    });
  } catch (err) {
    let errorMsg = err instanceof Error ? err.message : "请求失败";
    // Show validation details if available
    if (err instanceof ApiError && err.details?.errors) {
      const details = (err.details.errors as any[])
        .map((e) => `${e.loc?.join(".")}: ${e.msg}`)
        .join("; ");
      errorMsg += `\n详情: ${details}`;
    }
    messages.value.push({
      role: "assistant",
      content: `错误: ${errorMsg}`,
    });
  } finally {
    isLoading.value = false;
    await nextTick();
    scrollToBottom();
  }
}

function scrollToBottom() {
  if (messagesContainer.value) {
    messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight;
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

function newChat() {
  messages.value = [];
  conversationId.value = null;
}
</script>

<template>
  <div class="flex h-full flex-col">
    <!-- Header -->
    <div class="flex items-center justify-between border-b border-surface-200 px-6 py-4">
      <div>
        <h1 class="text-lg font-semibold text-surface-800">知识问答</h1>
        <p class="text-xs text-surface-400">基于知识库的 AI 对话</p>
      </div>
      <button
        class="btn btn-secondary btn-sm"
        @click="newChat"
      >
        新对话
      </button>
    </div>

    <!-- Messages -->
    <div
      ref="messagesContainer"
      class="flex-1 overflow-y-auto px-6 py-4 space-y-4"
    >
      <!-- Empty state -->
      <div
        v-if="messages.length === 0"
        class="flex h-full flex-col items-center justify-center text-surface-400"
      >
        <svg class="h-12 w-12 mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1">
          <path stroke-linecap="round" stroke-linejoin="round" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
        </svg>
        <p class="text-sm">输入问题开始对话</p>
        <p class="text-xs mt-1">AI 将基于知识库内容回答</p>
      </div>

      <!-- Message bubbles -->
      <div
        v-for="(msg, idx) in messages"
        :key="idx"
        :class="[
          'flex',
          msg.role === 'user' ? 'justify-end' : 'justify-start',
        ]"
      >
        <div
          :class="[
            'max-w-[80%] rounded-lg px-4 py-3',
            msg.role === 'user'
              ? 'bg-brand-600 text-white'
              : 'bg-surface-100 text-surface-800',
          ]"
        >
          <p class="text-sm whitespace-pre-wrap">{{ msg.content }}</p>

          <!-- Citations -->
          <div
            v-if="msg.citations && msg.citations.length > 0"
            class="mt-2 border-t border-surface-200 pt-2"
          >
            <p class="text-xs text-surface-400 mb-1">引用来源:</p>
            <div
              v-for="(cite, ci) in msg.citations"
              :key="ci"
              class="text-xs text-surface-500"
            >
              [{{ ci + 1 }}] {{ cite.document_title }}
            </div>
          </div>

          <!-- Degraded badge -->
          <span
            v-if="msg.degraded"
            class="inline-block mt-1 rounded px-1.5 py-0.5 text-2xs bg-amber-100 text-amber-700"
          >
            AI 服务不可用
          </span>
        </div>
      </div>

      <!-- Loading indicator -->
      <div v-if="isLoading" class="flex justify-start">
        <div class="rounded-lg bg-surface-100 px-4 py-3">
          <div class="flex items-center gap-2">
            <svg class="h-4 w-4 animate-spin text-brand-500" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
            </svg>
            <span class="text-sm text-surface-500">思考中...</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Input -->
    <div class="border-t border-surface-200 px-6 py-4">
      <div class="flex gap-3">
        <textarea
          v-model="inputText"
          class="flex-1 rounded-lg border border-surface-300 px-4 py-3 text-sm resize-none focus:border-brand-400 focus:ring-1 focus:ring-brand-400 outline-none"
          rows="2"
          placeholder="输入问题... (Enter 发送, Shift+Enter 换行)"
          :disabled="isLoading"
          @keydown="onKeydown"
        />
        <button
          class="btn btn-primary self-end"
          :disabled="!inputText.trim() || isLoading"
          @click="sendMessage"
        >
          发送
        </button>
      </div>
    </div>
  </div>
</template>
