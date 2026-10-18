/**
 * 列表接口分页取数助手。
 *
 * 服务端 `page_size` 上限是 100，而筛选下拉、看板、项目网格都要"看到全部"，
 * 所以统一在客户端把页翻完（默认最多 10 页 = 1000 条，超出属于异常规模）。
 */
import type { Page } from '@/api/types'

export const MAX_LIST_PAGES = 10

export interface PageFetcher<T> {
  (page: number): Promise<Page<T>>
}

/** 按页取完一个列表接口；任一页失败都会抛出，由调用方决定如何呈现。 */
export async function fetchAllPages<T>(
  fetcher: PageFetcher<T>,
  options: { maxPages?: number } = {},
): Promise<{ items: T[]; total: number }> {
  const maxPages = options.maxPages ?? MAX_LIST_PAGES
  const items: T[] = []
  let total = 0
  for (let page = 1; page <= maxPages; page += 1) {
    const response = await fetcher(page)
    items.push(...response.items)
    total = response.total
    if (response.items.length === 0 || items.length >= total) break
  }
  return { items, total }
}
