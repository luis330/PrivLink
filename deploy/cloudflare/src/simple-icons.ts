/**
 * Simple Icons 静态数据 + 搜索。
 * 构建时通过 scripts/fetch-simple-icons.py 从 unpkg simple-icons 包下载并生成 simple-icons.json。
 *
 * JSON 格式：[{ slug, title, hex, source, ... }, ...]
 */

import SIMPLE_ICONS_DATA from "../assets/simple-icons.json";

export interface SimpleIconEntry {
  title: string;
  slug: string;
  hex: string;
  source: string;
}

export interface SimpleIconItem {
  name: string;
  slug: string;
  url: string;
}

/**
 * 返回全部图标列表；query 为空时返回全部，否则按 name/slug 子串过滤。
 */
export function listIcons(query: string = ""): SimpleIconItem[] {
  const q = query.trim().toLowerCase();
  const entries = SIMPLE_ICONS_DATA as SimpleIconEntry[];

  const matches = (e: SimpleIconEntry) =>
    e.title.toLowerCase().includes(q) || e.slug.toLowerCase().includes(q);

  const selected = q ? entries.filter(matches) : entries;

  return selected.map((e) => ({
    name: e.title,
    slug: e.slug,
    url: `https://cdn.simpleicons.org/${e.slug}`,
  }));
}

/** 根据 slug 获取图标 URL（用于存储 icon_rel_path） */
export function iconUrlForSlug(slug: string): string {
  return `https://cdn.simpleicons.org/${slug}`;
}

/**
 * slug 是否存在于图标库。
 * 对应 Python 端 `PUT /api/sites/{id}` 里对 `_icons_cache` 的存在性校验，
 * 避免把任意字符串拼成 CDN URL 存进 icon_rel_path。
 */
export function hasIconSlug(slug: string): boolean {
  const s = slug.trim().toLowerCase();
  if (!s) return false;
  return (SIMPLE_ICONS_DATA as SimpleIconEntry[]).some(
    (e) => e.slug.toLowerCase() === s
  );
}
