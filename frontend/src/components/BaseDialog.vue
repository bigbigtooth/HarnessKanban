<script setup lang="ts">
/**
 * 基础弹窗：键盘可访问（焦点陷阱、ESC 关闭、aria 属性），无任何持久化写入。
 * 任务详情等页面弹窗应复用本组件，而不是各自实现。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    open: boolean
    title: string
    description?: string
    closeOnBackdrop?: boolean
    /** 宽弹窗（详情、看板表单）：桌面更宽，窄屏仍为全宽。 */
    size?: 'md' | 'lg'
  }>(),
  { description: '', closeOnBackdrop: true, size: 'md' },
)

const emit = defineEmits<{ close: [] }>()

let dialogCounter = 0
const uid = `base-dialog-${(dialogCounter += 1)}`
const titleId = computed(() => `${uid}-title`)
const descriptionId = computed(() => `${uid}-description`)

const containerRef = ref<HTMLElement | null>(null)
let previouslyFocused: HTMLElement | null = null

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

function focusableElements(): HTMLElement[] {
  const container = containerRef.value
  if (!container) return []
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) => element.offsetParent !== null || element === document.activeElement,
  )
}

function onKeydown(event: KeyboardEvent): void {
  if (!props.open) return
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    emit('close')
    return
  }
  if (event.key !== 'Tab') return
  const focusable = focusableElements()
  const container = containerRef.value
  if (!container) return
  if (focusable.length === 0) {
    event.preventDefault()
    container.focus()
    return
  }
  const first = focusable[0]!
  const last = focusable[focusable.length - 1]!
  const active = document.activeElement as HTMLElement | null
  if (event.shiftKey && (active === first || active === container)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus()
  }
}

function releaseListeners(): void {
  document.removeEventListener('keydown', onKeydown, true)
}

watch(
  () => props.open,
  async (open) => {
    if (open) {
      previouslyFocused = (document.activeElement as HTMLElement | null) ?? null
      document.addEventListener('keydown', onKeydown, true)
      await nextTick()
      const focusable = focusableElements()
      if (focusable.length > 0) focusable[0]!.focus()
      else containerRef.value?.focus()
    } else {
      releaseListeners()
      if (previouslyFocused && typeof previouslyFocused.focus === 'function' && previouslyFocused.isConnected) {
        previouslyFocused.focus()
      }
      previouslyFocused = null
    }
  },
  { immediate: true },
)

onBeforeUnmount(releaseListeners)

defineExpose({ containerRef })
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="dialog-backdrop" data-testid="dialog-backdrop" @click.self="closeOnBackdrop && emit('close')">
      <div
        ref="containerRef"
        class="dialog"
        :class="`dialog--${size}`"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="titleId"
        :aria-describedby="description ? descriptionId : undefined"
        tabindex="-1"
        data-testid="base-dialog"
      >
        <header class="dialog-header">
          <div class="dialog-header__text">
            <h2 :id="titleId" class="dialog-title">{{ title }}</h2>
            <p v-if="description" :id="descriptionId" class="dialog-description">{{ description }}</p>
          </div>
          <button type="button" class="dialog-close" aria-label="关闭对话框" data-testid="dialog-close" @click="emit('close')">
            <span aria-hidden="true">×</span>
          </button>
        </header>
        <div class="dialog-body">
          <slot />
        </div>
        <footer v-if="$slots.footer" class="dialog-footer">
          <slot name="footer" />
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.dialog-backdrop {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgb(16 24 40 / 55%);
  backdrop-filter: blur(2px);
  padding: var(--space-4);
  z-index: 50;
}

.dialog {
  display: flex;
  flex-direction: column;
  width: min(640px, 100%);
  max-height: 88vh;
  background: var(--color-surface);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}

.dialog--lg {
  width: min(920px, 100%);
}

.dialog-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface);
}

.dialog-header__text {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}

.dialog-title {
  margin: 0;
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
}

.dialog-description {
  margin: 0;
  color: var(--color-text-muted);
  font-size: var(--text-sm);
}

.dialog-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 2rem;
  height: 2rem;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-muted);
  font-size: 1.25rem;
  line-height: 1;
  flex: none;
  transition: background var(--transition-fast), color var(--transition-fast);
}

.dialog-close:hover {
  background: var(--color-surface-sunken);
  color: var(--color-text);
}

.dialog-body {
  padding: var(--space-5);
  overflow-y: auto;
  flex: 1;
}

.dialog-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--color-border);
  background: var(--color-surface-muted);
  flex-wrap: wrap;
}

@media (max-width: 640px) {
  .dialog-backdrop {
    padding: 0;
    align-items: stretch;
  }

  .dialog {
    max-height: 100vh;
    height: 100%;
    border-radius: 0;
  }
}
</style>
