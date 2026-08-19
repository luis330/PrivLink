/**
 * D1 数据库访问层。
 * 使用 typeof D1Database 兼容 wrangler 各版本绑定类型。
 */

import type { D1Database } from "@cloudflare/workers-types";

// ── 初始化（幂等）──────────────────────────────────────

export async function initStorage(db: D1Database): Promise<void> {
  await db.prepare(
    "CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
  ).run();
  await db.prepare(
    "CREATE TABLE IF NOT EXISTS sites (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE, site_name TEXT, icon_rel_path TEXT, icon_source_url TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_status TEXT NOT NULL, last_error TEXT, sort_order INTEGER NOT NULL DEFAULT 0, is_public INTEGER NOT NULL DEFAULT 1)"
  ).run();
  await db.prepare(
    "CREATE TABLE IF NOT EXISTS tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE, created_at TEXT NOT NULL)"
  ).run();
  await db.prepare(
    "CREATE TABLE IF NOT EXISTS site_tags (site_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, PRIMARY KEY (site_id, tag_id), FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE, FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE)"
  ).run();
  await db.prepare(
    "CREATE INDEX IF NOT EXISTS idx_site_tags_tag ON site_tags(tag_id)"
  ).run();
}

// ── 设置查询 ───────────────────────────────────────────

export async function getAppSetting(db: D1Database, key: string): Promise<string | null> {
  const result = await db.prepare("SELECT value FROM app_settings WHERE key = ?").bind(key).first<string | null>("value");
  return result ?? null;
}

export async function setAppSetting(db: D1Database, key: string, value: string): Promise<void> {
  const now = utcNow();
  await db.prepare(
    `INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
     ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`
  ).bind(key, value, now).run();
}

// ── 标签查询 ───────────────────────────────────────────

export async function fetchSiteTags(db: D1Database, siteId: number): Promise<string[]> {
  const rows = await db.prepare(
    `SELECT tags.name FROM site_tags JOIN tags ON tags.id = site_tags.tag_id
     WHERE site_tags.site_id = ? ORDER BY tags.name COLLATE NOCASE ASC`
  ).bind(siteId).all<{ name: string }>();
  return rows.results.map((r) => r.name);
}

export async function fetchAllSiteTags(db: D1Database): Promise<Map<number, string[]>> {
  const rows = await db.prepare(
    `SELECT site_tags.site_id, tags.name FROM site_tags JOIN tags ON tags.id = site_tags.tag_id
     ORDER BY tags.name COLLATE NOCASE ASC`
  ).all<{ site_id: number; name: string }>();
  const result = new Map<number, string[]>();
  for (const row of rows.results) {
    const arr = result.get(row.site_id) ?? [];
    arr.push(row.name);
    result.set(row.site_id, arr);
  }
  return result;
}

// ── 工具 ───────────────────────────────────────────────

export function utcNow(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}
