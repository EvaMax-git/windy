<script setup lang="ts">
/**
 * AssetTab — 知识库导入核心交互
 *
 * 完整路径:
 *   1. 选择项目 → 拖拽/选择文件
 *   2. 文件列表显示 (文件名+格式+大小)
 *   3. 一键上传 → 弹出 管道+子库 选择面板
 *   4. 提交处理后显示进度 (排队→处理中→完成/失败)
 *   5. 处理完成可跳转知识库搜索结果
 *   6. 空状态引导页
 */
import { ref, computed, watch, onMounted, onBeforeUnmount } from "vue";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { useRouter, useRoute } from "vue-router";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import { useToast } from "@/composables/useToast";
import {
  fetchAssets,
  fetchAsset,
  fetchAssetMetadata,
  addAssetMetadata,
  deleteAssetMetadata,
  deleteAsset,
  restoreAsset,
  uploadAsset,
  fetchProjects,
  fetchIngestPipelines,
  fetchIngestStores,
  submitIngestProcess,
  fetchIngestStatus,
} from "@/api/client";
import type {
  Asset,
  AssetMetadata,
  ProjectRead,
  IngestPipeline,
  IngestStore,
  IngestProcessItem,
} from "@/types";

const router = useRouter();
const route = useRoute();
const queryClient = useQueryClient();

const toast = useToast();

// ── Project selection ──
const selectedProjectId = ref("");
const selectedProject = ref<ProjectRead | null>(null);

const { data: projectsData } = useQuery({
  queryKey: ["projects"],
  queryFn: () => fetchProjects({ page: 1, page_size: 200 }),
});

const projects = computed(() => projectsData.value?.items ?? []);

// Read project_id from query param (from Dashboard [导入数据] button)
onMounted(() => {
  const pid = route.query.project_id as string;
  if (pid) {
    selectedProjectId.value = pid;
  }
  _restoreUploads();
});

watch(selectedProjectId, (newPid) => {
  if (newPid) {
    selectedProject.value = projects.value.find((p) => p.project_id === newPid) ?? null;
  } else {
    selectedProject.value = null;
  }
});

// ── Asset list query ──
const page = ref(1);
const pageSize = ref(50);

const listKey = computed(() => [
  "assets",
  page.value,
  pageSize.value,
  selectedProjectId.value,
] as const);

const { data: assetData, isLoading: assetsLoading, isError, error } = useQuery({
  queryKey: listKey,
  queryFn: () =>
    fetchAssets({
      page: page.value,
      page_size: pageSize.value,
      project_id: selectedProjectId.value || undefined,
    }),
  enabled: computed(() => !!selectedProjectId.value),
  placeholderData: (prev) => prev,
});

const totalAssets = computed(() => assetData.value?.page_info?.total_items ?? 0);

// ── Upload state ──
interface UploadFileItem {
  file: File;
  progress: number;
  status: "pending" | "uploading" | "succeeded" | "failed";
  error?: string;
  assetId?: string;
}

const MAX_FILE_SIZE = 500 * 1024 * 1024; // 500MB
const ALLOWED_EXTENSIONS: Record<string, string[]> = {
  "PDF 文档": [".pdf"],
  "Markdown": [".md"],
  "纯文本": [".txt", ".text"],
  "JSON 数据": [".json"],
  "聊天导出": [".csv", ".html"],
  "Python": [".py"],
  "TypeScript/JS": [".ts", ".tsx", ".js", ".jsx"],
  "Go": [".go"],
  "Rust": [".rs"],
  "Java": [".java"],
  "C/C++": [".c", ".cpp", ".h"],
  "其他代码": [".rb", ".php", ".swift", ".kt", ".cs"],
};

const allExtensions = computed(() => {
  const all: string[] = [];
  for (const exts of Object.values(ALLOWED_EXTENSIONS)) {
    all.push(...exts);
  }
  return all;
});

const acceptString = computed(() => allExtensions.value.join(","));

const fileInput = ref<HTMLInputElement | null>(null);
const isDragging = ref(false);
const files = ref<UploadFileItem[]>([]);
const isUploading = ref(false);
const uploadAllProgress = ref(0);
const showUploader = ref(true);

// ── Persist uploaded assets across page refreshes ──
const UPLOAD_STORAGE_KEY = "mneme_asset_uploads";

function _persistState() {
  const payload = {
    projectId: selectedProjectId.value,
    pipeline: selectedPipeline.value,
    stores: selectedStores.value,
    isProcessing: isProcessing.value,
    processingRunId: processingRunId.value,
    files: files.value.map((f) => ({
      fileName: f.file.name,
      fileSize: f.file.size,
      fileType: f.file.type || "application/octet-stream",
      status: f.status,
      progress: f.progress,
      assetId: f.assetId,
      error: f.error,
    })),
    processingItems: processingItems.value.map((p) => ({
      asset_id: p.asset_id,
      status: p.status,
      title: p.title,
      original_filename: p.original_filename,
    })),
  };
  localStorage.setItem(UPLOAD_STORAGE_KEY, JSON.stringify(payload));
}

function _restoreUploads() {
  try {
    const raw = localStorage.getItem(UPLOAD_STORAGE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);

    if (saved.projectId) selectedProjectId.value = saved.projectId;
    if (saved.pipeline) selectedPipeline.value = saved.pipeline;
    if (saved.stores) selectedStores.value = saved.stores;

    if (saved.files && saved.files.length) {
      files.value = saved.files.map((f: any) => ({
        file: new File([], f.fileName, { type: f.fileType }),
        progress: f.progress,
        status: f.status as "pending" | "uploading" | "succeeded" | "failed",
        assetId: f.assetId,
        error: f.error,
      }));
      showUploader.value = true;
    }

    if (saved.processingItems && saved.processingItems.length) {
      processingItems.value = saved.processingItems.map((p: any) => ({
        ...p,
        progress_percent: _calcProgress(p.status),
      }));
      if (saved.isProcessing) {
        isProcessing.value = true;
        processingRunId.value = saved.processingRunId || "";
        startPolling();
      }
    }
  } catch {
    _clearPersistedUploads();
  }
}

function _clearPersistedUploads() {
  localStorage.removeItem(UPLOAD_STORAGE_KEY);
}

function getFileCategory(fileName: string): string {
  const ext = "." + fileName.split(".").pop()?.toLowerCase();
  for (const [cat, exts] of Object.entries(ALLOWED_EXTENSIONS)) {
    if (exts.includes(ext)) return cat;
  }
  return "其他文件";
}

function getFileIcon(cat: string): string {
  const map: Record<string, string> = {
    "PDF 文档": "📕",
    "Markdown": "📝",
    "纯文本": "📃",
    "JSON 数据": "📋",
    "聊天导出": "💬",
    "Python": "🐍",
    "TypeScript/JS": "⚡",
    "Go": "🔵",
    "Rust": "🦀",
    "Java": "☕",
    "C/C++": "⚙️",
    "其他代码": "💻",
    "其他文件": "📎",
  };
  return map[cat] || "📎";
}

function formatSize(bytes: number | null): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function validateFile(file: File): string | null {
  const ext = "." + file.name.split(".").pop()?.toLowerCase();
  if (!allExtensions.value.includes(ext)) {
    return `不支持的文件类型: ${ext}`;
  }
  if (file.size > MAX_FILE_SIZE) {
    return `文件过大 (最大 ${formatSize(MAX_FILE_SIZE)})`;
  }
  if (file.size === 0) {
    return "文件为空";
  }
  return null;
}

function handleFiles(newFiles: FileList | File[]) {
  const fileArray = Array.from(newFiles);
  for (const file of fileArray) {
    const error = validateFile(file);
    files.value.push({
      file,
      progress: 0,
      status: error ? "failed" : "pending",
      error: error || undefined,
    });
  }
  showUploader.value = true;
}

function onDragOver(e: DragEvent) {
  e.preventDefault();
  if (selectedProjectId.value) isDragging.value = true;
}

function onDragLeave() {
  isDragging.value = false;
}

function onDrop(e: DragEvent) {
  e.preventDefault();
  isDragging.value = false;
  if (e.dataTransfer?.files && selectedProjectId.value) {
    handleFiles(e.dataTransfer.files);
  }
}

function onFileInputChange(e: Event) {
  const input = e.target as HTMLInputElement;
  if (input.files) handleFiles(input.files);
  input.value = "";
}

function removeFile(index: number) {
  files.value.splice(index, 1);
}

function clearFiles() {
  files.value = [];
}

// ── Upload to backend ──
async function uploadAllFiles() {
  if (isUploading.value || !selectedProjectId.value) return;
  isUploading.value = true;
  let completed = 0;
  const pending = files.value.filter((f) => f.status === "pending");
  const total = pending.length;

  for (const item of pending) {
    item.status = "uploading";
    item.progress = 0;
    try {
      const asset = await uploadAsset(item.file, selectedProjectId.value, {
        onProgress: (pct) => { item.progress = pct; },
      });
      item.status = "succeeded";
      item.progress = 100;
      item.assetId = asset.asset_id;
    } catch (err: any) {
      item.status = "failed";
      item.error = err?.message || "上传失败";
      toast.error(`「${item.file.name}」上传失败`, {
        message: err?.message || "请检查网络连接后重试",
      });
    }
    completed++;
    uploadAllProgress.value = Math.round((completed / total) * 100);
  }
  isUploading.value = false;
  const succeeded = files.value.filter((f) => f.status === "succeeded").length;
  const failed = files.value.filter((f) => f.status === "failed").length;
  if (succeeded > 0 && failed === 0) {
    toast.success(`全部上传成功`, {
      message: `${succeeded} 个文件已完成上传`,
    });
  } else if (failed > 0 && succeeded === 0) {
    toast.error(`上传全部失败`, {
      message: `${failed} 个文件均上传失败，请检查后重试`,
    });
  } else if (failed > 0 && succeeded > 0) {
    toast.warning(`部分上传成功`, {
      message: `${succeeded} 个成功，${failed} 个失败`,
    });
  }
  _persistState();
  queryClient.invalidateQueries({ queryKey: ["assets"] });
}

const uploadedAssetIds = computed(() =>
  files.value.filter((f) => f.status === "succeeded" && f.assetId).map((f) => f.assetId!)
);

// ── Pipeline & Store Selection Panel ──
const showPipelinePanel = ref(false);

const { data: pipelinesData } = useQuery({
  queryKey: ["ingest-pipelines"],
  queryFn: fetchIngestPipelines,
  staleTime: 60_000,
  enabled: showPipelinePanel,
  retry: 3,
});

const { data: storesData } = useQuery({
  queryKey: ["ingest-stores"],
  queryFn: fetchIngestStores,
  staleTime: 60_000,
  enabled: showPipelinePanel,
  retry: 3,
});

const selectedPipeline = ref("");
const selectedStores = ref<string[]>([]);

function getSuggestedPipeline(): string {
  if (!files.value.length) return "standard_chunk";
  const exts = files.value.map((f) => "." + f.file.name.split(".").pop()?.toLowerCase());
  const codeExts = [".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".cpp", ".c", ".h", ".rb", ".php", ".swift", ".kt", ".cs"];
  const hasCode = exts.some((e) => codeExts.includes(e));
  if (hasCode) return "code_parse";
  const hasImage = exts.some((e) => [".png", ".jpg", ".jpeg", ".webp", ".tiff"].includes(e));
  if (hasImage) return "ocr_document";
  const hasChat = exts.some((e) => [".csv", ".html"].includes(e));
  if (hasChat) return "dialog_parse";
  return "standard_chunk";
}

function openPipelinePanel() {
  selectedPipeline.value = getSuggestedPipeline();
  showPipelinePanel.value = true;
}

// Auto-select default stores when storesData loads and panel is open
watch(storesData, (data) => {
  if (!data || !showPipelinePanel.value) return;
  if (selectedStores.value.length > 0) return; // preserve user selection
  const preferred = data.filter(
    (s) => s.type === "vector" || s.type === "fulltext"
  );
  selectedStores.value = preferred.length > 0
    ? preferred.map((s) => s.store_key)
    : data.map((s) => s.store_key);
});

function toggleStore(key: string) {
  const idx = selectedStores.value.indexOf(key);
  if (idx >= 0) {
    selectedStores.value.splice(idx, 1);
  } else {
    selectedStores.value.push(key);
  }
}

// ── Processing state ──
const processingItems = ref<IngestProcessItem[]>([]);
const isProcessing = ref(false);
const processingRunId = ref("");
let _pollTimer: ReturnType<typeof setInterval> | null = null;

/** Map asset_id → file info, saved before files are cleared */
const _fileInfoMap = new Map<string, { title: string; originalFilename: string }>();

function _calcProgress(status: string): number {
  switch (status) {
    case "completed": return 100;
    case "failed": return 100;
    case "processing": return 60;
    case "queued": return 10;
    default: return 0;
  }
}

async function submitProcess() {
  if (!selectedPipeline.value || !selectedStores.value.length || !selectedProjectId.value) return;
  isProcessing.value = true;
  showPipelinePanel.value = false;

  // Save file info before clearing
  _fileInfoMap.clear();
  for (const f of files.value) {
    if (f.assetId) {
      _fileInfoMap.set(f.assetId, {
        title: f.file.name,
        originalFilename: f.file.name,
      });
    }
  }

  try {
    const resp = await submitIngestProcess({
      asset_ids: uploadedAssetIds.value,
      pipeline_key: selectedPipeline.value,
      store_keys: selectedStores.value,
      project_id: selectedProjectId.value,
    });
    processingRunId.value = resp.run_id;
    processingItems.value = resp.items.map((item) => {
      const info = _fileInfoMap.get(item.asset_id);
      return {
        ...item,
        title: item.title || info?.title || `资产 ${item.asset_id.slice(0, 8)}`,
        original_filename: info?.originalFilename,
        progress_percent: _calcProgress(item.status),
      };
    });

    // Start polling for status updates
    clearFiles();
    _persistState();
    startPolling();
  } catch (err: any) {
    console.error("Process submit failed", err);
    toast.error("提交处理任务失败", {
      message: err?.message || "请检查管道和子库配置后重试",
    });
    isProcessing.value = false;
  }
}

function startPolling() {
  if (_pollTimer) clearInterval(_pollTimer);
  _pollTimer = setInterval(pollStatus, 2000);
  pollStatus();
}

async function pollStatus() {
  const ids = processingItems.value.map((p) => p.asset_id);
  if (!ids.length) {
    stopPolling();
    return;
  }
  try {
    const statuses = await fetchIngestStatus(ids);
    // Preserve titles and original_filenames from existing items, add progress
    const prevMap = new Map(processingItems.value.map((p) => [p.asset_id, p]));
    processingItems.value = statuses.map((s) => {
      const prev = prevMap.get(s.asset_id);
      return {
        ...s,
        title: s.title || prev?.title || `资产 ${s.asset_id.slice(0, 8)}`,
        original_filename: s.original_filename || prev?.original_filename,
        progress_percent: _calcProgress(s.status),
      };
    });
    // Refresh asset list
    queryClient.invalidateQueries({ queryKey: ["assets"] });

    const allDone = statuses.every((s) => s.status === "completed" || s.status === "failed");
    if (allDone) {
      const completed = statuses.filter((s) => s.status === "completed").length;
      const failed = statuses.filter((s) => s.status === "failed").length;
      if (completed > 0 && failed === 0) {
        toast.success("全部处理完成", {
          message: `${completed} 个文件已成功导入知识库`,
        });
      } else if (failed > 0 && completed > 0) {
        toast.warning("部分处理完成", {
          message: `${completed} 个成功，${failed} 个失败`,
        });
      } else if (failed > 0 && completed === 0) {
        toast.error("处理全部失败", {
          message: `${failed} 个文件均处理失败`,
        });
      }
      stopPolling();
    }
  } catch (err) {
    console.error("Status poll failed", err);
  }
}

function stopPolling() {
  if (_pollTimer) {
    clearInterval(_pollTimer);
    _pollTimer = null;
  }
  isProcessing.value = false;
  _persistState();
}

function jumpToSearch(item: IngestProcessItem) {
  router.push({
    path: "/app/knowledge",
    query: { tab: "search", q: item.title, project_id: selectedProjectId.value },
  });
}

function jumpToKnowledge() {
  router.push({
    path: "/app/knowledge",
    query: { tab: "knowledge", project_id: selectedProjectId.value || undefined },
  });
}

// Cleanup polling on unmount
onBeforeUnmount(() => stopPolling());

// ── Asset detail drawer (existing functionality) ──
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

const { data: metadataList, refetch: refetchMetadata } = useQuery({
  queryKey: ["asset-metadata", selectedAssetId],
  queryFn: async () => {
    const result = await fetchAssetMetadata(selectedAssetId.value!);
    // API returns PaginatedData, extract items
    if (result && typeof result === 'object' && 'items' in result) {
      return (result as any).items as AssetMetadata[];
    }
    return result as unknown as AssetMetadata[];
  },
  enabled: computed(() => drawerOpen.value && !!selectedAssetId.value),
});

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

function formatTime(iso: string): string { return new Date(iso).toLocaleString(); }
function formatHash(hash: string): string { return hash.length <= 16 ? hash : hash.slice(0, 8) + "..." + hash.slice(-8); }
function assetTypeLabel(type: string): string {
  const map: Record<string, string> = { document: "文档", image: "图片", audio: "音频", video: "视频", archive: "压缩包", dataset: "数据集", note: "笔记", url: "链接", other: "其他" };
  return map[type] || type;
}

const SENSITIVITY_STYLES: Record<string, string> = {
  public: "bg-surface-100 text-surface-600", normal: "bg-blue-50 text-blue-700",
  private: "bg-amber-50 text-amber-700", sensitive: "bg-orange-50 text-orange-700", secret: "bg-red-50 text-red-700",
};
const SENSITIVITY_LABELS: Record<string, string> = {
  public: "公开", normal: "普通", private: "内部", sensitive: "敏感", secret: "机密",
};
const ASSET_TYPE_STYLES: Record<string, string> = {
  document: "bg-indigo-50 text-indigo-700", image: "bg-violet-50 text-violet-700", audio: "bg-pink-50 text-pink-700",
  video: "bg-rose-50 text-rose-700", archive: "bg-teal-50 text-teal-700", dataset: "bg-cyan-50 text-cyan-700",
  note: "bg-green-50 text-green-700", url: "bg-sky-50 text-sky-700", other: "bg-surface-100 text-surface-600",
};

function openDetail(asset: Asset) {
  selectedAssetId.value = asset.asset_id;
  drawerTab.value = "info";
  drawerOpen.value = true;
}

const metaToast = ref<{ msg: string; type: "success" | "error" } | null>(null);

function showMetaToast(msg: string, type: "success" | "error") {
  metaToast.value = { msg, type };
  setTimeout(() => { metaToast.value = null; }, 4000);
}

async function handleAddMetadata() {
  if (!selectedAssetId.value || !addMetaForm.value.key.trim()) return;
  metaSubmitting.value = true;
  try {
    await addAssetMetadata(selectedAssetId.value, { metadata_key: addMetaForm.value.key.trim(), metadata_value: addMetaForm.value.value, value_type: addMetaForm.value.type });
    addMetaForm.value = { key: "", value: "", type: "text" };
    await queryClient.invalidateQueries({ queryKey: ["asset-metadata", selectedAssetId.value] });
    await refetchMetadata();
    showMetaToast("元数据已添加", "success");
  } catch (e: unknown) {
    showMetaToast((e as Error)?.message || "添加元数据失败", "error");
  } finally { metaSubmitting.value = false; }
}

async function handleDeleteMetadata(metaId: string) {
  if (!selectedAssetId.value || !confirm("确定要删除此元数据条目？")) return;
  try {
    await deleteAssetMetadata(selectedAssetId.value, metaId);
    await queryClient.invalidateQueries({ queryKey: ["asset-metadata", selectedAssetId.value] });
    await refetchMetadata();
    showMetaToast("元数据已删除", "success");
  } catch (e: unknown) {
    showMetaToast((e as Error)?.message || "删除元数据失败", "error");
  }
}

async function handleDeleteAsset() {
  if (!selectedAssetId.value || !confirm("确定要删除此资产？（软删除，可恢复）")) return;
  actionLoading.value = true;
  try {
    await deleteAsset(selectedAssetId.value);
    queryClient.invalidateQueries({ queryKey: ["assets"] });
    queryClient.invalidateQueries({ queryKey: ["asset", selectedAssetId.value] });
  } catch (e) { console.error(e); } finally { actionLoading.value = false; }
}

async function handleRestoreAsset() {
  if (!selectedAssetId.value || !confirm("确定要恢复此资产？")) return;
  actionLoading.value = true;
  try {
    await restoreAsset(selectedAssetId.value);
    queryClient.invalidateQueries({ queryKey: ["assets"] });
    queryClient.invalidateQueries({ queryKey: ["asset", selectedAssetId.value] });
  } catch (e) { console.error(e); } finally { actionLoading.value = false; }
}

function getMetaTypeIcon(type: string): string {
  const map: Record<string, string> = { text: "Aa", number: "123", boolean: "T/F", date: "📅", json: "{}" };
  return map[type] || "?";
}

// ── Status helpers for processing items ──
function processStatusLabel(status: string): string {
  const map: Record<string, string> = { queued: "排队中", processing: "处理中", completed: "已完成", failed: "失败" };
  return map[status] || status;
}

function processStatusColor(status: string): string {
  const map: Record<string, string> = { queued: "text-surface-400", processing: "text-brand-600", completed: "text-emerald-600", failed: "text-red-600" };
  return map[status] || "text-surface-400";
}

function processBarColor(status: string): string {
  const map: Record<string, string> = { queued: "bg-surface-300", processing: "bg-brand-500", completed: "bg-emerald-500", failed: "bg-red-400" };
  return map[status] || "bg-surface-300";
}

function processStatusIcon(status: string): string {
  const map: Record<string, string> = {
    queued: `<svg class="h-4 w-4 text-surface-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path stroke-linecap="round" stroke-linejoin="round" d="M12 6v6l4 2"/></svg>`,
    processing: `<svg class="h-4 w-4 animate-spin text-brand-500" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>`,
    completed: `<svg class="h-4 w-4 text-emerald-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>`,
    failed: `<svg class="h-4 w-4 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>`,
  };
  return map[status] || "";
}
</script>

<template>
  <div>
    <!-- ═══════════════════════════════════════════════ -->
    <!--  Project Selector -->
    <!-- ═══════════════════════════════════════════════ -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-end gap-3">
        <div class="flex flex-col gap-1 flex-1 min-w-[240px]">
          <label class="text-2xs font-semibold uppercase text-surface-400">目标项目</label>
          <select
            v-model="selectedProjectId"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">— 选择项目 —</option>
            <option v-for="p in projects" :key="p.project_id" :value="p.project_id">
              {{ p.name }} ({{ p.project_code }})
            </option>
          </select>
        </div>
        <span class="text-xs text-surface-400 self-end mb-1">
          {{ totalAssets }} 个资产
        </span>
      </div>
    </div>

    <!-- ═══════════════════════════════════════════════ -->
    <!--  Empty State Guide (no project or no assets) -->
    <!-- ═══════════════════════════════════════════════ -->
    <div
      v-if="!selectedProjectId"
      class="card border-dashed border-2 border-surface-200 bg-white overflow-hidden"
    >
      <!-- If no project selected yet -->
      <div v-if="!selectedProjectId" class="flex flex-col items-center py-16 px-4 text-center">
        <div class="mb-4 text-6xl">📦</div>
        <h3 class="text-lg font-semibold text-surface-700 mb-2">欢迎使用知识库导入</h3>
        <p class="text-sm text-surface-400 max-w-md mb-6">
          请先在上方选择一个目标项目，然后拖拽文件到此处开始导入。
          支持 PDF、Markdown、纯文本、聊天导出、代码文件等格式。
        </p>
        <div class="flex items-center gap-2 text-xs text-surface-400">
          <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" stroke-linejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
          </svg>
          也支持从 Dashboard [导入数据] 快捷入口直达
        </div>
      </div>

      <!-- Has project but no assets yet -->
      <div v-else>
        <!-- Drag & Drop Zone -->
        <div
          :class="[
            'relative m-4 rounded-xl border-2 border-dashed p-10 text-center transition-all duration-200 cursor-pointer',
            isDragging
              ? 'border-brand-400 bg-brand-50/80 scale-[1.01]'
              : 'border-surface-200 hover:border-brand-300 hover:bg-surface-50',
          ]"
          @dragover="onDragOver"
          @dragleave="onDragLeave"
          @drop="onDrop"
          @click="fileInput?.click()"
        >
          <input
            ref="fileInput"
            type="file"
            :accept="acceptString"
            multiple
            class="hidden"
            @change="onFileInputChange"
          />

          <div class="flex flex-col items-center gap-4">
            <div
              :class="[
                'flex h-16 w-16 items-center justify-center rounded-2xl transition-all',
                isDragging ? 'bg-brand-100 text-brand-600 scale-110' : 'bg-surface-100 text-surface-400',
              ]"
            >
              <svg xmlns="http://www.w3.org/2000/svg" class="h-8 w-8" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
                <path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5"/>
              </svg>
            </div>
            <div>
              <p class="text-base font-medium text-surface-700">
                {{ isDragging ? '✨ 松开文件即开始导入' : '拖拽文件到此处开始导入' }}
              </p>
              <p class="mt-2 text-sm text-surface-400">
                支持 PDF · Markdown · 纯文本 · 聊天导出 · 代码文件
              </p>
              <p class="mt-1 text-xs text-surface-300">
                单文件最大 {{ formatSize(MAX_FILE_SIZE) }}，可一次选择多个文件
              </p>
            </div>
          </div>
        </div>

        <!-- Supported formats quick reference -->
        <div class="px-4 pb-4">
          <div class="flex flex-wrap gap-1.5 justify-center">
            <span v-for="(exts, cat) in ALLOWED_EXTENSIONS" :key="cat"
              class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-2xs bg-surface-50 text-surface-500 border border-surface-100"
            >
              <span>{{ getFileIcon(cat) }}</span>
              {{ cat }}
            </span>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══════════════════════════════════════════════ -->
    <!--  File Upload Area (when project selected) -->
    <!-- ═══════════════════════════════════════════════ -->
    <div
      v-if="selectedProjectId && !isProcessing"
      class="card mb-6 overflow-hidden"
    >
      <!-- Drop zone — prominent when no files yet -->
      <div
        v-if="files.length === 0"
        :class="[
          'relative m-4 rounded-xl border-2 border-dashed p-10 text-center transition-all duration-200 cursor-pointer',
          isDragging
            ? 'border-brand-400 bg-brand-50/80 scale-[1.01]'
            : 'border-surface-200 hover:border-brand-300 hover:bg-surface-50',
        ]"
        @dragover="onDragOver"
        @dragleave="onDragLeave"
        @drop="onDrop"
        @click="fileInput?.click()"
      >
        <input
          ref="fileInput"
          type="file"
          :accept="acceptString"
          multiple
          class="hidden"
          @change="onFileInputChange"
        />

        <div class="flex flex-col items-center gap-4">
          <div
            :class="[
              'flex h-16 w-16 items-center justify-center rounded-2xl transition-all',
              isDragging ? 'bg-brand-100 text-brand-600 scale-110' : 'bg-surface-100 text-surface-400',
            ]"
          >
            <svg xmlns="http://www.w3.org/2000/svg" class="h-8 w-8" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5"/>
            </svg>
          </div>
          <div>
            <p class="text-base font-medium text-surface-700">
              {{ isDragging ? '✨ 松开文件即开始导入' : '拖拽文件到此处开始导入' }}
            </p>
            <p class="mt-2 text-sm text-surface-400">
              支持 PDF · Markdown · 纯文本 · 聊天导出 · 代码文件
            </p>
            <p class="mt-1 text-xs text-surface-300">
              单文件最大 {{ formatSize(MAX_FILE_SIZE) }}，可一次选择多个文件
            </p>
          </div>
        </div>
      </div>

      <!-- Supported formats quick reference (initial state) -->
      <div v-if="files.length === 0" class="px-4 pb-4">
        <div class="flex flex-wrap gap-1.5 justify-center">
          <span v-for="(exts, cat) in ALLOWED_EXTENSIONS" :key="cat"
            class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-2xs bg-surface-50 text-surface-500 border border-surface-100"
          >
            <span>{{ getFileIcon(cat) }}</span>
            {{ cat }}
          </span>
        </div>
      </div>

      <!-- File list -->
      <div v-if="files.length > 0" class="divide-y divide-surface-100">
        <div
          v-for="(item, idx) in files"
          :key="idx"
          class="flex items-center gap-3 px-4 py-3 hover:bg-surface-50 transition-colors"
        >
          <!-- File icon -->
          <span class="text-xl shrink-0">{{ getFileIcon(getFileCategory(item.file.name)) }}</span>

          <!-- File info -->
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <p class="text-sm font-medium text-surface-700 truncate">{{ item.file.name }}</p>
              <span class="text-2xs shrink-0 rounded px-1.5 py-0.5 bg-surface-100 text-surface-500">
                {{ getFileCategory(item.file.name) }}
              </span>
            </div>
            <p class="text-xs text-surface-400">{{ formatSize(item.file.size) }}</p>

            <!-- Upload progress -->
            <div v-if="item.status === 'uploading'" class="mt-1.5 h-1 rounded-full bg-surface-100 overflow-hidden w-48">
              <div class="h-full rounded-full bg-brand-500 transition-all duration-300" :style="{ width: item.progress + '%' }"></div>
            </div>

            <!-- Status messages -->
            <p v-if="item.status === 'succeeded'" class="mt-0.5 text-xs text-emerald-600">✓ 上传成功</p>
            <p v-if="item.error" class="mt-0.5 text-xs text-red-500">{{ item.error }}</p>
          </div>

          <!-- Remove -->
          <button
            v-if="item.status !== 'uploading'"
            class="shrink-0 rounded p-1.5 text-surface-400 hover:bg-red-50 hover:text-red-500 transition-colors"
            @click="removeFile(idx)"
          >
            <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/>
            </svg>
          </button>
        </div>

        <!-- Actions bar -->
        <div class="flex items-center justify-between px-4 py-3 bg-surface-50">
          <button class="text-xs text-surface-400 hover:text-surface-600" @click="clearFiles">清空列表</button>
          <div class="flex items-center gap-2">
            <!-- Add more files -->
            <div class="relative">
              <input ref="fileInput" type="file" :accept="acceptString" multiple class="hidden" @change="onFileInputChange" />
              <button class="btn btn-secondary btn-sm" @click="fileInput?.click()">+ 添加文件</button>
            </div>
            <!-- Upload button -->
            <button
              class="btn btn-primary btn-sm"
              :disabled="isUploading || !files.some(f => f.status === 'pending')"
              @click="uploadAllFiles"
            >
              <svg v-if="isUploading" class="h-3.5 w-3.5 mr-1 animate-spin" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
              </svg>
              {{ isUploading ? `上传中... ${uploadAllProgress}%` : `上传文件 (${files.filter(f => f.status === 'pending').length})` }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══════════════════════════════════════════════ -->
    <!--  "下一步" — Pipeline & Store Selection -->
    <!-- ═══════════════════════════════════════════════ -->
    <div
      v-if="selectedProjectId && !isProcessing"
      class="card mb-6 p-4 bg-brand-50/50 border-brand-200"
    >
      <div class="flex items-start gap-3">
        <div class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-100 text-brand-600">
          <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" stroke-linejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z"/>
          </svg>
        </div>
        <div class="flex-1 min-w-0">
          <p class="text-sm font-semibold text-brand-700">
            {{ uploadedAssetIds.length > 0 ? `${uploadedAssetIds.length} 个文件上传成功，请选择处理方式` : '请选择处理管道和目标子库' }}
          </p>
          <p class="text-xs text-brand-500 mt-0.5">选择处理管道和目标子库，然后上传文件开始导入知识库</p>
        </div>
        <button class="btn btn-primary btn-sm shrink-0" @click="openPipelinePanel">
          选择管道 & 子库 →
        </button>
      </div>
    </div>

    <!-- ═══════════════════════════════════════════════ -->
    <!--  Processing Progress Panel -->
    <!-- ═══════════════════════════════════════════════ -->
    <div v-if="processingItems.length > 0" class="card mb-6 overflow-hidden">
      <div class="px-4 py-3 border-b border-surface-100 bg-surface-50 flex items-center justify-between">
        <div class="flex items-center gap-2">
          <svg
            v-if="processingItems.some(p => p.status === 'processing' || p.status === 'queued')"
            class="h-4 w-4 animate-spin text-brand-500" fill="none" viewBox="0 0 24 24"
          >
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
          </svg>
          <span class="text-sm font-semibold text-surface-700">
            处理进度 ({{ processingItems.filter(p => p.status === 'completed').length }}/{{ processingItems.length }})
          </span>
        </div>
        <button
          v-if="processingItems.every(p => p.status === 'completed' || p.status === 'failed')"
          class="text-xs text-surface-400 hover:text-surface-600"
          @click="processingItems = []"
        >
          清除
        </button>
      </div>

      <div class="divide-y divide-surface-100">
        <div
          v-for="item in processingItems"
          :key="item.asset_id"
          class="px-4 py-3 flex items-center gap-3"
        >
          <!-- Status icon -->
          <span v-html="processStatusIcon(item.status)"></span>

          <!-- Info -->
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <p class="text-sm text-surface-700 truncate">{{ item.title }}</p>
              <span :class="['text-2xs font-medium', processStatusColor(item.status)]">
                {{ processStatusLabel(item.status) }}
              </span>
            </div>
            <p v-if="item.original_filename" class="text-2xs text-surface-400 truncate">{{ item.original_filename }}</p>

            <!-- Progress bar -->
            <div class="mt-1.5 h-1.5 rounded-full bg-surface-100 overflow-hidden w-48">
              <div
                :class="['h-full rounded-full transition-all duration-500', processBarColor(item.status)]"
                :style="{ width: item.progress_percent + '%' }"
              ></div>
            </div>

            <!-- Error -->
            <p v-if="item.error" class="mt-1 text-xs text-red-500">{{ item.error }}</p>
          </div>

          <!-- Jump to search / knowledge when completed -->
          <div v-if="item.status === 'completed'" class="flex items-center gap-1 shrink-0">
            <button
              class="btn btn-ghost btn-xs text-xs text-brand-600"
              @click="jumpToKnowledge()"
            >
              📄 查看文档
            </button>
            <button
              class="btn btn-primary btn-xs"
              @click="jumpToSearch(item)"
            >
              🔍 查看结果
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══════════════════════════════════════════════ -->
    <!--  Error -->
    <!-- ═══════════════════════════════════════════════ -->
    <div v-if="isError && selectedProjectId" class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700">
      加载资产失败: {{ (error as Error)?.message }}
    </div>

    <!-- ═══════════════════════════════════════════════ -->
    <!--  Asset Table (when data exists) -->
    <!-- ═══════════════════════════════════════════════ -->
    <DataTable
      v-if="selectedProjectId && totalAssets > 0"
      :items="assetData?.items ?? []"
      :columns="columns"
      :loading="assetsLoading"
      empty-message="暂无资产"
      clickable
      row-key="asset_id"
      @row-click="openDetail"
    >
      <template #cell-title="{ value, item }">
        <div class="flex items-center gap-2 min-w-0">
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

      <template #cell-asset_uid="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ (value as string).slice(0, 20) }}…</span>
      </template>

      <template #cell-asset_type="{ value }">
        <span :class="['badge text-2xs font-medium', ASSET_TYPE_STYLES[value as string] || 'bg-surface-100 text-surface-600']">
          {{ assetTypeLabel(value as string) }}
        </span>
      </template>

      <template #cell-status="{ value, item }">
        <StatusBadge :status="value as string" />
        <span v-if="(item as Asset).knowledge_state && (item as Asset).knowledge_state !== 'not_started'" class="ml-1 text-2xs text-surface-400">
          · K:{{ (item as Asset).knowledge_state }}
        </span>
      </template>

      <template #cell-sensitivity_level="{ value }">
        <span :class="['badge text-2xs font-medium', SENSITIVITY_STYLES[value as string] || 'bg-surface-100 text-surface-600']">
          {{ SENSITIVITY_LABELS[value as string] || value }}
        </span>
      </template>

      <template #cell-ingest_state="{ value }">
        <StatusBadge :status="value as string" />
      </template>

      <template #cell-size_bytes="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ formatSize(value as number | null) }}</span>
      </template>

      <template #cell-created_at="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
      </template>
    </DataTable>

    <Pagination
      v-if="assetData?.page_info && totalAssets > 0"
      :page-info="assetData.page_info"
      @page-change="(p: number) => (page = p)"
    />

    <!-- ═══════════════════════════════════════════════ -->
    <!--  Detail Drawer -->
    <!-- ═══════════════════════════════════════════════ -->
    <DetailDrawer :open="drawerOpen" title="资产详情" width="w-[540px] max-w-full" @close="drawerOpen = false">
      <div v-if="detailLoading" class="py-4"><LoadingSkeleton variant="detail" /></div>

      <template v-else-if="assetDetail">
        <div class="flex items-center gap-1 border-b border-surface-200 mb-4">
          <button
            :class="['px-3 py-2 text-sm font-medium border-b-2 transition-colors -mb-px', drawerTab === 'info' ? 'border-brand-500 text-brand-600' : 'border-transparent text-surface-500 hover:text-surface-700']"
            @click="drawerTab = 'info'"
          >基本信息</button>
          <button
            :class="['px-3 py-2 text-sm font-medium border-b-2 transition-colors -mb-px', drawerTab === 'metadata' ? 'border-brand-500 text-brand-600' : 'border-transparent text-surface-500 hover:text-surface-700']"
            @click="drawerTab = 'metadata'"
          >元数据 ({{ metadataList?.length ?? 0 }})</button>
        </div>

        <div v-if="drawerTab === 'info'">
          <dl class="space-y-4">
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">资产标识</dt><dd class="mt-1 font-mono text-xs text-surface-700 break-all">{{ assetDetail.asset_uid }}</dd></div>
            <div class="grid grid-cols-2 gap-4">
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">标题</dt><dd class="mt-1 text-sm text-surface-700">{{ assetDetail.title }}</dd></div>
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">类型</dt><dd class="mt-1"><span :class="['badge text-2xs font-medium', ASSET_TYPE_STYLES[assetDetail.asset_type] || 'bg-surface-100 text-surface-600']">{{ assetTypeLabel(assetDetail.asset_type) }}</span></dd></div>
            </div>
            <div class="grid grid-cols-2 gap-4">
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">状态</dt><dd class="mt-1"><StatusBadge :status="assetDetail.status" /></dd></div>
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">敏感等级</dt><dd class="mt-1"><span :class="['badge text-2xs font-medium', SENSITIVITY_STYLES[assetDetail.sensitivity_level] || 'bg-surface-100 text-surface-600']">{{ SENSITIVITY_LABELS[assetDetail.sensitivity_level] || assetDetail.sensitivity_level }}</span></dd></div>
            </div>
            <div class="grid grid-cols-2 gap-4">
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">入库状态</dt><dd class="mt-1"><StatusBadge :status="assetDetail.ingest_state" /></dd></div>
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">知识状态</dt><dd class="mt-1"><StatusBadge :status="assetDetail.knowledge_state" /></dd></div>
            </div>
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">原始文件名</dt><dd class="mt-1 text-xs text-surface-700">{{ assetDetail.original_filename || '—' }}</dd></div>
            <div v-if="assetDetail.media_type"><dt class="text-2xs font-semibold uppercase text-surface-400">MIME</dt><dd class="mt-1 font-mono text-xs text-surface-500">{{ assetDetail.media_type }}</dd></div>
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">文件大小</dt><dd class="mt-1 font-mono text-xs">{{ formatSize(assetDetail.size_bytes) }}</dd></div>
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">内容哈希</dt><dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ formatHash(assetDetail.content_hash) }}</dd></div>
            <div><dt class="text-2xs font-semibold uppercase text-surface-400">存储引用</dt><dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ assetDetail.storage_ref }}</dd></div>
            <div v-if="assetDetail.canonical_uri"><dt class="text-2xs font-semibold uppercase text-surface-400">规范URI</dt><dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ assetDetail.canonical_uri }}</dd></div>
            <div class="grid grid-cols-2 gap-4">
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt><dd class="mt-1 text-xs">{{ formatTime(assetDetail.created_at) }}</dd></div>
              <div><dt class="text-2xs font-semibold uppercase text-surface-400">更新时间</dt><dd class="mt-1 text-xs">{{ formatTime(assetDetail.updated_at) }}</dd></div>
            </div>
          </dl>
          <div class="mt-6 pt-4 border-t border-surface-200 flex items-center gap-2">
            <button v-if="assetDetail.status === 'deleted' || assetDetail.status === 'archived'" class="btn btn-secondary btn-sm" :disabled="actionLoading" @click="handleRestoreAsset">恢复资产</button>
            <button v-if="assetDetail.status === 'active'" class="btn btn-danger btn-sm" :disabled="actionLoading" @click="handleDeleteAsset">删除资产</button>
          </div>
        </div>

        <div v-if="drawerTab === 'metadata'">
          <div v-if="metaToast" :class="['mb-4 px-3 py-2 rounded-lg text-xs font-medium', metaToast.type === 'success' ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' : 'bg-red-50 text-red-700 border border-red-200']">{{ metaToast.msg }}</div>
          <div class="rounded-lg border border-surface-200 bg-surface-50 p-4 mb-4">
            <p class="text-xs font-semibold text-surface-700 mb-3">添加元数据</p>
            <div class="space-y-3">
              <div class="grid grid-cols-4 gap-2">
                <input v-model="addMetaForm.key" type="text" placeholder="键名" class="col-span-1 rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                <input v-model="addMetaForm.value" type="text" placeholder="值" class="col-span-2 rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
                <select v-model="addMetaForm.type" class="col-span-1 rounded-lg border border-surface-200 bg-white px-2 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500">
                  <option value="text">文本</option><option value="number">数字</option><option value="boolean">布尔</option><option value="date">日期</option><option value="json">JSON</option>
                </select>
              </div>
              <button class="btn btn-primary btn-sm w-full" :disabled="metaSubmitting || !addMetaForm.key.trim()" data-testid="asset-metadata-add" @click="handleAddMetadata">{{ metaSubmitting ? '添加中...' : '添加元数据' }}</button>
            </div>
          </div>
          <div v-if="metadataList && metadataList.length > 0" class="divide-y divide-surface-100 rounded-lg border border-surface-200 overflow-hidden">
            <div v-for="meta in metadataList" :key="meta.asset_metadata_id" class="flex items-start gap-3 px-4 py-3 hover:bg-surface-50 transition-colors">
              <span class="shrink-0 inline-flex items-center justify-center h-6 w-6 rounded text-2xs font-mono font-bold bg-surface-100 text-surface-500">{{ getMetaTypeIcon(meta.value_type) }}</span>
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2"><p class="text-sm font-medium text-surface-700">{{ meta.metadata_key }}</p><span class="badge text-2xs bg-surface-100 text-surface-500">{{ meta.source }}</span></div>
                <p class="mt-0.5 text-sm text-surface-600 break-all font-mono">{{ meta.metadata_value ?? '(空值)' }}</p>
                <p class="mt-1 text-2xs text-surface-400">{{ formatTime(meta.created_at) }}</p>
              </div>
              <button class="shrink-0 rounded p-1 text-surface-400 hover:bg-red-50 hover:text-red-500 transition-colors" title="删除元数据" @click="handleDeleteMetadata(meta.asset_metadata_id)">
                <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
              </button>
            </div>
          </div>
          <div v-else-if="metadataList" class="flex flex-col items-center justify-center py-8 text-sm text-surface-400">暂无元数据</div>
        </div>
      </template>
      <div v-else class="flex flex-col items-center justify-center py-12 text-sm text-surface-400">加载资产详情失败</div>
      <template #footer>
        <div class="flex items-center justify-between">
          <span class="text-2xs text-surface-400">ID: {{ selectedAssetId ? selectedAssetId.slice(0, 8) + '...' : '—' }}</span>
          <button class="btn btn-secondary btn-sm" @click="drawerOpen = false">关闭</button>
        </div>
      </template>
    </DetailDrawer>

    <!-- ═══════════════════════════════════════════════ -->
    <!--  Pipeline & Store Selection Modal -->
    <!-- ═══════════════════════════════════════════════ -->
    <Teleport to="body">
      <Transition name="modal">
        <div
          v-if="showPipelinePanel"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4"
          @click.self="showPipelinePanel = false"
        >
          <div class="bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto" @click.stop>
            <!-- Header -->
            <div class="px-6 py-4 border-b border-surface-200 flex items-center justify-between">
              <div>
                <h3 class="text-lg font-semibold text-surface-800">选择处理方式</h3>
                <p class="text-xs text-surface-400 mt-0.5">{{ uploadedAssetIds.length }} 个文件 · 项目: {{ selectedProject?.name }}</p>
              </div>
              <button class="rounded-lg p-1.5 text-surface-400 hover:bg-surface-100 hover:text-surface-600" @click="showPipelinePanel = false">
                <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>
              </button>
            </div>

            <!-- Pipeline selection -->
            <div class="px-6 py-4 border-b border-surface-100">
              <label class="text-sm font-semibold text-surface-700 mb-3 block">选择处理管道</label>
              <div v-if="!(pipelinesData ?? []).length" class="flex flex-col items-center py-6 text-center">
                <span class="text-2xl mb-2">⚙️</span>
                <p class="text-sm text-surface-400">暂无可用处理管道</p>
                <p class="text-xs text-surface-300 mt-1">请先创建 asset_import 类型的管道定义</p>
              </div>
              <div v-else class="space-y-2">
                <label
                  v-for="pipe in pipelinesData"
                  :key="pipe.pipeline_key"
                  :class="[
                    'flex items-start gap-3 rounded-xl border-2 p-3 cursor-pointer transition-all',
                    selectedPipeline === pipe.pipeline_key
                      ? 'border-brand-400 bg-brand-50/50'
                      : 'border-surface-200 hover:border-surface-300 hover:bg-surface-50',
                  ]"
                >
                  <input
                    type="radio"
                    :value="pipe.pipeline_key"
                    v-model="selectedPipeline"
                    class="mt-0.5 shrink-0 h-4 w-4 text-brand-600 focus:ring-brand-500"
                  />
                  <div class="min-w-0 flex-1">
                    <span class="text-sm font-medium text-surface-700 flex items-center gap-1.5">
                      <span>{{ pipe.icon }}</span>{{ pipe.name }}
                    </span>
                    <p class="text-xs text-surface-400 mt-0.5">{{ pipe.description }}</p>
                    <div class="flex flex-wrap gap-1 mt-1.5">
                      <span
                        v-for="fmt in pipe.supported_formats"
                        :key="fmt"
                        class="text-2xs rounded px-1.5 py-0.5 bg-surface-100 text-surface-500 font-mono"
                      >.{{ fmt }}</span>
                    </div>
                  </div>
                </label>
              </div>
            </div>

            <!-- Store selection -->
            <div class="px-6 py-4">
              <label class="text-sm font-semibold text-surface-700 mb-3 block">目标子库 (可多选)</label>
              <div v-if="!(storesData ?? []).length" class="flex flex-col items-center py-6 text-center">
                <span class="text-2xl mb-2">📚</span>
                <p class="text-sm text-surface-400">暂无可用子库</p>
                <p class="text-xs text-surface-300 mt-1">请先创建子库注册</p>
              </div>
              <div v-else class="space-y-2">
                <label
                  v-for="store in storesData"
                  :key="store.store_key"
                  :class="[
                    'flex items-start gap-3 rounded-xl border-2 p-3 cursor-pointer transition-all',
                    selectedStores.includes(store.store_key)
                      ? 'border-brand-400 bg-brand-50/50'
                      : 'border-surface-200 hover:border-surface-300 hover:bg-surface-50',
                  ]"
                  @click="toggleStore(store.store_key)"
                >
                  <div
                    :class="[
                      'mt-0.5 shrink-0 h-5 w-5 rounded border-2 flex items-center justify-center transition-all',
                      selectedStores.includes(store.store_key)
                        ? 'bg-brand-600 border-brand-600 text-white'
                        : 'border-surface-300',
                    ]"
                  >
                    <svg v-if="selectedStores.includes(store.store_key)" xmlns="http://www.w3.org/2000/svg" class="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="3"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>
                  </div>
                  <div class="min-w-0 flex-1">
                    <span class="text-sm font-medium text-surface-700 flex items-center gap-1.5">
                      <span>{{ store.icon }}</span>{{ store.name }}
                    </span>
                    <p class="text-xs text-surface-400 mt-0.5">{{ store.description }}</p>
                  </div>
                </label>
              </div>
            </div>

            <!-- Footer actions (sticky) -->
            <div class="px-6 py-4 border-t border-surface-200 bg-surface-50 rounded-b-2xl flex items-center justify-between gap-2 sticky bottom-0 z-10">
              <p v-if="uploadedAssetIds.length > 0 && (!selectedPipeline || !selectedStores.length)" class="text-xs text-amber-600">
                还需选择管道和子库，才能开始处理
              </p>
              <span v-else class="text-xs text-surface-400">选择管道和子库后开始入库</span>
              <div class="flex items-center gap-2">
                <button class="btn btn-secondary btn-sm" @click="showPipelinePanel = false">取消</button>
                <button
                  class="btn btn-primary btn-sm"
                  :disabled="!selectedPipeline || !selectedStores.length || uploadedAssetIds.length === 0"
                  @click="submitProcess"
                >
                  {{ uploadedAssetIds.length > 0 ? `开始处理 (${uploadedAssetIds.length} 个文件)` : '请先上传文件' }}
                </button>
              </div>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>
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
</style>
