<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { EditorView, keymap, lineNumbers, highlightActiveLine } from '@codemirror/view'
import { defaultKeymap } from '@codemirror/commands'
import { javascript } from '@codemirror/lang-javascript'
import { python } from '@codemirror/lang-python'
import { json } from '@codemirror/lang-json'
import { markdown } from '@codemirror/lang-markdown'
import { oneDark } from '@codemirror/theme-one-dark'
import type { Extension } from '@codemirror/state'

const props = defineProps<{
  content: string
  language: string
}>()

const emit = defineEmits<{ change: [content: string] }>()

const containerRef = ref<HTMLDivElement | null>(null)
let view: EditorView | null = null

function langExtension(lang: string): Extension {
  switch (lang) {
    case 'typescript': case 'javascript': case 'ts': case 'js': return javascript()
    case 'python': case 'py': return python()
    case 'json': return json()
    case 'markdown': case 'md': return markdown()
    default: return javascript()
  }
}

function createEditor() {
  if (!containerRef.value) return
  view = new EditorView({
    doc: props.content,
    extensions: [
      lineNumbers(),
      highlightActiveLine(),
      keymap.of(defaultKeymap),
      langExtension(props.language),
      oneDark,
      EditorView.updateListener.of((update) => {
        if (update.docChanged) {
          emit('change', update.state.doc.toString())
        }
      }),
    ],
    parent: containerRef.value,
  })
}

function destroyEditor() { view?.destroy(); view = null }

onMounted(() => nextTick(createEditor))
onUnmounted(destroyEditor)

watch(() => props.content, (newContent) => {
  if (!view) return
  const current = view.state.doc.toString()
  if (newContent !== current) {
    view.dispatch({ changes: { from: 0, to: current.length, insert: newContent } })
  }
})

watch(() => props.language, () => { destroyEditor(); nextTick(createEditor) })
</script>

<template>
  <div class="cm-editor-wrapper">
    <div class="cm-lang-badge">{{ language }}</div>
    <div ref="containerRef" class="cm-container" />
  </div>
</template>

<style>
.cm-editor-wrapper {
  position: relative;
  height: 100%;
  background: #1a1b26;
}
.cm-lang-badge {
  position: absolute;
  top: 0.75rem;
  right: 1.25rem;
  z-index: 10;
  font-size: 0.625rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 600;
  color: #7aa2f7;
  background: #1f2335;
  padding: 0.2rem 0.6rem;
  border-radius: 5px;
  border: 1px solid #3b4261;
}
.cm-container { height: 100%; overflow: auto; }
.cm-container .cm-editor { height: 100%; }
.cm-container .cm-editor .cm-scroller {
  font-family: 'JetBrains Mono', 'Cascadia Code', monospace;
  font-size: 0.875rem;
  line-height: 1.65;
  padding: 1rem 0;
}
</style>