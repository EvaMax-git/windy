<script setup lang="ts">
import { ref, computed } from "vue";
import { uploadAsset, uploadAndImport, getImportJobStatus } from "@/api/client";

interface UploadFileItem {
  file: File;
  progress: number;
  status: "pending" | "uploading" | "parsing" | "succeeded" | "failed" | "timeout";
  error?: string;
  assetId?: string;
  jobId?: string;
  parseResult?: { block_count?: number; chunk_count?: number };
}

const props = defineProps<{
  projectId?: string;
  disabled?: boolean;
}>();

const emit = defineEmits<{
  "upload-complete": [results: { file: File; assetId: string; success: boolean }[]];
}>();

const MAX_FILE_SIZE = 100 * 1024 * 1024; // 100MB
const ALLOWED_TYPES: Record<string, string[]> = {
  document: [".pdf", ".doc", ".docx", ".txt", ".md", ".csv", ".json", ".xml", ".html"],
  image: [".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"],
  audio: [".mp3", ".wav", ".ogg", ".flac", ".m4a"],
  video: [".mp4", ".webm", ".avi", ".mov"],
  archive: [".zip", ".tar", ".gz", ".7z", ".rar"],
};

const fileInput = ref<HTMLInputElement | null>(null);
const isDragging = ref(false);
const files = ref<UploadFileItem[]>([]);
const uploadAllProgress = ref(0);
const isUploading = ref(false);
const showUploader = ref(false);

const allAllowedExtensions = computed(() => {
  const all: string[] = [];
  for (const exts of Object.values(ALLOWED_TYPES)) {
    all.push(...exts);
  }
  return all;
});

const acceptString = computed(() => allAllowedExtensions.value.join(","));

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function validateFile(file: File): string | null {
  const ext = "." + file.name.split(".").pop()?.toLowerCase();
  if (!allAllowedExtensions.value.includes(ext)) {
    return `不支持的文件类型: ${ext}`;
  }
  if (file.size > MAX_FILE_SIZE) {
    return `文件过大 (最大 ${formatSize(MAX_FILE_SIZE)}): ${formatSize(file.size)}`;
  }
  if (file.size === 0) {
    return "文件为空";
  }
  return null;
}

async function handleFiles(newFiles: FileList | File[]) {
  const fileArray = Array.from(newFiles);
  const valid: UploadFileItem[] = [];

  for (const file of fileArray) {
    const error = validateFile(file);
    valid.push({
      file,
      progress: 0,
      status: error ? "failed" : "pending",
      error: error || undefined,
    });
  }

  files.value = [...files.value, ...valid];
  showUploader.value = true;
}

function onDragOver(e: DragEvent) {
  e.preventDefault();
  if (!props.disabled) isDragging.value = true;
}

function onDragLeave() {
  isDragging.value = false;
}

function onDrop(e: DragEvent) {
  e.preventDefault();
  isDragging.value = false;
  if (!props.disabled && e.dataTransfer?.files) {
    handleFiles(e.dataTransfer.files);
  }
}

function onFileInputChange(e: Event) {
  const input = e.target as HTMLInputElement;
  if (input.files) {
    handleFiles(input.files);
  }
  input.value = "";
}

function removeFile(index: number) {
  files.value.splice(index, 1);
}

function clearAll() {
  files.value = [];
  uploadAllProgress.value = 0;
}

async function uploadAll() {
  if (isUploading.value) return;

  isUploading.value = true;
  const results: { file: File; assetId: string; success: boolean }[] = [];
  const pendingFiles = files.value.filter((f) => f.status !== "succeeded");

  let completed = 0;
  const total = pendingFiles.length;

  for (const item of pendingFiles) {
    if (item.status === "failed" && item.error) {
      completed++;
      uploadAllProgress.value = Math.round((completed / total) * 100);
      continue;
    }

    item.status = "uploading";
    item.progress = 0;

    try {
      const ext = "." + item.file.name.split(".").pop()?.toLowerCase();
      const isDocx = ext === ".docx";

      if (isDocx) {
        // Use /import endpoint to trigger auto-parse pipeline
        const result = await uploadAndImport(item.file, {
          projectId: props.projectId,
          onProgress: (percent) => {
            item.progress = percent;
          },
        });
        item.status = "parsing";
        item.progress = 100;
        item.jobId = result.job_id;
        item.assetId = result.asset_uid;

        // Poll job status until done/failed
        pollJobStatus(item);
        results.push({ file: item.file, assetId: result.asset_uid || "", success: true });
      } else {
        const asset = await uploadAsset(item.file, props.projectId, {
          onProgress: (percent) => {
            item.progress = percent;
          },
        });
        item.status = "succeeded";
        item.progress = 100;
        item.assetId = asset.asset_id;
        results.push({ file: item.file, assetId: asset.asset_id, success: true });
      }
    } catch (err) {
      item.status = "failed";
      item.error = err instanceof Error ? err.message : "上传失败";
      results.push({ file: item.file, assetId: "", success: false });
    }

    completed++;
    uploadAllProgress.value = Math.round((completed / total) * 100);
  }

  isUploading.value = false;
  emit("upload-complete", results);
}

async function pollJobStatus(item: UploadFileItem) {
  if (!item.jobId) return;
  const maxAttempts = 60;
  for (let i = 0; i < maxAttempts; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const status = await getImportJobStatus(item.jobId);
      if (status.status === "done") {
        item.status = "succeeded";
        item.parseResult = { block_count: (status as any).block_count, chunk_count: (status as any).chunk_count };
        return;
      }
      if (status.status === "failed") {
        item.status = "failed";
        item.error = status.error || "解析失败";
        return;
      }
    } catch {
      // ignore polling errors, keep trying
    }
  }
  // Timeout — parsing took too long
  item.status = "timeout";
  item.error = "解析超时，文件可能仍在处理中";
}

function getStatusIcon(status: string): string {
  switch (status) {
    case "succeeded":
      return `<svg class="h-4 w-4 text-emerald-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>`;
    case "failed":
      return `<svg class="h-4 w-4 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>`;
    case "uploading":
    case "parsing":
      return `<svg class="h-4 w-4 animate-spin text-brand-500" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>`;
    default:
      return `<svg class="h-4 w-4 text-surface-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 4v16m8-8H4"/></svg>`;
  }
}
</script>

<template>
  <div class="space-y-4">
    <!-- Upload trigger button -->
    <button
      v-if="!showUploader"
      @click="showUploader = true"
      :disabled="disabled"
      class="btn btn-primary inline-flex items-center gap-2"
    >
      <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"/>
      </svg>
      上传资产
    </button>

    <!-- Upload area -->
    <div v-if="showUploader" class="rounded-lg border border-surface-200 bg-white">
      <!-- Drop zone -->
      <div
        :class="[
          'relative rounded-t-lg border-2 border-dashed p-8 text-center transition-colors cursor-pointer',
          isDragging
            ? 'border-brand-400 bg-brand-50'
            : 'border-surface-300 hover:border-brand-300 hover:bg-surface-50',
          disabled ? 'opacity-50 cursor-not-allowed' : '',
        ]"
        @dragover="onDragOver"
        @dragleave="onDragLeave"
        @drop="onDrop"
        @click="!disabled && fileInput?.click()"
      >
        <input
          ref="fileInput"
          type="file"
          :accept="acceptString"
          multiple
          class="hidden"
          :disabled="disabled"
          @change="onFileInputChange"
        />

        <div class="flex flex-col items-center gap-3">
          <!-- Upload icon -->
          <div
            :class="[
              'flex h-14 w-14 items-center justify-center rounded-full',
              isDragging ? 'bg-brand-100 text-brand-600' : 'bg-surface-100 text-surface-400',
            ]"
          >
            <svg xmlns="http://www.w3.org/2000/svg" class="h-7 w-7" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"/>
            </svg>
          </div>

          <div>
            <p class="text-sm font-medium text-surface-700">
              {{ isDragging ? '松开文件以上传' : '拖拽文件到此处或点击选择' }}
            </p>
            <p class="mt-1 text-xs text-surface-400">
              支持文档、图片、音视频、压缩包，单文件最大 {{ formatSize(MAX_FILE_SIZE) }}
            </p>
          </div>
        </div>
      </div>

      <!-- File list -->
      <div v-if="files.length > 0" class="divide-y divide-surface-100">
        <!-- Overall progress -->
        <div v-if="isUploading" class="px-4 py-2 bg-surface-50">
          <div class="flex items-center justify-between mb-1">
            <span class="text-xs text-surface-500">总进度</span>
            <span class="text-xs font-mono text-surface-500">{{ uploadAllProgress }}%</span>
          </div>
          <div class="h-1.5 rounded-full bg-surface-200 overflow-hidden">
            <div
              class="h-full rounded-full bg-brand-500 transition-all duration-300"
              :style="{ width: uploadAllProgress + '%' }"
            ></div>
          </div>
        </div>

        <div
          v-for="(item, idx) in files"
          :key="idx"
          class="flex items-center gap-3 px-4 py-3"
        >
          <!-- Status icon -->
          <span v-html="getStatusIcon(item.status)"></span>

          <!-- File info -->
          <div class="min-w-0 flex-1">
            <p class="text-sm text-surface-700 truncate">{{ item.file.name }}</p>
            <p class="text-xs text-surface-400">{{ formatSize(item.file.size) }}</p>

            <!-- Progress bar -->
            <div
              v-if="item.status === 'uploading'"
              class="mt-1.5 h-1 rounded-full bg-surface-100 overflow-hidden"
            >
              <div
                class="h-full rounded-full bg-brand-500 transition-all duration-300"
                :style="{ width: item.progress + '%' }"
              ></div>
            </div>

            <!-- Error message -->
            <p v-if="item.error" class="mt-1 text-xs text-red-500">{{ item.error }}</p>
            <!-- Parsing status -->
            <p v-if="item.status === 'parsing'" class="mt-1 text-xs text-brand-600">
              正在解析文档...
            </p>
            <!-- Success check -->
            <p v-if="item.status === 'succeeded'" class="mt-1 text-xs text-emerald-600">
              {{ item.parseResult ? '解析完成' : '上传成功' }}
              <span v-if="item.parseResult?.chunk_count" class="font-mono">
                · {{ item.parseResult.chunk_count }} 个分块
              </span>
              <span v-if="item.assetId" class="font-mono">· {{ item.assetId.slice(0, 8) }}...</span>
            </p>
          </div>

          <!-- Remove button -->
          <button
            v-if="item.status !== 'uploading' && item.status !== 'parsing'"
            class="shrink-0 rounded p-1 text-surface-400 hover:bg-surface-100 hover:text-surface-600 transition-colors"
            @click="removeFile(idx)"
          >
            <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/>
            </svg>
          </button>
        </div>
      </div>

      <!-- Actions -->
      <div
        v-if="files.length > 0"
        class="flex items-center justify-between border-t border-surface-100 px-4 py-3"
      >
        <button
          class="text-xs text-surface-400 hover:text-surface-600 transition-colors"
          @click="clearAll"
        >
          清除全部
        </button>
        <div class="flex items-center gap-2">
          <button
            class="btn btn-secondary btn-sm"
            @click="showUploader = false"
          >
            收起
          </button>
          <button
            class="btn btn-primary btn-sm"
            :disabled="isUploading || disabled || !files.some(f => f.status === 'pending') || files.some(f => f.status === 'parsing')"
            @click="uploadAll"
          >
            <svg
              v-if="isUploading"
              class="h-3.5 w-3.5 mr-1 animate-spin"
              fill="none"
              viewBox="0 0 24 24"
            >
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
            </svg>
            全部上传 ({{ files.filter(f => f.status === 'pending').length }})
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
