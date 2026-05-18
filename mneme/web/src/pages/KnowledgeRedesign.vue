<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import MarkdownEditor from '@/components/editor/MarkdownEditor.vue'
import CodeMirrorEditor from '@/components/editor/CodeMirrorEditor.vue'
import TreeItem from '@/components/TreeItem.vue'
import type { TreeNode, DocContent, IndexStateItem } from '@/api/client'

const log = (...args: any[]) => console.log('[Kv2]', ...args)

const sidebarOpen = ref(true)
const panelOpen = ref(false)
const tree = ref<TreeNode[]>([])
const selectedNode = ref<TreeNode | null>(null)
const docContent = ref<DocContent | null>(null)
const editableContent = ref('')
const activeDocId = ref<string | null>(null)
const indexStates = ref<IndexStateItem[]>([])
const projectId = ref('')
const loading = ref(false)
const treeLoading = ref(false)
const treeFilter = ref('')
const toastMsg = ref('')
const toastKind = ref<'ok'|'err'>('ok')
const saveState = ref<'saved'|'saving'|'error'|'unsaved'>('saved')
const markdownEditorRef = ref<InstanceType<typeof MarkdownEditor> | null>(null)
const codeMirrorRef = ref<InstanceType<typeof CodeMirrorEditor> | null>(null)
let toastTimer: any = null

const projects = ref<Array<{ id: string; name: string }>>([])

function uid(): string {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16)
  })
}

function toast(msg: string, kind: 'ok'|'err' = 'ok') {
  toastMsg.value = msg; toastKind.value = kind
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toastMsg.value = '' }, 3000)
}

async function apiGet(path: string) {
  const url = '/api/v4' + path
  const r = await fetch(url, { credentials: 'include' })
  const j = await r.json()
  if (!r.ok) throw new Error(j?.error?.message || r.statusText)
  return j?.data || j
}

async function loadProjects() {
  try {
    const data = await apiGet('/projects')
    const arr = Array.isArray(data) ? data : (data?.items || [])
    if (arr.length === 0) { toast('No projects', 'err'); return }
    projects.value = arr.map((p: any) => ({ id: p.project_id || p.id, name: p.name || p.project_code || p.id }))
    projectId.value = projects.value[0]!.id
    await loadTree()
  } catch (e: any) { toast('Failed: ' + e.message, 'err') }
}

async function loadTree() {
  if (!projectId.value) return
  treeLoading.value = true
  try { const data = await apiGet('/projects/' + projectId.value + '/tree'); tree.value = data?.tree || [] }
  catch (e: any) { toast('Tree: ' + e.message, 'err') }
  finally { treeLoading.value = false }
}

const filteredTree = computed(() => {
  if (!treeFilter.value) return tree.value
  const q = treeFilter.value.toLowerCase()
  function filter(nodes: TreeNode[]): TreeNode[] {
    const out: TreeNode[] = []
    for (const n of nodes) {
      const match = n.name.toLowerCase().includes(q)
      const fc = n.children ? filter(n.children) : undefined
      if (match || (fc && fc.length)) out.push({ ...n, children: fc || n.children })
    }
    return out
  }
  return filter(tree.value)
})

async function selectNode(node: TreeNode) {
  if (node.type === 'folder' || !node.document_id) return
  selectedNode.value = node; loading.value = true; panelOpen.value = true
  try {
    const doc = await apiGet('/knowledge/documents/' + node.document_id + '/v2/content')
    docContent.value = doc; editableContent.value = doc.content_markdown || ''
    activeDocId.value = doc.document_id; saveState.value = 'saved'
    const st = await apiGet('/knowledge/documents/' + doc.document_id + '/index-states')
    indexStates.value = st?.backends || []
  } catch (e: any) { toast('Load: ' + e.message, 'err') }
  finally { loading.value = false }
}

function onEditorChange(content: string) {
  if (content !== editableContent.value) { editableContent.value = content; saveState.value = 'unsaved' }
}

async function manualSave() {
  if (!activeDocId.value) return
  let content = ''
  if (markdownEditorRef.value) content = markdownEditorRef.value.getContent()
  else if (codeMirrorRef.value) content = editableContent.value
  if (!content) { toast('Nothing to save', 'err'); return }

  saveState.value = 'saving'
  try {
    const r = await fetch('/api/v4/knowledge/documents/' + activeDocId.value + '/v2/content', {
      method: 'PATCH', credentials: 'include',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': uid() },
      body: JSON.stringify({ content_markdown: content }),
    })
    const j = await r.json()
    if (!r.ok) throw new Error('HTTP ' + r.status)
    saveState.value = 'saved'
    log('SAVED v' + j.new_version)
    toast('Saved v' + (j.new_version || '?') + ': ' + content.slice(0, 40))
  } catch (e: any) { saveState.value = 'error'; toast('Save failed: ' + (e.message || '?'), 'err') }
}

async function handleNewDoc() {
  const title = prompt('Document title:')
  if (!title || !projectId.value) return
  try {
    const r = await fetch('/api/v4/knowledge/documents/v2', {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': uid() },
      body: JSON.stringify({ project_id: projectId.value, title, content_markdown: '# ' + title, lang: 'markdown' }),
    })
    const j = await r.json()
    if (!r.ok) { toast('Create failed: HTTP ' + r.status, 'err'); return }
    const d = j?.data || j
    if (d?.document_id) { await loadTree(); toast('Created: ' + title); setTimeout(() => { const nd = findNode(tree.value, d.document_id); if (nd) selectNode(nd) }, 200) }
  } catch (e: any) { toast('Create: ' + e.message, 'err') }
}

function findNode(nodes: TreeNode[], did: string): TreeNode | null {
  for (const n of nodes) { if (n.document_id === did) return n; if (n.children) { const f = findNode(n.children, did); if (f) return f } }
  return null
}

function onKeydown(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); manualSave() }
  if ((e.ctrlKey || e.metaKey) && e.key === 'b') { e.preventDefault(); sidebarOpen.value = !sidebarOpen.value }
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault() }
}
onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))

const useCodeEditor = computed(() => {
  if (!docContent.value) return false
  return ['typescript','javascript','python','json'].includes(docContent.value.lang || 'markdown')
})

loadProjects()
</script>

<template>
  <div class="app-shell">
    <!-- LEFT SIDEBAR -->
    <aside :class="sidebarOpen ? 'w-64' : 'w-0'" class="sidebar">
      <div class="w-64 h-full flex flex-col">
        <div class="p-3 space-y-2 border-b" style="border-color:#3b4261">
          <select v-model="projectId" class="w-full rounded-lg px-3 py-2 text-sm border-0 outline-none cursor-pointer" style="background:#2a2e40;color:#c0caf5" @change="loadTree">
            <option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
          <div class="relative">
            <input v-model="treeFilter" type="text" placeholder="Filter..." class="w-full rounded-lg px-3 py-1.5 text-xs border-0 outline-none" style="background:#2a2e40;color:#c0caf5;padding-left:2rem" />
            <span class="absolute left-2 top-1/2 -translate-y-1/2 text-xs opacity-50">&#128269;</span>
          </div>
        </div>
        <div class="flex-1 overflow-y-auto py-1 px-1">
          <div v-if="treeLoading" class="flex items-center justify-center py-12"><span class="text-xs animate-pulse" style="color:#565f89">Loading...</span></div>
          <div v-else-if="filteredTree.length===0 && tree.length>0" class="flex flex-col items-center py-12 px-4">
            <span class="text-xs" style="color:#565f89">No matches</span>
          </div>
          <div v-else-if="filteredTree.length===0" class="flex flex-col items-center py-16 px-6">
            <div class="text-3xl mb-4 opacity-20">&#128462;</div>
            <div class="text-xs font-medium mb-1" style="color:#565f89">Empty project</div>
            <div class="text-xs mb-4 text-center" style="color:#3b4261">Create your first document to get started</div>
            <button class="text-xs px-4 py-1.5 rounded-lg font-medium" style="background:#7aa2f7;color:#1a1b26" @click="handleNewDoc">+ New Document</button>
          </div>
          <template v-else>
            <TreeItem v-for="node in filteredTree" :key="'t'+ (node.document_id||node.path||'') + node.name" :node="node" :selected="selectedNode" @select="selectNode" />
          </template>
        </div>
        <div class="p-2 border-t flex gap-1" style="border-color:#3b4261">
          <button class="flex-1 text-xs py-2 rounded-lg font-medium transition-all hover:opacity-90" style="background:#7aa2f7;color:#1a1b26" @click="handleNewDoc">+ New</button>
          <button class="text-xs px-3 py-2 rounded-lg transition-all hover:bg-white/5" style="color:#565f89" title="Toggle sidebar (Ctrl+B)" @click="sidebarOpen=!sidebarOpen">&laquo;</button>
        </div>
      </div>
    </aside>

    <!-- MAIN -->
    <main class="flex-1 flex flex-col min-w-0">
      <div class="flex items-center justify-between px-4 py-2 border-b shrink-0" style="background:#1f2335;border-color:#3b4261">
        <div class="flex items-center gap-3 min-w-0">
          <button class="text-lg p-1 rounded hover:opacity-80 transition-opacity" style="color:#a9b1d6" @click="sidebarOpen=!sidebarOpen" title="Toggle sidebar">&#9776;</button>
          <span v-if="selectedNode" class="text-sm font-medium truncate" style="color:#c0caf5">{{ selectedNode.name }}</span>
          <span v-else class="text-sm" style="color:#565f89">No document selected</span>
        </div>
        <div class="flex items-center gap-2 flex-shrink-0">
          <span v-if="docContent" class="text-xs px-2 py-0.5 rounded font-medium uppercase tracking-wider" style="background:#2a2e40;color:#7aa2f7">{{ docContent.lang }}</span>
          <button v-if="selectedNode" class="text-xs px-4 py-1.5 rounded-md font-medium transition-all flex items-center gap-1.5" :class="{'bg-blue-500 text-white hover:bg-blue-400':saveState==='unsaved','bg-green-500/20 text-green-400':saveState==='saved','bg-yellow-500/20 text-yellow-400 animate-pulse':saveState==='saving','bg-red-500/20 text-red-400 hover:bg-red-500/30':saveState==='error'}" :disabled="saveState==='saving'" @click="manualSave">
            <span v-if="saveState==='saved'">&#10003;</span><span v-else-if="saveState==='saving'">&#8635;</span><span v-else-if="saveState==='error'">&#10007;</span><span v-else>&#x1F4BE;</span>
            {{ saveState==='saved'?'Saved':saveState==='saving'?'Saving...':saveState==='error'?'Retry':'Save' }}
          </button>
          <button class="text-xs px-2.5 py-1.5 rounded-md transition-all hover:bg-white/5" style="color:#a9b1d6" :class="panelOpen ? 'bg-white/10' : ''" @click="panelOpen=!panelOpen">Info</button>
        </div>
      </div>

      <div class="flex-1 overflow-hidden">
        <div v-if="!selectedNode" class="h-full flex items-center justify-center select-none">
          <div class="text-center max-w-sm">
            <div class="text-5xl mb-8 opacity-20">&#128221;</div>
            <div class="text-lg font-semibold mb-2" style="color:#a9b1d6">Welcome</div>
            <div class="text-sm mb-6" style="color:#565f89">Select a document from the sidebar or create a new one</div>
            <div class="flex flex-col gap-2 text-xs" style="color:#3b4261">
              <div class="flex gap-3 justify-center"><kbd class="px-2 py-0.5 rounded border text-[10px]" style="border-color:#3b4261;background:#1f2335">Ctrl+N</kbd><span>New doc</span></div>
              <div class="flex gap-3 justify-center"><kbd class="px-2 py-0.5 rounded border text-[10px]" style="border-color:#3b4261;background:#1f2335">Ctrl+S</kbd><span>Save</span></div>
              <div class="flex gap-3 justify-center"><kbd class="px-2 py-0.5 rounded border text-[10px]" style="border-color:#3b4261;background:#1f2335">Ctrl+B</kbd><span>Sidebar</span></div>
            </div>
          </div>
        </div>
        <div v-else-if="loading" class="h-full flex items-center justify-center"><span class="text-sm animate-pulse" style="color:#565f89">Loading document...</span></div>
        <CodeMirrorEditor v-else-if="useCodeEditor" ref="codeMirrorRef" :key="activeDocId" :content="editableContent" :language="docContent?.lang||'javascript'" @change="onEditorChange" />
        <MarkdownEditor v-else ref="markdownEditorRef" :key="activeDocId" :content="editableContent" />
      </div>

      <div v-if="selectedNode" class="flex items-center justify-between px-4 py-1.5 border-t text-xs shrink-0" style="background:#1f2335;border-color:#3b4261">
        <div class="flex items-center gap-2">
          <span class="w-2 h-2 rounded-full" :class="{'bg-green-400':saveState==='saved','bg-yellow-400':saveState==='saving'||saveState==='unsaved','bg-red-400':saveState==='error'}"/>
          <span :style="{color:saveState==='saved'?'#9ece6a':saveState==='saving'||saveState==='unsaved'?'#e0af68':'#f7768e'}">{{ saveState==='saved'?'Saved':saveState==='saving'?'Saving...':saveState==='unsaved'?'Edited':'Error' }}</span>
        </div>
        <span style="color:#565f89">v{{ docContent?.current_version }}</span>
      </div>
    </main>

    <!-- RIGHT PANEL -->
    <aside v-if="panelOpen&&selectedNode" class="w-64 shrink-0 border-l overflow-y-auto" style="background:#1f2335;border-color:#3b4261">
      <div class="p-4 space-y-5">
        <section>
          <h3 class="text-xs font-semibold mb-3 uppercase tracking-wider" style="color:#565f89">Index States</h3>
          <div v-if="indexStates.length===0" class="text-xs" style="color:#565f89">No indexes</div>
          <div v-for="s in indexStates" :key="s.backend_type" class="flex justify-between items-center py-1.5">
            <span class="text-xs font-medium capitalize" style="color:#a9b1d6">{{ s.backend_type }}</span>
            <span class="text-xs px-2 py-0.5 rounded-full font-medium capitalize" :class="{'text-green-400 bg-green-400/10':s.state==='ready','text-yellow-400 bg-yellow-400/10':s.state==='stale','text-red-400 bg-red-400/10':s.state==='failed','text-gray-500 bg-gray-500/10':s.state!=='ready'&&s.state!=='stale'&&s.state!=='failed'}">{{ s.state }}</span>
          </div>
        </section>
        <section v-if="docContent">
          <h3 class="text-xs font-semibold mb-3 uppercase tracking-wider" style="color:#565f89">Info</h3>
          <dl class="space-y-2 text-xs">
            <div class="flex justify-between"><dt style="color:#565f89">Version</dt><dd style="color:#a9b1d6">{{ docContent.current_version }}</dd></div>
            <div class="flex justify-between"><dt style="color:#565f89">Language</dt><dd style="color:#a9b1d6">{{ docContent.lang }}</dd></div>
            <div v-if="docContent.content_hash" class="flex justify-between"><dt style="color:#565f89">Hash</dt><dd class="font-mono text-[10px] truncate ml-2" style="color:#565f89;max-width:100px">{{ docContent.content_hash.slice(0,12) }}</dd></div>
          </dl>
        </section>
      </div>
    </aside>

    <Transition name="toast"><div v-if="toastMsg" class="toast" :class="toastKind==='err'?'toast-err':'toast-ok'">{{ toastMsg }}</div></Transition>
  </div>
</template>

<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',system-ui,-apple-system,sans-serif}
.app-shell{display:flex;height:100vh;overflow:hidden;background:#1a1b26;color:#c0caf5}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#3b4261;border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:#565f89}
.sidebar{background:#1f2335;border-color:#3b4261;width:260px;flex-shrink:0;border-right-width:1px;overflow:hidden;transition:width .2s}
.toast{position:fixed;bottom:2rem;left:50%;transform:translateX(-50%);padding:.625rem 1.5rem;border-radius:9999px;font-size:.8125rem;font-weight:500;z-index:9999;pointer-events:none;box-shadow:0 8px 32px rgba(0,0,0,.4)}
.toast-ok{background:#9ece6a;color:#1a1b26}
.toast-err{background:#f7768e;color:#1a1b26}
.toast-enter-active{transition:all .25s ease-out}
.toast-leave-active{transition:all .2s ease-in}
.toast-enter-from,.toast-leave-to{opacity:0;transform:translateX(-50%) translateY(1rem)}
select option{background:#1f2335;color:#c0caf5}
</style>