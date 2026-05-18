import { ref, watch, type Ref } from 'vue'

export type SaveState = 'saved' | 'saving' | 'error' | 'unsaved'

export function useAutoSave(
  content: Ref<string>,
  documentId: Ref<string | null>,
) {
  const saveState = ref<SaveState>('saved')
  const lastSavedContent = ref(content.value)
  let timer: ReturnType<typeof setTimeout> | null = null

  // Generate simple UUID for Idempotency-Key
  function uid(): string {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
      const r = Math.random() * 16 | 0
      return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16)
    })
  }

  watch(
    content,
    (newVal) => {
      if (!documentId.value) return
      if (newVal === lastSavedContent.value) {
        saveState.value = 'saved'
        return
      }
      saveState.value = 'unsaved'
      if (timer) clearTimeout(timer)
      timer = setTimeout(async () => {
        if (!documentId.value) return
        saveState.value = 'saving'
        try {
          const r = await fetch(
            '/api/v4/knowledge/documents/' + documentId.value + '/v2/content',
            {
              method: 'PATCH',
              credentials: 'include',
              headers: {
                'Content-Type': 'application/json',
                'Idempotency-Key': uid(),
              },
              body: JSON.stringify({ content_markdown: newVal }),
            },
          )
          if (!r.ok) throw new Error('Save failed: ' + r.status)
          lastSavedContent.value = newVal
          saveState.value = 'saved'
        } catch {
          saveState.value = 'error'
        }
      }, 800)
    },
  )

  async function flush() {
    if (timer) { clearTimeout(timer); timer = null }
    if (saveState.value === 'unsaved' && documentId.value) {
      saveState.value = 'saving'
      try {
        const r = await fetch(
          '/api/v4/knowledge/documents/' + documentId.value + '/v2/content',
          {
            method: 'PATCH',
            credentials: 'include',
            headers: {
              'Content-Type': 'application/json',
              'Idempotency-Key': uid(),
            },
            body: JSON.stringify({ content_markdown: content.value }),
          },
        )
        if (!r.ok) throw new Error('Flush failed: ' + r.status)
        lastSavedContent.value = content.value
        saveState.value = 'saved'
      } catch {
        saveState.value = 'error'
      }
    }
  }

  function markSaved(content: string) {
    lastSavedContent.value = content
    saveState.value = 'saved'
    if (timer) { clearTimeout(timer); timer = null }
  }

  return { saveState, flush, markSaved }
}

export function saveStateLabel(state: SaveState): string {
  switch (state) {
    case 'saved':   return '已保存'
    case 'saving':  return '保存中...'
    case 'unsaved': return '未保存'
    case 'error':   return '保存失败'
  }
}