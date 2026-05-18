<script setup lang="ts">
import { ref } from 'vue'
import type { TreeNode } from '@/api/client'

const props = defineProps<{
  node: TreeNode
  selected: TreeNode | null
  depth?: number
}>()

const emit = defineEmits<{ select: [node: TreeNode] }>()

const collapsed = ref(false)

const iconMap: Record<string, string> = {
  typescript: 'TS', javascript: 'JS', python: 'PY', json: '{}', markdown: 'MD',
  yaml: 'Y', go: 'Go', rust: 'RS', sql: 'SQ', css: '#', html: '<>',
}
const colorMap: Record<string, string> = {
  typescript: '#3178c6', javascript: '#f0db4f', python: '#3572A5', json: '#f0db4f',
  markdown: '#7aa2f7', go: '#00ADD8', rust: '#dea584', sql: '#e0af68',
}

function icon(node: TreeNode): string { return iconMap[node.lang || ''] || '·' }
function icColor(node: TreeNode): string { return colorMap[node.lang || ''] || '#565f89' }
</script>

<template>
  <div class="tree-node">
    <!-- Folder -->
    <div
      v-if="node.type === 'folder'"
      class="folder-row"
      :style="{ paddingLeft: ((depth || 0) * 16 + 8) + 'px' }"
      @click="collapsed = !collapsed"
    >
      <span class="folder-chevron" :class="{ collapsed: collapsed }">&#9656;</span>
      <span class="folder-icon">&#128193;</span>
      <span class="folder-name">{{ node.name }}</span>
      <span class="folder-count" v-if="node.children">{{ node.children.length }}</span>
    </div>

    <!-- File -->
    <button
      v-else
      class="file-row"
      :class="{ active: selected?.document_id === node.document_id }"
      :style="{ paddingLeft: ((depth || 0) * 16 + 28) + 'px' }"
      @click="emit('select', node)"
    >
      <span class="file-icon-badge" :style="{ background: icColor(node) + '20', color: icColor(node) }">
        {{ icon(node) }}
      </span>
      <span class="file-name">{{ node.name }}</span>
    </button>

    <!-- Children -->
    <div v-if="node.children && node.children.length && !collapsed" class="children-block">
      <TreeItem
        v-for="child in node.children"
        :key="(child.document_id || child.path || '') + child.name"
        :node="child"
        :selected="selected"
        :depth="(depth || 0) + 1"
        @select="emit('select', $event)"
      />
    </div>
  </div>
</template>

<style scoped>
.tree-node { user-select: none; }
.folder-row {
  display: flex; align-items: center; gap: 4px; padding: 4px 8px;
  cursor: pointer; border-radius: 4px; margin: 1px 4px;
  transition: background 0.15s;
}
.folder-row:hover { background: rgba(255,255,255,0.03); }
.folder-chevron { font-size: 9px; color: #565f89; width: 12px; transition: transform 0.15s; flex-shrink: 0; }
.folder-chevron.collapsed { transform: rotate(-90deg); }
.folder-icon { font-size: 14px; flex-shrink: 0; }
.folder-name { font-size: 12.5px; font-weight: 500; color: #a9b1d6; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.folder-count { font-size: 10px; color: #565f89; background: #2a2e40; padding: 0 5px; border-radius: 8px; }
.file-row {
  display: flex; align-items: center; gap: 8px; width: 100%; text-align: left;
  padding: 5px 10px; border: none; background: none; cursor: pointer;
  border-radius: 0 6px 6px 0; margin: 1px 4px 1px 0;
  transition: all 0.12s; font-family: inherit;
  border-left: 2px solid transparent;
}
.file-row:hover { background: rgba(255,255,255,0.04); }
.file-row.active {
  background: rgba(122,162,247,0.08);
  border-left-color: #7aa2f7;
}
.file-icon-badge {
  width: 22px; height: 22px; border-radius: 4px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 9.5px; font-weight: 700; font-family: 'JetBrains Mono', monospace;
  letter-spacing: -0.5px;
}
.file-name {
  font-size: 12.8px; color: #a9b1d6; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  transition: color 0.12s;
}
.file-row:hover .file-name { color: #c0caf5; }
.file-row.active .file-name { color: #c0caf5; font-weight: 500; }
.children-block { overflow: hidden; }
</style>