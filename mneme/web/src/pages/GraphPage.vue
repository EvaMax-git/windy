<script setup lang="ts">
import { ref, computed, watch, onUnmounted, reactive } from "vue";
import { useRouter } from "vue-router";
import { useQuery } from "@tanstack/vue-query";
import PageHeader from "@/components/PageHeader.vue";
import DetailDrawer from "@/components/DetailDrawer.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import { fetchGraphData, fetchGraphNode, fetchKnowledgeDocuments } from "@/api/client";
import type { GraphNode, GraphEdge, GraphData, GraphNodeType } from "@/types";

const router = useRouter();

// ── Check whether knowledge documents exist (determines guide vs. empty) ──
const { data: knowledgeDocs } = useQuery({
  queryKey: ["knowledge-documents-for-graph"],
  queryFn: () => fetchKnowledgeDocuments({ page: 1, page_size: 1 }),
});

const hasKnowledgeDocs = computed(
  () => (knowledgeDocs.value?.page_info?.total_items ?? 0) > 0,
);

// ── Filters ──
const filterNodeType = ref("");
const filterSearch = ref("");
const searchInput = ref("");
let searchTimer: ReturnType<typeof setTimeout> | null = null;

function onSearchInput() {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    filterSearch.value = searchInput.value.trim();
  }, 350);
}

// ── Graph data query ──
const graphKey = computed(() => [
  "graph",
  filterNodeType.value,
  filterSearch.value,
] as const);

const { data: graphData, isLoading, isError, error, refetch } = useQuery({
  queryKey: graphKey,
  queryFn: () =>
    fetchGraphData({
      node_type: filterNodeType.value || undefined,
      search: filterSearch.value || undefined,
      limit: 300,
      depth: 2,
    }),
  placeholderData: (prev) => prev,
});

// ── Layout: force-directed (simple) ──
interface LayoutNode extends GraphNode {
  x: number;
  y: number;
  vx: number;
  vy: number;
  radius: number;
  color: string;
}

interface LayoutEdge {
  source: LayoutNode;
  target: LayoutNode;
  relation_type: string;
  weight: number;
  label: string | null;
}

const NODE_COLORS: Record<string, string> = {
  memory: "#6366f1",
  document: "#10b981",
  concept: "#f59e0b",
  entity: "#ef4444",
  agent: "#8b5cf6",
};

const NODE_SIZES: Record<string, number> = {
  memory: 18,
  document: 22,
  concept: 16,
  entity: 16,
  agent: 20,
};

const layoutNodes = ref<LayoutNode[]>([]);
const layoutEdges = ref<LayoutEdge[]>([]);
const nodeMap = new Map<string, LayoutNode>();

function buildLayout(data: GraphData) {
  const nodes: LayoutNode[] = data.nodes.map((n, i) => {
    const angle = (2 * Math.PI * i) / Math.max(data.nodes.length, 1);
    const radius = 200 + Math.random() * 100;
    return {
      ...n,
      x: 500 + radius * Math.cos(angle),
      y: 350 + radius * Math.sin(angle),
      vx: 0,
      vy: 0,
      radius: NODE_SIZES[n.node_type] || 16,
      color: NODE_COLORS[n.node_type] || "#64748b",
    };
  });

  nodeMap.clear();
  nodes.forEach((n) => nodeMap.set(n.node_id, n));

  const edges: LayoutEdge[] = [];
  for (const e of data.edges) {
    const src = nodeMap.get(e.from_node_id);
    const tgt = nodeMap.get(e.to_node_id);
    if (src && tgt) {
      edges.push({
        source: src,
        target: tgt,
        relation_type: e.relation_type,
        weight: e.weight,
        label: e.label,
      });
    }
  }

  layoutNodes.value = nodes;
  layoutEdges.value = edges;
}

// Force simulation
let animFrameId: number | null = null;
const isSimulating = ref(false);

function runSimulation(iterations = 80) {
  if (animFrameId) cancelAnimationFrame(animFrameId);
  isSimulating.value = true;

  let iter = 0;
  function tick() {
    if (iter >= iterations || layoutNodes.value.length === 0) {
      isSimulating.value = false;
      return;
    }

    const nodes = layoutNodes.value;
    const edges = layoutEdges.value;
    const alpha = 1 - iter / iterations;

    // Repulsion between nodes
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const dx = nodes[j].x - nodes[i].x;
        const dy = nodes[j].y - nodes[i].y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const force = (800 * alpha) / dist;
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        nodes[i].vx -= fx;
        nodes[i].vy -= fy;
        nodes[j].vx += fx;
        nodes[j].vy += fy;
      }
    }

    // Attraction along edges
    for (const edge of edges) {
      const dx = edge.target.x - edge.source.x;
      const dy = edge.target.y - edge.source.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const force = (dist - 120) * 0.005 * alpha;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      edge.source.vx += fx;
      edge.source.vy += fy;
      edge.target.vx -= fx;
      edge.target.vy -= fy;
    }

    // Center gravity
    for (const node of nodes) {
      node.vx += (500 - node.x) * 0.001 * alpha;
      node.vy += (350 - node.y) * 0.001 * alpha;
    }

    // Apply velocity with damping
    for (const node of nodes) {
      node.vx *= 0.85;
      node.vy *= 0.85;
      node.x += node.vx;
      node.y += node.vy;
      // Clamp
      node.x = Math.max(40, Math.min(960, node.x));
      node.y = Math.max(40, Math.min(660, node.y));
    }

    iter++;
    animFrameId = requestAnimationFrame(tick);
  }

  tick();
}

// ── Watch graph data → rebuild layout ──
watch(
  graphData,
  (data) => {
    if (data && data.nodes.length > 0) {
      buildLayout(data);
      runSimulation();
    } else {
      layoutNodes.value = [];
      layoutEdges.value = [];
    }
  },
  { immediate: true },
);

onUnmounted(() => {
  if (animFrameId) cancelAnimationFrame(animFrameId);
  if (searchTimer) clearTimeout(searchTimer);
});

// ── Drag ──
const dragging = reactive({ nodeId: null as string | null, offsetX: 0, offsetY: 0 });

function onNodeMouseDown(e: MouseEvent, node: LayoutNode) {
  e.preventDefault();
  dragging.nodeId = node.node_id;
  const svg = svgRef.value;
  if (!svg) return;
  const pt = svgPoint(e);
  dragging.offsetX = pt.x - node.x;
  dragging.offsetY = pt.y - node.y;
}

function onMouseMove(e: MouseEvent) {
  if (!dragging.nodeId) return;
  const node = layoutNodes.value.find((n) => n.node_id === dragging.nodeId);
  if (!node) return;
  const pt = svgPoint(e);
  node.x = pt.x - dragging.offsetX;
  node.y = pt.y - dragging.offsetY;
  node.vx = 0;
  node.vy = 0;
}

function onMouseUp() {
  dragging.nodeId = null;
}

const svgRef = ref<SVGSVGElement | null>(null);

function svgPoint(e: MouseEvent): { x: number; y: number } {
  const svg = svgRef.value;
  if (!svg) return { x: 0, y: 0 };
  const rect = svg.getBoundingClientRect();
  const scaleX = 1000 / rect.width;
  const scaleY = 700 / rect.height;
  return {
    x: (e.clientX - rect.left) * scaleX,
    y: (e.clientY - rect.top) * scaleY,
  };
}

// ── Hover / Select ──
const hoveredNodeId = ref<string | null>(null);
const selectedNodeId = ref<string | null>(null);
const drawerOpen = ref(false);

function onNodeClick(node: LayoutNode) {
  selectedNodeId.value = node.node_id;
  drawerOpen.value = true;
}

// ── Detail query ──
const { data: nodeDetail, isLoading: detailLoading } = useQuery({
  queryKey: ["graph-node", selectedNodeId],
  queryFn: () => fetchGraphNode(selectedNodeId.value!),
  enabled: computed(() => drawerOpen.value && !!selectedNodeId.value),
});

// ── Zoom / Pan ──
const viewBox = reactive({ x: 0, y: 0, w: 1000, h: 700 });

function onWheel(e: WheelEvent) {
  e.preventDefault();
  const factor = e.deltaY > 0 ? 1.1 : 0.9;
  const pt = svgPoint(e);
  const newW = Math.max(200, Math.min(2000, viewBox.w * factor));
  const newH = Math.max(140, Math.min(1400, viewBox.h * factor));
  viewBox.x = pt.x - (pt.x - viewBox.x) * (newW / viewBox.w);
  viewBox.y = pt.y - (pt.y - viewBox.y) * (newH / viewBox.h);
  viewBox.w = newW;
  viewBox.h = newH;
}

// ── Panning ──
const isPanning = ref(false);
const panStart = reactive({ x: 0, y: 0 });

function onSvgMouseDown(e: MouseEvent) {
  if (dragging.nodeId) return;
  isPanning.value = true;
  const pt = svgPoint(e);
  panStart.x = pt.x;
  panStart.y = pt.y;
}

function onSvgMouseMove(e: MouseEvent) {
  if (isPanning.value && !dragging.nodeId) {
    const pt = svgPoint(e);
    viewBox.x -= pt.x - panStart.x;
    viewBox.y -= pt.y - panStart.y;
  }
  onMouseMove(e);
}

function onSvgMouseUp() {
  isPanning.value = false;
  onMouseUp();
}

// ── Helpers ──
function nodeTypeLabel(t: string): string {
  const m: Record<string, string> = { memory: "记忆", document: "文档", concept: "概念", entity: "实体", agent: "Agent" };
  return m[t] || t;
}

function formatTime(iso: string): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function edgePath(e: LayoutEdge): string {
  return `M ${e.source.x} ${e.source.y} L ${e.target.x} ${e.target.y}`;
}

// Connected edges for highlighting
const connectedNodeIds = computed(() => {
  if (!hoveredNodeId.value) return new Set<string>();
  const ids = new Set<string>();
  for (const e of layoutEdges.value) {
    if (e.source.node_id === hoveredNodeId.value) ids.add(e.target.node_id);
    if (e.target.node_id === hoveredNodeId.value) ids.add(e.source.node_id);
  }
  return ids;
});

function isNodeDimmed(node: LayoutNode): boolean {
  if (!hoveredNodeId.value) return false;
  return node.node_id !== hoveredNodeId.value && !connectedNodeIds.value.has(node.node_id);
}

function isEdgeDimmed(e: LayoutEdge): boolean {
  if (!hoveredNodeId.value) return false;
  return e.source.node_id !== hoveredNodeId.value && e.target.node_id !== hoveredNodeId.value;
}
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 sm:px-6 py-6 sm:py-8">
    <PageHeader
      title="知识图谱"
      subtitle="实体与关系的可视化 — 拖拽节点、搜索过滤、点击查看详细信息"
    />

    <!-- Filters -->
    <div class="card mb-6 p-4">
      <div class="flex flex-wrap items-end gap-3">
        <div class="flex flex-col gap-1">
          <label class="text-2xs font-semibold uppercase text-surface-400">节点类型</label>
          <select
            v-model="filterNodeType"
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          >
            <option value="">全部类型</option>
            <option value="memory">记忆</option>
            <option value="document">文档</option>
            <option value="concept">概念</option>
            <option value="entity">实体</option>
            <option value="agent">Agent</option>
          </select>
        </div>

        <div class="flex flex-col gap-1 flex-1 min-w-[200px]">
          <label class="text-2xs font-semibold uppercase text-surface-400">搜索节点</label>
          <input
            v-model="searchInput"
            type="text"
            placeholder="输入关键词搜索..."
            class="rounded-lg border border-surface-200 bg-white px-3 py-2 text-sm text-surface-700 placeholder-surface-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            @input="onSearchInput"
          />
        </div>

        <button class="btn btn-ghost btn-sm" @click="filterSearch = ''; searchInput = ''; filterNodeType = ''">
          清除
        </button>

        <span class="text-xs text-surface-400 ml-auto self-end pb-1">
          {{ graphData?.total_nodes ?? 0 }} 个节点 · {{ graphData?.total_edges ?? 0 }} 条边
        </span>
      </div>
    </div>

    <!-- Legend -->
    <div class="card mb-4 p-3">
      <div class="flex flex-wrap items-center gap-4 text-xs text-surface-500">
        <span class="text-2xs font-semibold uppercase text-surface-400 mr-1">图例</span>
        <span v-for="(color, type) in NODE_COLORS" :key="type" class="flex items-center gap-1.5">
          <span class="inline-block h-3 w-3 rounded-full" :style="{ backgroundColor: color }"></span>
          {{ nodeTypeLabel(type) }}
        </span>
        <span class="flex items-center gap-1.5 ml-4 text-2xs text-surface-400">
          <svg width="20" height="6"><line x1="0" y1="3" x2="20" y2="3" stroke="#94a3b8" stroke-width="1.5"/></svg>
          关系
        </span>
      </div>
    </div>

    <!-- Error -->
    <div
      v-if="isError"
      class="card mb-6 border-red-200 bg-red-50 p-4 text-sm text-red-700"
    >
      加载图谱数据失败: {{ (error as Error)?.message }}
    </div>

    <!-- Graph Canvas -->
    <div class="card overflow-hidden relative">
      <!-- Loading overlay -->
      <div
        v-if="isLoading"
        class="absolute inset-0 z-10 flex items-center justify-center bg-white/80"
      >
        <div class="flex items-center gap-2 text-sm text-surface-400">
          <svg class="h-4 w-4 animate-spin" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
          加载图谱...
        </div>
      </div>

      <!-- Guide page: graph building in progress -->
      <div
        v-else-if="!isLoading && layoutNodes.length === 0"
        class="flex flex-col items-center justify-center py-20 px-6"
      >
        <!-- Animated network icon -->
        <div class="relative mb-6">
          <svg xmlns="http://www.w3.org/2000/svg" class="h-20 w-20 text-brand-300" fill="none" viewBox="0 0 80 80" stroke="currentColor" stroke-width="1.5">
            <!-- Nodes -->
            <circle cx="40" cy="20" r="6" fill="currentColor" opacity="0.4">
              <animate attributeName="opacity" values="0.2;0.5;0.2" dur="3s" repeatCount="indefinite" />
            </circle>
            <circle cx="18" cy="50" r="5" fill="currentColor" opacity="0.3">
              <animate attributeName="opacity" values="0.2;0.5;0.2" dur="3s" begin="0.8s" repeatCount="indefinite" />
            </circle>
            <circle cx="62" cy="50" r="5" fill="currentColor" opacity="0.3">
              <animate attributeName="opacity" values="0.2;0.5;0.2" dur="3s" begin="1.6s" repeatCount="indefinite" />
            </circle>
            <circle cx="30" cy="65" r="4" fill="currentColor" opacity="0.2">
              <animate attributeName="opacity" values="0.1;0.4;0.1" dur="3s" begin="0.4s" repeatCount="indefinite" />
            </circle>
            <circle cx="52" cy="65" r="4" fill="currentColor" opacity="0.2">
              <animate attributeName="opacity" values="0.1;0.4;0.1" dur="3s" begin="1.2s" repeatCount="indefinite" />
            </circle>
            <!-- Edges (dashed to suggest building) -->
            <line x1="40" y1="26" x2="21" y2="46" stroke="currentColor" stroke-width="1" stroke-dasharray="4 3" opacity="0.3">
              <animate attributeName="stroke-dashoffset" from="0" to="14" dur="2s" repeatCount="indefinite" />
            </line>
            <line x1="40" y1="26" x2="59" y2="46" stroke="currentColor" stroke-width="1" stroke-dasharray="4 3" opacity="0.3">
              <animate attributeName="stroke-dashoffset" from="0" to="14" dur="2s" begin="0.5s" repeatCount="indefinite" />
            </line>
            <line x1="20" y1="54" x2="30" y2="62" stroke="currentColor" stroke-width="1" stroke-dasharray="4 3" opacity="0.2">
              <animate attributeName="stroke-dashoffset" from="0" to="14" dur="2s" begin="1s" repeatCount="indefinite" />
            </line>
            <line x1="60" y1="54" x2="52" y2="62" stroke="currentColor" stroke-width="1" stroke-dasharray="4 3" opacity="0.2">
              <animate attributeName="stroke-dashoffset" from="0" to="14" dur="2s" begin="1.5s" repeatCount="indefinite" />
            </line>
          </svg>
          <!-- Spinner ring -->
          <svg class="absolute inset-0 h-20 w-20 animate-spin text-brand-400" style="animation-duration: 4s" viewBox="0 0 80 80" fill="none">
            <circle cx="40" cy="40" r="36" stroke="currentColor" stroke-width="2" stroke-dasharray="8 12" opacity="0.3" />
          </svg>
        </div>

        <h3 class="text-lg font-semibold text-surface-700 mb-2">图谱构建中</h3>
        <p class="text-sm text-surface-400 text-center max-w-md mb-2">
          导入知识后自动生成实体与关系图谱
        </p>
        <p class="text-xs text-surface-300 text-center max-w-sm mb-8">
          系统会对知识文档进行实体抽取、关系识别与图谱构建，完成后即可在此查看可视化图谱。
        </p>

        <!-- Step cards -->
        <div class="flex flex-wrap justify-center gap-4 mb-8 max-w-2xl w-full">
          <div class="flex items-start gap-3 rounded-xl border border-surface-200 bg-white p-4 flex-1 min-w-[200px] max-w-[260px]">
            <span class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-brand-100 text-brand-600 text-xs font-bold">1</span>
            <div>
              <p class="text-sm font-medium text-surface-700">导入文档</p>
              <p class="text-xs text-surface-400 mt-0.5">在知识库中上传文档或导入资源</p>
            </div>
          </div>
          <div class="flex items-start gap-3 rounded-xl border border-surface-200 bg-white p-4 flex-1 min-w-[200px] max-w-[260px]">
            <span class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-brand-100 text-brand-600 text-xs font-bold">2</span>
            <div>
              <p class="text-sm font-medium text-surface-700">自动处理</p>
              <p class="text-xs text-surface-400 mt-0.5">系统自动抽取实体、识别关系</p>
            </div>
          </div>
          <div class="flex items-start gap-3 rounded-xl border border-surface-200 bg-white p-4 flex-1 min-w-[200px] max-w-[260px]">
            <span class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-brand-100 text-brand-600 text-xs font-bold">3</span>
            <div>
              <p class="text-sm font-medium text-surface-700">查看图谱</p>
              <p class="text-xs text-surface-400 mt-0.5">图谱自动生成，可交互探索</p>
            </div>
          </div>
        </div>

        <!-- CTA button -->
        <button
          class="btn btn-primary"
          @click="router.push('/app/knowledge')"
        >
          <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" stroke-linejoin="round" d="M12 4v16m8-8H4" />
          </svg>
          {{ hasKnowledgeDocs ? '前往知识库' : '导入知识' }}
        </button>

        <!-- Hint if knowledge docs already exist -->
        <p v-if="hasKnowledgeDocs" class="text-xs text-surface-300 mt-4">
          知识文档已导入，图谱正在后台构建中，请稍候刷新。
        </p>
      </div>

      <!-- SVG Graph -->
      <svg
        v-else
        ref="svgRef"
        :viewBox="`${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`"
        class="w-full h-[70vh] select-none cursor-grab"
        :class="{ 'cursor-grabbing': isPanning }"
        @mousedown="onSvgMouseDown"
        @mousemove="onSvgMouseMove"
        @mouseup="onSvgMouseUp"
        @mouseleave="onSvgMouseUp"
        @wheel.prevent="onWheel"
      >
        <!-- Edges -->
        <g>
          <g v-for="(e, i) in layoutEdges" :key="i">
            <path
              :d="edgePath(e)"
              fill="none"
              :stroke="isEdgeDimmed(e) ? '#e2e8f0' : '#94a3b8'"
              :stroke-width="isEdgeDimmed(e) ? 0.8 : 1.5"
              :stroke-dasharray="e.weight < 0.5 ? '4 3' : 'none'"
              :opacity="isEdgeDimmed(e) ? 0.3 : 0.7"
              marker-end="url(#arrowhead)"
            />
            <text
              v-if="e.label && !isEdgeDimmed(e)"
              :x="(e.source.x + e.target.x) / 2"
              :y="(e.source.y + e.target.y) / 2 - 6"
              text-anchor="middle"
              class="fill-surface-400 text-[9px] pointer-events-none"
            >
              {{ e.label }}
            </text>
          </g>
        </g>

        <!-- Arrow marker -->
        <defs>
          <marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
            <polygon points="0 0, 8 3, 0 6" fill="#94a3b8" opacity="0.6" />
          </marker>
        </defs>

        <!-- Nodes -->
        <g v-for="node in layoutNodes" :key="node.node_id">
          <!-- Outer glow for hovered -->
          <circle
            v-if="hoveredNodeId === node.node_id"
            :cx="node.x"
            :cy="node.y"
            :r="node.radius + 6"
            :fill="node.color"
            opacity="0.15"
          />
          <!-- Node circle -->
          <circle
            :cx="node.x"
            :cy="node.y"
            :r="node.radius"
            :fill="node.color"
            :stroke="selectedNodeId === node.node_id ? '#1e293b' : '#fff'"
            :stroke-width="selectedNodeId === node.node_id ? 3 : 2"
            :opacity="isNodeDimmed(node) ? 0.2 : 1"
            class="cursor-pointer transition-opacity"
            @mousedown.stop="onNodeMouseDown($event, node)"
            @click.stop="onNodeClick(node)"
            @mouseenter="hoveredNodeId = node.node_id"
            @mouseleave="hoveredNodeId = null"
          />
          <!-- Type icon letter -->
          <text
            :x="node.x"
            :y="node.y + 1"
            text-anchor="middle"
            dominant-baseline="middle"
            class="fill-white text-[10px] font-bold pointer-events-none"
            :opacity="isNodeDimmed(node) ? 0.2 : 1"
          >
            {{ node.node_type === 'memory' ? 'M' : node.node_type === 'document' ? 'D' : node.node_type === 'concept' ? 'C' : node.node_type === 'entity' ? 'E' : 'A' }}
          </text>
          <!-- Label -->
          <text
            :x="node.x"
            :y="node.y + node.radius + 14"
            text-anchor="middle"
            class="fill-surface-600 text-[11px] font-medium pointer-events-none"
            :opacity="isNodeDimmed(node) ? 0.2 : 1"
          >
            {{ node.label.length > 16 ? node.label.slice(0, 16) + '…' : node.label }}
          </text>
        </g>
      </svg>
    </div>

    <!-- Node Detail Drawer -->
    <DetailDrawer
      :open="drawerOpen"
      title="节点详情"
      width="w-[480px] max-w-full"
      @close="drawerOpen = false; selectedNodeId = null"
    >
      <div v-if="detailLoading" class="py-4">
        <LoadingSkeleton variant="detail" />
      </div>

      <template v-else-if="nodeDetail">
        <dl class="space-y-4">
          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">节点ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-700 break-all">{{ nodeDetail.node_id }}</dd>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">类型</dt>
              <dd class="mt-1">
                <span
                  class="badge text-2xs font-medium"
                  :style="{ backgroundColor: NODE_COLORS[nodeDetail.node_type] + '20', color: NODE_COLORS[nodeDetail.node_type] }"
                >
                  {{ nodeTypeLabel(nodeDetail.node_type) }}
                </span>
              </dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">项目ID</dt>
              <dd class="mt-1 font-mono text-xs text-surface-500 truncate">
                {{ nodeDetail.project_id ? String(nodeDetail.project_id).slice(0, 12) + '…' : '—' }}
              </dd>
            </div>
          </div>

          <div>
            <dt class="text-2xs font-semibold uppercase text-surface-400">标签</dt>
            <dd class="mt-1 text-sm text-surface-700 font-medium">{{ nodeDetail.label }}</dd>
          </div>

          <div v-if="nodeDetail.description">
            <dt class="text-2xs font-semibold uppercase text-surface-400">描述</dt>
            <dd class="mt-1 text-sm text-surface-600 leading-relaxed">{{ nodeDetail.description }}</dd>
          </div>

          <div v-if="nodeDetail.source_id">
            <dt class="text-2xs font-semibold uppercase text-surface-400">来源ID</dt>
            <dd class="mt-1 font-mono text-xs text-surface-500 break-all">{{ nodeDetail.source_id }}</dd>
          </div>

          <!-- Properties -->
          <div v-if="nodeDetail.properties && Object.keys(nodeDetail.properties).length > 0">
            <dt class="text-2xs font-semibold uppercase text-surface-400 mb-2">属性</dt>
            <div class="rounded-lg border border-surface-200 divide-y divide-surface-100 overflow-hidden">
              <div
                v-for="(val, key) in nodeDetail.properties"
                :key="key"
                class="flex items-start gap-3 px-3 py-2"
              >
                <span class="text-xs font-medium text-surface-500 shrink-0">{{ key }}</span>
                <span class="text-xs text-surface-700 font-mono break-all">{{ val }}</span>
              </div>
            </div>
          </div>

          <!-- Connected edges for this node -->
          <div v-if="nodeDetail">
            <dt class="text-2xs font-semibold uppercase text-surface-400 mb-2">
              关联关系 ({{ layoutEdges.filter(e => e.source.node_id === nodeDetail!.node_id || e.target.node_id === nodeDetail!.node_id).length }})
            </dt>
            <div
              v-if="layoutEdges.filter(e => e.source.node_id === nodeDetail!.node_id || e.target.node_id === nodeDetail!.node_id).length > 0"
              class="space-y-1"
            >
              <div
                v-for="(e, i) in layoutEdges.filter(e => e.source.node_id === nodeDetail!.node_id || e.target.node_id === nodeDetail!.node_id)"
                :key="i"
                class="flex items-center gap-2 rounded-lg border border-surface-200 px-3 py-2 text-xs"
              >
                <span
                  class="inline-block h-2 w-2 rounded-full shrink-0"
                  :style="{ backgroundColor: e.source.node_id === nodeDetail!.node_id ? e.target.color : e.source.color }"
                ></span>
                <span class="text-surface-600 font-medium">
                  {{ e.source.node_id === nodeDetail.node_id ? e.target.label : e.source.label }}
                </span>
                <span class="text-surface-400 mx-1">—</span>
                <span class="badge text-2xs bg-surface-100 text-surface-600">
                  {{ e.relation_type }}
                </span>
                <span class="text-surface-300 ml-auto font-mono">{{ e.weight.toFixed(2) }}</span>
              </div>
            </div>
            <div v-else class="text-xs text-surface-400 py-2">无关联关系</div>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">创建时间</dt>
              <dd class="mt-1 text-xs">{{ formatTime(nodeDetail.created_at) }}</dd>
            </div>
            <div>
              <dt class="text-2xs font-semibold uppercase text-surface-400">更新时间</dt>
              <dd class="mt-1 text-xs">{{ formatTime(nodeDetail.updated_at) }}</dd>
            </div>
          </div>
        </dl>
      </template>

      <div v-else class="flex flex-col items-center justify-center py-12 text-sm text-surface-400">
        加载节点详情失败
      </div>

      <template #footer>
        <div class="flex items-center justify-between">
          <span class="text-2xs text-surface-400">
            ID: {{ selectedNodeId ? selectedNodeId.slice(0, 8) + '...' : '—' }}
          </span>
          <button class="btn btn-secondary btn-sm" @click="drawerOpen = false; selectedNodeId = null">
            关闭
          </button>
        </div>
      </template>
    </DetailDrawer>
  </div>
</template>
