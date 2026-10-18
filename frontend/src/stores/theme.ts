/**
 * 主题切换：浅色主题取 docs/html/demo-light.html，深色主题取 docs/html/demo-dark.html。
 *
 * 两套设计令牌都在 `styles/demo.css` 里：未加前缀的规则是浅色，`html[data-theme="dark"]`
 * 前缀的规则是深色覆盖层。因此切换主题只需改 <html> 上的 data-theme 属性。
 */
import { ref } from 'vue'

export type ThemeName = 'light' | 'dark'

export const THEME_STORAGE_KEY = 'harvesstask.theme'
export const DEFAULT_THEME: ThemeName = 'light'

function isThemeName(value: unknown): value is ThemeName {
  return value === 'light' || value === 'dark'
}

function readStoredTheme(): ThemeName {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    return isThemeName(stored) ? stored : DEFAULT_THEME
  } catch {
    // 隐私模式等场景下 localStorage 不可用：主题仍可切换，只是不记忆。
    return DEFAULT_THEME
  }
}

/** 当前主题（模块级单例，全应用共享）。 */
export const theme = ref<ThemeName>(DEFAULT_THEME)

function applyToDocument(name: ThemeName): void {
  if (typeof document === 'undefined') return
  document.documentElement.dataset.theme = name
  document.documentElement.style.colorScheme = name
}

/** 应用启动时调用：把已保存（或默认）的主题写到 <html> 上。 */
export function initTheme(): ThemeName {
  theme.value = readStoredTheme()
  applyToDocument(theme.value)
  return theme.value
}

export function setTheme(name: ThemeName): ThemeName {
  theme.value = name
  applyToDocument(name)
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, name)
  } catch {
    // 存不下就只在本次会话生效。
  }
  return name
}

export function toggleTheme(): ThemeName {
  return setTheme(theme.value === 'light' ? 'dark' : 'light')
}
