<script setup lang="ts">
import { ref, computed } from "vue";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import PageHeader from "@/components/PageHeader.vue";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import AssetUpload from "@/components/AssetUpload.vue";
import {
  fetchAssets,
  fetchAsset,
  fetchAssetMetadata,
  addAssetMetadata,
  deleteAssetMetadata,
  deleteAsset,
  restoreAsset,
} from "@/api/client";
import type { Asset, AssetMetadata } from "@/types";

// ── List state ──
const page = ref(1);
const pageSize = ref(50);
const filterProjectId = ref("");
const filterAssetType = ref("");
const filterStatus = ref("");
const filterSensitivity = ref("");
const filterIngestState = ref("");

const listKey = computed(() => [
  "assets",
  page.value,
  pageSize.value,
  filterProjectId.value,
  filterAssetType.value,
  filterStatus.value,
  filterSensitivity.value,
  filterIngestState.value,
] as const);

const { data, isLoading, isError, error } = useQuery({
  queryKey: listKey,
  queryFn: () =>
    fetchAssets({
      page: page.value,
      page_size: pageSize.value,
      project_id: filterProjectId.value || undefined,
      asset_type: filterAssetType.value || undefined,
      status: filterStatus.value || undefined,
      sensitivity_level: filterSensitivity.value || undefined,
      ingest_state: filterIngestState.value || undefined,
    }),
  placeholderData: (prev) => prev,
});

// ── Detail drawer ──
const selectedAssetId = ref<string | null>(null);
const drawerTab = ref<"info" | "metadata">("info");
const drawerOpen = ref(false);
const addMetaForm = ref({ key: "", value: "", type: "text" });
const metaSubmitting = ref(false);
const actionLoading = ref(false);

const { data: assetDetail, isLoading: detailLoading } = useQuery({
  queryKey: ["asset", selectedAssetId],
  queryFn: () => fetchAsset(selectedAssetId.value!),
  enabled: computed(() => drawerOpen.value && !!selectedAssetId.value),
});

const { data: metadataList } = useQuery({
  queryKey: ["asset-metadata", selectedAssetId],
  queryFn: () => fetchAssetMetadata(selectedAssetId.value!),
  enabled: computed(() => drawerOpen.value && !!selectedAssetId.value),
});

const queryClient = useQueryClient();

// ── Table columns ──
const columns = [
  { key: "title", label: "标题", width: "220px" },
  { key: "asset_uid", label: "资产标识", width: "180px" },
  { key: "asset_type", label: "类型", width: "90px" },
  { key: "status", label: "状态", width: "90px" },
  { key: "sensitivity_level", label: "敏感等级", width: "90px" },
  { key: "ingest_state", label: "入库状态", width: "90px" },
  { key: "size_bytes", label: "大小", width: "80px" },
  { key: "created_at", label: "创建时间", width: "160px" },
];

// ── Helpers ──
function formatSize(bytes: number | null): string {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

function formatHash(hash: string): string {
  if (hash.length <= 16) return hash;
  return hash.slice(0, 8) + "..." + hash.slice(-8);
}

function assetTypeLabel(type: string): string {
  const map: Record<string, string> = {
    document: "文档",
    image: "图片",
    audio: "音频",
    video: "视频",
    archive: "压缩包",
    dataset: "数据集",
    note: "笔记",
    url: "链接",
    other: "其他",
  };
  return map[type] || type;
}

const SENSITIVITY_STYLES: Record<string, string> = {
  public: "bg-surface-100 text-surface-600",
  normal: "bg-blue-50 text-blue-700",
  private: "bg-amber-50 text-amber-700",
  sensitive: "bg-orange-50 text-orange-700",
  secret: "bg-red-50 text-red-700",
};

const SENSITIVITY_LABELS: Record<string, string> = {
  public: "公开",
  normal: "普通",
  private: "内部",
  sensitive: "敏感",
  secret: "机密",
};

const ASSET_TYPE_STYLES: Record<string, string> = {
  document: "bg-indigo-50 text-indigo-700",
  image: "bg-violet-50 text-violet-700",
  audio: "bg-pink-50 text-pink-700",
  video: "bg-rose-50 text-rose-700",
  archive: "bg-teal-50 text-teal-700",
  dataset: "bg-cyan-50 text-cyan-700",
  note: "bg-green-50 text-green-700",
  url: "bg-sky-50 text-sky-700",
  other: "bg-surface-100 text-surface-600",
};

function openDetail(asset: Asset) {
  selectedAssetId.value = asset.asset_id;
  drawerTab.value = "info";
  drawerOpen.value = true;
}

// ── Upload handler ──
function onUploadComplete(results: { file: File; assetId: string; success: boolean }[]) {
  const successCount = results.filter((r) => r.success).length;
  if (successCount > 0) {
    // Refresh the asset list
    queryClient.invalidateQueries({ queryKey: ["assets"] });
  }
}

// ── Metadata actions ──
async function handleAddMetadata() {
  if (!selectedAssetId.value || !addMetaForm.value.key.trim()) return;
  metaSubmitting.value = true;
  try {
    await addAssetMetadata(selectedAssetId.value, {
      metadata_key: addMetaForm.value.key.trim(),
      metadata_value: addMetaForm.value.value,
      value_type: addMetaForm.value.type,
    });
    addMetaForm.value = { key: "", value: "", type: "text" };
    queryClient.invalidateQueries({ queryKey: ["asset-metadata", selectedAssetId.value] });
  } catch (e) {
    console.error("Failed to add metadata", e);
  } finally {
    metaSubmitting.value = false;
  }
}

async function handleDeleteMetadata(metaId: string) {
  if (!selectedAssetId.value || !confirm("确定要删除此元数据条目？")) return;
  try {
    await deleteAssetMetadata(selectedAssetId.value, metaId);
    queryClient.invalidateQueries({ queryKey: ["asset-metadata", selectedAssetId.value] });
  } catch (e) {
    console.error("Failed to delete metadata", e);
  }
}

async function handleDeleteAsset() {
  if (!selectedAssetId.value || !confirm("确定要删除此资产？（软删除，可恢复）")) return;
  actionLoading.value = true;
  try {
    await deleteAsset(selectedAssetId.value);
    queryClient.invalidateQueries({ queryKey: ["assets"] });
    queryClient.invalidateQueries({ queryKey: ["asset", selectedAssetId.value] });
  } catch (e) {
    console.error("Failed to delete asset", e);
  } finally {
    actionLoading.value = false;
  }
}

async function handleRestoreAsset() {
  if (!selectedAssetId.value || !confirm("确定要恢复此资产？")) return;
  actionLoading.value = true;
  try {
    await restoreAsset(selectedAssetId.value);
    queryClient.invalidateQueries({ queryKey: ["assets"] });
    queryClient.invalidateQueries({ queryKey: ["asset", selectedAssetId.value] });
  } catch (e) {
    console.error("Failed to restore asset", e);
  } finally {
    actionLoading.value = false;
  }
}

function getMetaTypeIcon(type: string): string {
  const map: Record<string, string> = {
    text: "Aa",
    number: "123",
    boolean: "T/F",
    date: "📅",
    json: "{}",
  };
  return map[type] || "?";
}
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader
      title="资产管理"
      subtitle="上传、查看和管理项目资产 — 支持拖拽上传、元数据管理和敏感等级标记"
    />

    <!-- Upload zone -->
    <div class="mb-6">
      <AssetUpload
        v-if="filterProjectId"
        :project-id="filterProjectId"
        @upload-complete="onUploadComplete"
      />
      <div
        v-else
        class="rounded-lg border border-surface-200 bg-surface-50 p-4 text-sm text-surface-500"
      >
        <p>请先选择一个项目以启用上传功能。</p>
      </div>
    </div>

    <!-- Filters -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-center gap-3">
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-medium text-surface-400">项目ID</label>
          <input
            v-model="filterProjectId"
            type="text"
            placeholder="UUID..."
            class="w-64 rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 placeholder:text-surface-300"
          />
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-medium text-surface-400">资产类型</label>
          <select
            v-model="filterAssetType"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部类型</option>
            <option value="document">文档</option>
            <option value="image">图片</option>
            <option value="audio">音频</option>
            <option value="video">视频</option>
            <option value="archive">压缩包</option>
            <option value="dataset">数据集</option>
            <option value="note">笔记</option>
            <option value="url">链接</option>
            <option value="other">其他</option>
          </select>
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-medium text-surface-400">状态</label>
          <select
            v-model="filterStatus"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部状态</option>
            <option value="active">活跃</option>
            <option value="archived">已归档</option>
            <option value="deleted">已删除</option>
            <option value="quarantined">已隔离</option>
          </select>
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-medium text-surface-400">敏感等级</label>
          <select
            v-model="filterSensitivity"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部等级</option>
            <option value="public">公开</option>
            <option value="normal">普通</option>
            <option value="private">内部</option>
            <option value="sensitive">敏感</option>
            <option value="secret">机密</option>
          </select>
        </div>

        <div class="flex flex-col gap-1">
          <label class="text-2xs font-medium text-surface-400">入库状态</label>
          <select
            v-model="filterIngestState"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部状态</option>
            <option value="pending">待处理</option>
            <option value="staged">已暂存</option>
            <option value="importing">导入中</option>
            <option value="ready">就绪</option>
            <option value="failed">失败</option>
          </select>
        </div>

        <span class="self-end text-xs text-surface-400 ml-auto">
          {{ data?.page_info?.total_items ?? 0 }} 个资产
        </span>
      </div>
    </div>

    <!-- Error -->
    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载资产失败: {{ (error as Error)?.message }}
    </div>

    <!-- Table -->
    <DataTable
      :items="data?.items ?? []"
      :columns="columns"
      :loading="isLoading"
      empty-message="暂无资产 — 请上传或检查项目ID是否正确"
      clickable
      row-key="asset_id"
      @row-click="openDetail"
    >
      <!-- Title -->
      <template #cell-title="{ value, item }">
        <div class="flex items-center gap-2 min-w-0">
          <!-- File type icon -->
          <span class="shrink-0 text-xs text-surface-400">
            {{ ((item as Asset).asset_type === 'image' ? '🖼' :
                (item as Asset).asset_type === 'document' ? '📄' :
                (item as Asset).asset_type === 'audio' ? '🎵' :
                (item as Asset).asset_type === 'video' ? '🎬' :
                (item as Asset).asset_type === 'archive' ? '📦' :
                (item as Asset).asset_type === 'dataset' ? '📊' :
                (item as Asset).asset_type === 'note' ? '📝' :
                (item as Asset).asset_type === 'url' ? '🔗' : '📎') }}
          </span>
          <div class="min-w-0">
            <p class="truncate text-sm font-medium text-surface-800">{{ value }}</p>
            <p class="text-2xs text-surface-400 truncate">{{ (item as Asset).original_filename || '—' }}</p>
          </div>
        </div>
      </template>

      <!-- Asset UID -->
      <template #cell-asset_uid="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ (value as string).slice(0, 20) }}…</span>
      </template>

      <!-- Asset Type -->
      <template #cell-asset_type="{ value }">
        <span
          :class="[
            'badge text-2xs font-medium',
            ASSET_TYPE_STYLES[value as string] || 'bg-surface-100 text-surface-600',
          ]"
        >
          {{ assetTypeLabel(value as string) }}
        </span>
      </template>

      <!-- Status -->
      <template #cell-status="{ value, item }">
        <StatusBadge :status="value as string" />
        <span
          v-if="(item as Asset).knowledge_state && (item as Asset).knowledge_state !== 'not_started'"
          class="ml-1 text-2xs text-surface-400"
        >
          · K:{{ (item as Asset).knowledge_state }}
        </span>
      </template>

      <!-- Sensitivity Level -->
      <template #cell-sensitivity_level="{ value }">
        <span
          :class="[
            'badge text-2xs font-medium',
            SENSITIVITY_STYLES[value as string] || 'bg-surface-100 text-surface-600',
          ]"
        >
          {{ SENSITIVITY_LABELS[value as string] || value }}
        </span>
      </template>

      <!-- Ingest State -->
      <template #cell-ingest_state="{ value }">
        <StatusBadge :status="value as string" />
      </template>

      <!-- Size -->
      <template #cell-size_bytes="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ formatSize(value as number | null) }}</span>
      </template>

      <!-- Created At -->
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
      title="资产详情"
      width="w-[540px] max-w-full"
      @close="drawerOpen = false"
    >
      <!-- Loading -->
      <div v-if="detailLoading" class="py-4">
        <LoadingSkeleton variant="detail" />
      </div>

      <template v-else-if="assetDetail">
        <!-- Tab bar -->
        <div class="flex items-center gap-1 border-b border-surface-200 mb-4">
          <button
            :class="[
              'px-3 py-2 text-sm font-medium border-b-2 transition-colors -mb-px',
              drawerTab === 'info'
                ? 'border-brand-500 text-brand-600'
                : 'border-transparent text-surface-500 hover:text-surface-700',
            ]"
            @click="drawerTab = 'info'"
          >
            基本信息
          </button>
          <button
            :class="[
              'px-3 py-2 text-sm font-medium border-b-2 transition-colors -mb-px',
              drawerTab === 'metadata'
                ? 'border-brand-500 text-brand-600'
                : 'border-transparent text-surface-500 hover:text-surface-700',
            ]"
            @click="drawerTab = 'metadata'"
          >
            元数据 ({{ metadataList?.length ?? 0 }})
          </button>
        </div>

        <!-- Info Tab -->
        <div v-if="drawerTab === 'info'">
          <dl class="space-y-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">资产标识 (UID)</dt>
              <dd class="mt-1 font-mono text-xs text-surface-700 break-all">{{ assetDetail.asset_uid }}</dd>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">标题</dt>
                <dd class="mt-1 text-sm text-surface-700">{{ assetDetail.title }}</dd>
              </div>
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">类型</dt>
                <dd class="mt-1">
                  <span
                    :class="[
                      'badge text-2xs font-medium',
                      ASSET_TYPE_STYLES[assetDetail.asset_type] || 'bg-surface-100 text-surface-600',
                    ]"
                  >
                    {{ assetTypeLabel(assetDetail.asset_type) }}
                  </span>
                </dd>
              </div>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">状态</dt>
                <dd class="mt-1"><StatusBadge :status="assetDetail.status" /></dd>
              </div>
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">敏感等级</dt>
                <dd class="mt-1">
                  <span
                    :class="[
                      'badge text-2xs font-medium',
                      SENSITIVITY_STYLES[assetDetail.sensitivity_level] || 'bg-surface-100 text-surface-600',
                    ]"
                  >
                    {{ SENSITIVITY_LABELS[assetDetail.sensitivity_level] || assetDetail.sensitivity_level }}
                  </span>
                </dd>
              </div>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">入库状态</dt>
                <dd class="mt-1"><StatusBadge :status="assetDetail.ingest_state" /></dd>
              </div>
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">知识状态</dt>
                <dd class="mt-1"><StatusBadge :status="assetDetail.knowledge_state" /></dd>
              </div>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">版本</dt>
                <dd class="mt-1 font-mono text-xs">{{ assetDetail.current_version }}</dd>
              </div>
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">保留策略</dt>
                <dd class="mt-1 text-xs">{{ assetDetail.retention_policy }}</dd>
              </div>
            </div>

            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">原始文件名</dt>
              <dd class="mt-1 text-xs text-surface-700">{{ assetDetail.original_filename || '—' }}</dd>
            </div>

            <div v-if="assetDetail.media_type">
              <dt class="text-2xs font-semibold uppercase text-surface-400">MIME 类型</dt>
              <dd class="mt-1 font-mono text-xs text-surface-500">{{ assetDetail.media_type }}</dd>
            </div>

            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">文件大小</dt>
              <dd class="mt-1 font-mono text-xs">{{ formatSize(assetDetail.size_bytes) }}</dd>
            </div>

            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">内容哈希 (SHA-256)</dt>
              <dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ formatHash(assetDetail.content_hash) }}</dd>
            </div>

            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">存储引用</dt>
              <dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ assetDetail.storage_ref }}</dd>
            </div>

            <div v-if="assetDetail.canonical_uri">
              <dt class="text-2xs font-semibold uppercase text-surface-400">规范 URI</dt>
              <dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ assetDetail.canonical_uri }}</dd>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">项目ID</dt>
                <dd class="mt-1 font-mono text-xs text-surface-500 truncate">
                  {{ assetDetail.project_id ? String(assetDetail.project_id).slice(0, 12) + '…' : '—' }}
                </dd>
              </div>
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">存储后端</dt>
                <dd class="mt-1 font-mono text-2xs text-surface-500">{{ assetDetail.storage_backend }}</dd>
              </div>
            </div>

            <div v-if="assetDetail.imported_from">
              <dt class="text-2xs font-semibold uppercase text-surface-400">导入来源</dt>
              <dd class="mt-1 text-xs">
                {{ assetDetail.imported_from }}
                <span
                  v-if="assetDetail.imported_source_id"
                  class="font-mono text-xs text-surface-400 ml-1"
                >
                  ({{ assetDetail.imported_source_id.slice(0, 12) }}…)
                </span>
              </dd>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt>
                <dd class="mt-1 text-xs">{{ formatTime(assetDetail.created_at) }}</dd>
              </div>
              <div>
                <dt class="text-2xs font-semibold uppercase text-surface-400">更新时间</dt>
                <dd class="mt-1 text-xs">{{ formatTime(assetDetail.updated_at) }}</dd>
              </div>
            </div>

            <div v-if="assetDetail.archived_at">
              <dt class="text-2xs font-semibold uppercase text-surface-400">归档时间</dt>
              <dd class="mt-1 text-xs">{{ formatTime(assetDetail.archived_at) }}</dd>
            </div>
          </dl>

          <!-- Actions -->
          <div class="mt-6 pt-4 border-t border-surface-200 flex items-center gap-2">
            <button
              v-if="assetDetail.status === 'deleted' || assetDetail.status === 'archived'"
              class="btn btn-secondary btn-sm"
              :disabled="actionLoading"
              @click="handleRestoreAsset"
            >
              恢复资产
            </button>
            <button
              v-if="assetDetail.status === 'active'"
              class="btn btn-danger btn-sm"
              :disabled="actionLoading"
              @click="handleDeleteAsset"
            >
              删除资产
            </button>
          </div>
        </div>

        <!-- Metadata Tab -->
        <div v-if="drawerTab === 'metadata'">
          <!-- Add metadata form -->
          <div class="rounded-lg border border-surface-200 bg-surface-50 p-4 mb-4">
            <p class="text-xs font-semibold text-surface-700 mb-3">添加元数据</p>
            <div class="space-y-3">
              <div class="grid grid-cols-4 gap-2">
                <input
                  v-model="addMetaForm.key"
                  type="text"
                  placeholder="键名"
                  class="col-span-1 rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                />
                <input
                  v-model="addMetaForm.value"
                  type="text"
                  placeholder="值"
                  class="col-span-2 rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                />
                <select
                  v-model="addMetaForm.type"
                  class="col-span-1 rounded-lg border border-surface-200 bg-white px-2 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                >
                  <option value="text">文本</option>
                  <option value="number">数字</option>
                  <option value="boolean">布尔</option>
                  <option value="date">日期</option>
                  <option value="json">JSON</option>
                </select>
              </div>
              <button
                class="btn btn-primary btn-sm w-full"
                :disabled="metaSubmitting || !addMetaForm.key.trim()"
                @click="handleAddMetadata"
              >
                {{ metaSubmitting ? '添加中...' : '添加元数据' }}
              </button>
            </div>
          </div>

          <!-- Metadata list -->
          <div v-if="metadataList && metadataList.length > 0" class="divide-y divide-surface-100 rounded-lg border border-surface-200 overflow-hidden">
            <div
              v-for="meta in metadataList"
              :key="meta.asset_metadata_id"
              class="flex items-start gap-3 px-4 py-3 hover:bg-surface-50 transition-colors"
            >
              <!-- Type badge -->
              <span
                class="shrink-0 inline-flex items-center justify-center h-6 w-6 rounded text-2xs font-mono font-bold bg-surface-100 text-surface-500"
              >
                {{ getMetaTypeIcon(meta.value_type) }}
              </span>

              <!-- Content -->
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2">
                  <p class="text-sm font-medium text-surface-700">{{ meta.metadata_key }}</p>
                  <span class="badge text-2xs bg-surface-100 text-surface-500">{{ meta.source }}</span>
                  <span
                    v-if="meta.confidence != null"
                    class="text-2xs font-mono text-surface-400"
                  >
                    {{ (meta.confidence * 100).toFixed(0) }}%
                  </span>
                </div>
                <p class="mt-0.5 text-sm text-surface-600 break-all font-mono">
                  {{ meta.metadata_value ?? '(空值)' }}
                </p>
                <p class="mt-1 text-2xs text-surface-400">
                  {{ formatTime(meta.created_at) }}
                </p>
              </div>

              <!-- Delete button -->
              <button
                class="shrink-0 rounded p-1 text-surface-400 hover:bg-red-50 hover:text-red-500 transition-colors"
                title="删除元数据"
                @click="handleDeleteMetadata(meta.asset_metadata_id)"
              >
                <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
                </svg>
              </button>
            </div>
          </div>

          <!-- Empty metadata -->
          <div
            v-else-if="metadataList"
            class="flex flex-col items-center justify-center py-8 text-sm text-surface-400"
          >
            暂无元数据
          </div>
        </div>
      </template>

      <!-- Empty detail -->
      <div v-else class="flex flex-col items-center justify-center py-12 text-sm text-surface-400">
        加载资产详情失败
      </div>

      <!-- Footer -->
      <template #footer>
        <div class="flex items-center justify-between">
          <span class="text-2xs text-surface-400">
            ID: {{ selectedAssetId ? selectedAssetId.slice(0, 8) + '...' : '—' }}
          </span>
          <button
            class="btn btn-secondary btn-sm"
            @click="drawerOpen = false"
          >
            关闭
          </button>
        </div>
      </template>
    </DetailDrawer>
  </div>
</template>
