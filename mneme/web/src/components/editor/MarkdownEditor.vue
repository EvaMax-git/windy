<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { Editor, rootCtx, defaultValueCtx, editorViewCtx, serializerCtx } from '@milkdown/kit/core'
import { commonmark } from '@milkdown/kit/preset/commonmark'

const props = defineProps<{ content: string }>()

const containerRef = ref<HTMLDivElement | null>(null)
const isReady = ref(false)
const errorMsg = ref('')
let editor: Editor | null = null

async function initEditor() {
  if (!containerRef.value) { errorMsg.value = 'No container'; return }
  if (editor) { try { editor.destroy() } catch {}; editor = null; isReady.value = false }
  try {
    editor = await Editor.make()
      .config((ctx: any) => {
        ctx.set(rootCtx, containerRef.value!)
        ctx.set(defaultValueCtx, props.content)
      })
      .use(commonmark)
      .create()
    isReady.value = true
    console.log('Editor ready')
  } catch (err: any) {
    console.error('Editor fail:', err)
    errorMsg.value = err?.message || String(err)
  }
}

function getContent(): string {
  if (!editor) return ''
  try {
    const view = editor.ctx.get(editorViewCtx)
    const serializer = editor.ctx.get(serializerCtx)
    return serializer(view.state.doc) || ''
  } catch {
    return ''
  }
}

onMounted(() => nextTick(initEditor))
onUnmounted(() => { if (editor) { try { editor.destroy() } catch {}; editor = null } })

watch(() => props.content, (val) => {
  if (!editor) return
  try { editor.action.replaceAll(val) } catch {}
})

defineExpose({ getContent, getEditor: () => editor })
</script>

<template>
  <div class="editor-host">
    <div v-if="errorMsg" class="p-4 text-yellow-400 text-sm bg-yellow-400/10 m-4 rounded">
      {{ errorMsg }}
      <button class="ml-2 px-2 py-0.5 rounded bg-blue-500 text-white text-xs" @click="errorMsg='';nextTick(initEditor)">Retry</button>
    </div>
    <div v-else-if="!isReady" class="p-4 text-sm flex items-center gap-2" style="color:#565f89">
      <span class="animate-pulse">Loading editor...</span>
    </div>
    <div ref="containerRef" class="editor-container" :style="{ display: isReady ? '' : 'none' }" />
  </div>
</template>

<style>
.editor-host { height: 100%; }
.editor-container { height: 100%; overflow: auto; }
.editor-container .milkdown {
  min-height: 100%; padding: 2rem 3rem; max-width: 900px; margin: 0 auto;
  font-family: 'Inter', system-ui, -apple-system, sans-serif; font-size: 15px; line-height: 1.72; color: #c0caf5;
}
.editor-container .milkdown h1 { font-size: 1.75rem; font-weight: 700; color: #c0caf5; }
.editor-container .milkdown h2 { font-size: 1.4rem; font-weight: 600; color: #c0caf5; }
.editor-container .milkdown h3 { font-size: 1.15rem; font-weight: 600; color: #c0caf5; }
.editor-container .milkdown p { margin: 0.5em 0; }
.editor-container .milkdown pre {
  background: #1a1b26; border: 1px solid #3b4261; border-radius: 10px; padding: 1.25rem; overflow-x: auto; margin: 1em 0;
}
.editor-container .milkdown pre code {
  font-family: 'JetBrains Mono', monospace; font-size: 0.8125rem; color: #c0caf5;
}
.editor-container .milkdown code {
  font-family: 'JetBrains Mono', monospace; font-size: 0.85em; background: #2a2e40; padding: 0.15em 0.4em; border-radius: 4px; color: #e0af68;
}
.editor-container .ProseMirror { outline: none; white-space: pre-wrap; }
</style>