<script setup lang="ts">
/**
 * ProjectManageTab — 项目 CRUD
 *
 * 前端直接调 /api/v4/projects
 *   - GET    /projects         列表
 *   - POST   /projects         创建
 *   - GET    /projects/:id     详情
 *   - PUT    /projects/:id     更新
 *   - DELETE /projects/:id     删除
 */
import { ref, computed } from "vue";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import DataTable from "@/components/DataTable.vue";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import {
  fetchProjects,
  fetchProject,
  createProject,
  updateProject,
  deleteProject,
} from "@/api/client";
import type {
  ProjectRead,
  ProjectCreateRequest,
  ProjectUpdateRequest,
} from "@/types";

const queryClient = useQueryClient();

// ── List state ──
const page = ref(1);
const pageSize = ref(20);
const filterStatus = ref("");

const listKey = computed(() => [
  "projects-manage",
  page.value,
  pageSize.value,
  filterStatus.value,
] as const);

const { data, isLoading, isError, error } = useQuery({
  queryKey: listKey,
  queryFn: () =>
    fetchProjects({
      page: page.value,
      page_size: pageSize.value,
      status: filterStatus.value || undefined,
    }),
  placeholderData: (prev) => prev,
});

const projects = computed(() => data.value?.items ?? []);
const totalItems = computed(() => data.value?.page_info?.total_items ?? 0);

// ── Create / Edit modal ──
const showForm = ref(false);
const editingProject = ref<ProjectRead | null>(null);
const formData = ref<ProjectCreateRequest>({
  project_code: "",
  name: "",
  description: null,
  sensitivity_default: "public",
});
const submitting = ref(false);

function openCreate() {
  editingProject.value = null;
  formData.value = {
    project_code: "",
    name: "",
    description: null,
    sensitivity_default: "public",
  };
  showForm.value = true;
}

function openEdit(project: ProjectRead) {
  editingProject.value = project;
  formData.value = {
    project_code: project.project_code,
    name: project.name,
    description: project.description,
    sensitivity_default: project.sensitivity_default,
  };
  showForm.value = true;
}

const submitError = ref<string | null>(null);

async function handleSubmit() {
  if (!formData.value.project_code.trim() || !formData.value.name.trim()) return;
  submitting.value = true;
  submitError.value = null;

  // Normalize: empty description string → null
  const description = formData.value.description?.trim() || null;

  try {
    if (editingProject.value) {
      const updatePayload: ProjectUpdateRequest = {
        name: formData.value.name,
        description: description,
        sensitivity_default: formData.value.sensitivity_default,
      };
      await updateProject(editingProject.value.project_id, updatePayload);
    } else {
      await createProject({
        ...formData.value,
        description,
      });
    }
    showForm.value = false;
    queryClient.invalidateQueries({ queryKey: ["projects-manage"] });
    queryClient.invalidateQueries({ queryKey: ["projects"] });
  } catch (e: any) {
    const msg = e?.message || "操作失败";
    submitError.value = msg;
    console.error("提交项目失败", e);
  } finally {
    submitting.value = false;
  }
}

// ── Delete ──
const confirmDeleteId = ref<string | null>(null);
const deleting = ref(false);
const deleteError = ref<string | null>(null);

async function handleDelete(projectId: string) {
  deleting.value = true;
  deleteError.value = null;
  try {
    await deleteProject(projectId);
    confirmDeleteId.value = null;
    queryClient.invalidateQueries({ queryKey: ["projects-manage"] });
    queryClient.invalidateQueries({ queryKey: ["projects"] });
  } catch (e: any) {
    const msg = e?.message || "删除失败";
    deleteError.value = msg;
    console.error("删除项目失败", e);
  } finally {
    deleting.value = false;
  }
}

// ── Detail drawer ──
const selectedProjectId = ref<string | null>(null);
const drawerOpen = ref(false);

const { data: projectDetail, isLoading: detailLoading } = useQuery({
  queryKey: ["project-detail", selectedProjectId],
  queryFn: () => fetchProject(selectedProjectId.value!),
  enabled: computed(() => drawerOpen.value && !!selectedProjectId.value),
});

function openDetail(project: ProjectRead) {
  selectedProjectId.value = project.project_id;
  drawerOpen.value = true;
}

// ── Helpers ──
function formatTime(iso?: string): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

const STATUS_STYLES: Record<string, string> = {
  active: "bg-emerald-50 text-emerald-700",
  archived: "bg-surface-100 text-surface-500",
  disabled: "bg-red-50 text-red-600",
};

const STATUS_LABELS: Record<string, string> = {
  active: "活跃",
  archived: "已归档",
  disabled: "已禁用",
};

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

const columns = [
  { key: "project_code", label: "项目编码", width: "120px" },
  { key: "name", label: "项目名称", width: "200px" },
  { key: "description", label: "描述", width: "240px" },
  { key: "status", label: "状态", width: "80px" },
  { key: "sensitivity_default", label: "默认敏感等级", width: "110px" },
  { key: "created_at", label: "创建时间", width: "160px" },
  { key: "actions", label: "操作", width: "120px" },
];

const SENSITIVITY_OPTIONS = [
  { value: "public", label: "公开" },
  { value: "normal", label: "普通" },
  { value: "private", label: "内部" },
  { value: "sensitive", label: "敏感" },
  { value: "secret", label: "机密" },
];
</script>

<template>
  <div>
    <!-- ── Toolbar ── -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="flex items-center gap-3">
          <h3 class="text-sm font-semibold text-surface-700">项目列表</h3>
          <span class="text-xs text-surface-400">{{ totalItems }} 个项目</span>
        </div>
        <div class="flex items-center gap-2">
          <select
            v-model="filterStatus"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-xs text-surface-600 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部状态</option>
            <option value="active">活跃</option>
            <option value="archived">已归档</option>
            <option value="disabled">已禁用</option>
          </select>
          <button class="btn btn-primary btn-sm" @click="openCreate">
            <svg xmlns="http://www.w3.org/2000/svg" class="h-3.5 w-3.5 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M12 4v16m8-8H4"/>
            </svg>
            新建项目
          </button>
        </div>
      </div>
    </div>

    <!-- ── Error ── -->
    <div v-if="isError" class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700">
      加载项目失败: {{ (error as Error)?.message }}
    </div>

    <!-- ── Table ── -->
    <DataTable
      :items="projects"
      :columns="columns"
      :loading="isLoading"
      empty-message="暂无项目"
      clickable
      row-key="project_id"
      @row-click="openDetail"
    >
      <template #cell-project_code="{ value }">
        <span class="font-mono text-xs font-semibold text-surface-700">{{ value }}</span>
      </template>

      <template #cell-name="{ value }">
        <span class="text-sm font-medium text-surface-800">{{ value }}</span>
      </template>

      <template #cell-description="{ value }">
        <span class="text-xs text-surface-500 truncate block max-w-[240px]">{{ value || '—' }}</span>
      </template>

      <template #cell-status="{ value }">
        <span :class="['badge text-2xs font-medium', STATUS_STYLES[value as string] || 'bg-surface-100 text-surface-600']">
          {{ STATUS_LABELS[value as string] || value }}
        </span>
      </template>

      <template #cell-sensitivity_default="{ value }">
        <span :class="['badge text-2xs font-medium', SENSITIVITY_STYLES[value as string] || 'bg-surface-100 text-surface-600']">
          {{ SENSITIVITY_LABELS[value as string] || value }}
        </span>
      </template>

      <template #cell-created_at="{ value }">
        <span class="font-mono text-xs text-surface-500">{{ formatTime(value as string) }}</span>
      </template>

      <template #cell-actions="{ item }">
        <div class="flex items-center gap-1" @click.stop>
          <button
            class="rounded p-1 text-surface-400 hover:text-brand-600 hover:bg-brand-50 transition-colors"
            title="编辑"
            data-testid="project-edit"
            @click="openEdit(item as ProjectRead)"
          >
            <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/>
            </svg>
          </button>
          <button
            class="rounded p-1 text-surface-400 hover:text-red-600 hover:bg-red-50 transition-colors"
            title="删除"
            data-testid="project-delete"
            @click="confirmDeleteId = (item as ProjectRead).project_id"
          >
            <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
            </svg>
          </button>
        </div>
      </template>
    </DataTable>

    <Pagination
      v-if="data?.page_info && totalItems > 0"
      :page-info="data.page_info"
      @page-change="(p: number) => (page = p)"
    />

    <!-- ── Create/Edit Modal ── -->
    <Teleport to="body">
      <Transition name="modal">
        <div
          v-if="showForm"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4"
          @click.self="showForm = false"
        >
          <div class="bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto" @click.stop>
            <!-- Header -->
            <div class="px-6 py-4 border-b border-surface-200 flex items-center justify-between">
              <h3 class="text-lg font-semibold text-surface-800">
                {{ editingProject ? '编辑项目' : '新建项目' }}
              </h3>
              <button class="rounded-lg p-1.5 text-surface-400 hover:bg-surface-100 hover:text-surface-600" @click="showForm = false">
                <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/>
                </svg>
              </button>
            </div>

            <!-- Form -->
            <div class="px-6 py-4 space-y-4">
              <!-- Project Code -->
              <div>
                <label class="block text-sm font-medium text-surface-700 mb-1">项目编码 <span class="text-red-500">*</span></label>
                <input
                  v-model="formData.project_code"
                  type="text"
                  :disabled="!!editingProject"
                  placeholder="例如: my-project"
                  class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:bg-surface-50 disabled:text-surface-400"
                />
                <p class="mt-1 text-xs text-surface-400">唯一标识，创建后不可修改</p>
              </div>

              <!-- Name -->
              <div>
                <label class="block text-sm font-medium text-surface-700 mb-1">项目名称 <span class="text-red-500">*</span></label>
                <input
                  v-model="formData.name"
                  type="text"
                  placeholder="项目显示名称"
                  class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                />
              </div>

              <!-- Description -->
              <div>
                <label class="block text-sm font-medium text-surface-700 mb-1">描述</label>
                <textarea
                  v-model="formData.description"
                  rows="3"
                  placeholder="项目描述（可选）"
                  class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 resize-none"
                ></textarea>
              </div>

              <!-- Sensitivity Default -->
              <div>
                <label class="block text-sm font-medium text-surface-700 mb-1">默认敏感等级</label>
                <select
                  v-model="formData.sensitivity_default"
                  class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
                >
                  <option v-for="opt in SENSITIVITY_OPTIONS" :key="opt.value" :value="opt.value">
                    {{ opt.label }}
                  </option>
                </select>
              </div>
            </div>

            <!-- Error -->
            <div v-if="submitError" class="px-6 py-3 bg-red-50 border-t border-red-100 text-sm text-red-700">
              {{ submitError }}
            </div>

            <!-- Footer -->
            <div class="px-6 py-4 border-t border-surface-200 bg-surface-50 rounded-b-2xl flex items-center justify-end gap-2">
              <button class="btn btn-secondary btn-sm" @click="showForm = false">取消</button>
              <button
                class="btn btn-primary btn-sm"
                :disabled="submitting || !formData.project_code.trim() || !formData.name.trim()"
                @click="handleSubmit"
              >
                {{ submitting ? '提交中...' : editingProject ? '保存更改' : '创建项目' }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- ── Delete Confirmation Modal ── -->
    <Teleport to="body">
      <Transition name="modal">
        <div
          v-if="confirmDeleteId"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4"
          @click.self="confirmDeleteId = null"
        >
          <div class="bg-white rounded-2xl shadow-2xl w-full max-w-sm" @click.stop>
            <div class="p-6">
              <div class="flex items-center gap-3 mb-4">
                <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-red-100 text-red-600">
                  <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>
                  </svg>
                </div>
                <div>
                  <h3 class="text-lg font-semibold text-surface-800">确认删除</h3>
                  <p class="text-sm text-surface-500 mt-0.5">此操作不可恢复，请谨慎操作</p>
                </div>
              </div>
              <div v-if="deleteError" class="mb-3 text-sm text-red-600 bg-red-50 rounded-lg p-2">
                {{ deleteError }}
              </div>
              <div class="flex items-center justify-end gap-2">
                <button class="btn btn-secondary btn-sm" @click="confirmDeleteId = null">取消</button>
                <button class="btn btn-danger btn-sm" :disabled="deleting" @click="handleDelete(confirmDeleteId!)">
                  {{ deleting ? '删除中...' : '确认删除' }}
                </button>
              </div>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- ── Detail Drawer ── -->
    <DetailDrawer :open="drawerOpen" title="项目详情" width="w-[540px] max-w-full" @close="drawerOpen = false">
      <div v-if="detailLoading" class="py-4"><LoadingSkeleton variant="detail" /></div>

      <template v-else-if="projectDetail">
        <dl class="space-y-4">
          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">项目编码</dt>
              <dd class="mt-1 font-mono text-sm text-surface-700">{{ projectDetail.project_code }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">项目名称</dt>
              <dd class="mt-1 text-sm text-surface-700">{{ projectDetail.name }}</dd>
            </div>
          </div>

          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">描述</dt>
            <dd class="mt-1 text-sm text-surface-600">{{ projectDetail.description || '—' }}</dd>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">状态</dt>
              <dd class="mt-1">
                <span :class="['badge text-2xs font-medium', STATUS_STYLES[projectDetail.status] || 'bg-surface-100 text-surface-600']">
                  {{ STATUS_LABELS[projectDetail.status] || projectDetail.status }}
                </span>
              </dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">默认敏感等级</dt>
              <dd class="mt-1">
                <span :class="['badge text-2xs font-medium', SENSITIVITY_STYLES[projectDetail.sensitivity_default] || 'bg-surface-100 text-surface-600']">
                  {{ SENSITIVITY_LABELS[projectDetail.sensitivity_default] || projectDetail.sensitivity_default }}
                </span>
              </dd>
            </div>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt>
              <dd class="mt-1 text-xs">{{ formatTime(projectDetail.created_at) }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">更新时间</dt>
              <dd class="mt-1 text-xs">{{ formatTime(projectDetail.updated_at) }}</dd>
            </div>
          </div>

          <div v-if="projectDetail.archived_at">
            <dt class="text-2xs font-semibold uppercase text-surface-400">归档时间</dt>
            <dd class="mt-1 text-xs">{{ formatTime(projectDetail.archived_at) }}</dd>
          </div>
        </dl>

        <div class="mt-6 pt-4 border-t border-surface-200 flex items-center gap-2">
          <button class="btn btn-secondary btn-sm" @click="openEdit(projectDetail); drawerOpen = false">编辑项目</button>
        </div>
      </template>

      <div v-else class="flex flex-col items-center justify-center py-12 text-sm text-surface-400">加载项目详情失败</div>

      <template #footer>
        <div class="flex items-center justify-between">
          <span class="text-2xs text-surface-400">ID: {{ selectedProjectId ? selectedProjectId.slice(0, 8) + '...' : '—' }}</span>
          <button class="btn btn-secondary btn-sm" @click="drawerOpen = false">关闭</button>
        </div>
      </template>
    </DetailDrawer>
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
