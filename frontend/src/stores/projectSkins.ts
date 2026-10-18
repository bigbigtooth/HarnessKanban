/**
 * 项目外观（封面配色 + emoji）：后端项目模型没有这两个字段，属于本机外观偏好，
 * 因此保存在 localStorage（`harvesstask.project-skins`），换浏览器/清缓存会回到默认值。
 *
 * 默认值由项目 id 哈希得出（稳定、无需存储）；用户显式选择后写入本地并优先生效。
 */
import { ref } from 'vue'

import { PROJECT_COVERS, PROJECT_EMOJIS, paletteFor, projectCoverView, type CoverPalette } from './home'

export const PROJECT_SKINS_STORAGE_KEY = 'harvesstask.project-skins'

export interface ProjectSkin {
  cover: number
  emoji: string
}

type SkinMap = Record<string, ProjectSkin>

function readSkins(): SkinMap {
  try {
    const raw = window.localStorage.getItem(PROJECT_SKINS_STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as unknown
    if (!parsed || typeof parsed !== 'object') return {}
    const result: SkinMap = {}
    for (const [projectId, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (!value || typeof value !== 'object') continue
      const skin = value as Partial<ProjectSkin>
      const cover = Number(skin.cover)
      if (!Number.isInteger(cover) || cover < 0 || cover >= PROJECT_COVERS.length) continue
      result[projectId] = { cover, emoji: isKnownEmoji(skin.emoji) ? skin.emoji! : PROJECT_EMOJIS[0] }
    }
    return result
  } catch {
    return {}
  }
}

function isKnownEmoji(value: unknown): value is string {
  return typeof value === 'string' && (PROJECT_EMOJIS as readonly string[]).includes(value)
}

export const projectSkins = ref<SkinMap>(readSkins())

function persist(): void {
  try {
    window.localStorage.setItem(PROJECT_SKINS_STORAGE_KEY, JSON.stringify(projectSkins.value))
  } catch {
    // 存不下就只在本次会话生效。
  }
}

/** 项目的封面（配色由 cover 索引决定，emoji 单独存）。 */
export function projectSkin(projectId: string): { cover: ProjectSkin; palette: CoverPalette } {
  const stored = projectSkins.value[projectId]
  const fallback = projectCoverView(projectId)
  const cover: ProjectSkin = stored ?? { cover: fallback.index, emoji: fallback.emoji }
  // 色板跟随主题（深色用 demo-dark 的霓虹色），emoji 与配色索引由用户选择。
  return { cover, palette: paletteFor(cover.cover) }
}

export function setProjectSkin(projectId: string, patch: Partial<ProjectSkin>): void {
  const current = projectSkin(projectId).cover
  projectSkins.value = { ...projectSkins.value, [projectId]: { ...current, ...patch } }
  persist()
}

/** 测试用：清掉本机外观偏好。 */
export function resetProjectSkins(): void {
  projectSkins.value = {}
  try {
    window.localStorage.removeItem(PROJECT_SKINS_STORAGE_KEY)
  } catch {
    // 忽略
  }
}
