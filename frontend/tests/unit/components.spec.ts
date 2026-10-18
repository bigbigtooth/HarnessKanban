/**
 * 公共组件测试：显式状态（加载/空/错误+重试）、键盘可访问弹窗（焦点陷阱、ESC、aria），
 * 以及“组件不写 localStorage”的断言。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { ApiError } from '@/api/client'
import EmptyState from '@/components/EmptyState.vue'
import LoadingState from '@/components/LoadingState.vue'
import ErrorState from '@/components/ErrorState.vue'
import RequestStatePanel from '@/components/RequestStatePanel.vue'
import BaseDialog from '@/components/BaseDialog.vue'
import { trackLocalStorage } from './helpers/fetchStub'

let tracker: ReturnType<typeof trackLocalStorage>
let wrappers: VueWrapper[] = []

function trackedMount(component: Parameters<typeof mount>[0], options: Parameters<typeof mount>[1] = {}): VueWrapper {
  const wrapper = mount(component, options) as VueWrapper
  wrappers.push(wrapper)
  return wrapper
}

beforeEach(() => {
  tracker = trackLocalStorage()
  wrappers = []
})

afterEach(() => {
  for (const wrapper of wrappers) {
    try {
      wrapper.unmount()
    } catch {
      // 测试内已手动 unmount（Teleport 清理需要）。
    }
  }
  tracker.restore()
})

describe('状态组件', () => {
  it('加载态使用 role=status，且不渲染任何数据占位', () => {
    const wrapper = trackedMount(LoadingState)
    expect(wrapper.find('[data-testid="state-loading"]').exists()).toBe(true)
    expect(wrapper.attributes('role')).toBe('status')
    expect(wrapper.text()).toContain('加载中')
  })

  it('空态只在服务端确认空结果时使用', () => {
    const wrapper = trackedMount(EmptyState, { props: { title: '暂无任务', description: '当前筛选下没有任务' } })
    expect(wrapper.find('[data-testid="state-empty"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('暂无任务')
  })

  it('错误态显示原因、错误码与请求号，并可重试', async () => {
    const error = new ApiError({
      code: 'VERSION_CONFLICT',
      message: '资源版本已变化，请重新读取当前状态',
      httpStatus: 409,
      requestId: 'req-409-1',
      kind: 'conflict',
    })
    const wrapper = trackedMount(ErrorState, { props: { error } })

    expect(wrapper.attributes('role')).toBe('alert')
    expect(wrapper.find('[data-testid="state-error-message"]').text()).toContain('资源版本已变化')
    expect(wrapper.find('[data-testid="state-error-code"]').text()).toBe('VERSION_CONFLICT')
    expect(wrapper.find('[data-testid="state-error-request-id"]').text()).toBe('req-409-1')
    expect(wrapper.text()).toContain('已填写的内容会为您保留')

    await wrapper.find('[data-testid="state-retry"]').trigger('click')
    expect(wrapper.emitted('retry')).toHaveLength(1)
  })

  it('RequestStatePanel：失败状态绝不渲染为空态，重试事件透传', async () => {
    const error = new ApiError({ code: 'DATABASE_BUSY', message: '数据库繁忙', httpStatus: 503, kind: 'retryable', retryable: true })
    const wrapper = trackedMount(RequestStatePanel, { props: { status: 'error', error } })

    expect(wrapper.find('[data-testid="state-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="state-empty"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="state-loading"]').exists()).toBe(false)

    await wrapper.findComponent(ErrorState).vm.$emit('retry')
    expect(wrapper.emitted('retry')).toHaveLength(1)
  })

  it('RequestStatePanel：loading/empty/cancelled 三态各自显式呈现', () => {
    const loading = trackedMount(RequestStatePanel, { props: { status: 'loading' } })
    expect(loading.find('[data-testid="state-loading"]').exists()).toBe(true)

    const empty = trackedMount(RequestStatePanel, { props: { status: 'empty' } })
    expect(empty.find('[data-testid="state-empty"]').exists()).toBe(true)

    const cancelled = trackedMount(RequestStatePanel, { props: { status: 'cancelled' } })
    expect(cancelled.find('[data-testid="panel-cancelled"]').text()).toContain('筛选条件保持不变')
  })

})

describe('BaseDialog（键盘可访问弹窗）', () => {
  it('aria 属性完整：role=dialog、aria-modal、labelledby/describedby', async () => {
    const wrapper = trackedMount(BaseDialog, {
      props: { open: true, title: '任务详情', description: '骨架说明' },
      slots: { default: '<button data-testid="inner-a">A</button><button data-testid="inner-b">B</button>' },
      attachTo: document.body,
    })
    await nextTick()

    // Teleport 到 body：直接用 document 查询。
    const dialog = document.querySelector<HTMLElement>('[data-testid="base-dialog"]')
    expect(dialog).not.toBeNull()
    expect(dialog!.getAttribute('role')).toBe('dialog')
    expect(dialog!.getAttribute('aria-modal')).toBe('true')
    const labelledBy = dialog!.getAttribute('aria-labelledby')
    expect(labelledBy).toBeTruthy()
    expect(document.getElementById(labelledBy!)?.textContent).toBe('任务详情')
    const describedBy = dialog!.getAttribute('aria-describedby')
    expect(describedBy).toBeTruthy()
    expect(document.getElementById(describedBy!)?.textContent).toBe('骨架说明')
    wrapper.unmount()
  })

  it('打开后焦点进入弹窗；Tab 在内部循环（焦点陷阱）', async () => {
    const wrapper = trackedMount(BaseDialog, {
      props: { open: true, title: '任务详情' },
      slots: { default: '<button data-testid="inner-a">A</button><button data-testid="inner-b">B</button>' },
      attachTo: document.body,
    })
    await nextTick()
    await nextTick()

    const first = document.querySelector<HTMLElement>('[data-testid="dialog-close"]')!
    const last = document.querySelector<HTMLElement>('[data-testid="inner-b"]')!
    expect(document.activeElement).toBe(first)

    last.focus()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
    expect(document.activeElement).toBe(first)

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true }))
    expect(document.activeElement).toBe(last)

    wrapper.unmount()
  })

  it('ESC 关闭弹窗并发出 close 事件', async () => {
    const wrapper = trackedMount(BaseDialog, {
      props: { open: true, title: '任务详情' },
      slots: { default: '<button>A</button>' },
      attachTo: document.body,
    })
    await nextTick()

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })

  it('关闭后恢复原先焦点，且不再监听键盘事件', async () => {
    const trigger = document.createElement('button')
    trigger.textContent = '打开'
    document.body.appendChild(trigger)
    trigger.focus()

    const wrapper = trackedMount(BaseDialog, {
      props: { open: false, title: '任务详情' },
      slots: { default: '<button>A</button>' },
      attachTo: document.body,
    })
    await wrapper.setProps({ open: true })
    await nextTick()
    expect(document.activeElement).not.toBe(trigger)

    await wrapper.setProps({ open: false })
    await nextTick()
    expect(document.activeElement).toBe(trigger)

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(wrapper.emitted('close')).toBeUndefined()
    trigger.remove()
  })
})

describe('localStorage 约束', () => {
  it('状态组件与弹窗渲染过程中没有任何 localStorage 写入（无密钥持久化）', async () => {
    const error = new ApiError({ code: 'VERSION_CONFLICT', message: '冲突', httpStatus: 409, kind: 'conflict' })
    trackedMount(RequestStatePanel, { props: { status: 'error', error } })
    trackedMount(RequestStatePanel, { props: { status: 'loading' } })
    trackedMount(RequestStatePanel, { props: { status: 'empty' } })
    const dialog = trackedMount(BaseDialog, {
      props: { open: true, title: '任务详情' },
      slots: { default: '<input data-testid="secret-input" value="AGENT_TOKEN-明文样例" />' },
      attachTo: document.body,
    })
    await nextTick()
    await dialog.setProps({ open: false })

    expect(tracker.writes).toEqual([])
    expect(window.localStorage.length).toBe(0)
  })
})
