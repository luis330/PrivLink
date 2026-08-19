-- PrivLink D1 初始化迁移（从 main.py init_storage() 移植）
-- 字段顺序与约束与 SQLite schema 完全一致

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    site_name TEXT,
    icon_rel_path TEXT,
    icon_source_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_status TEXT NOT NULL,
    last_error TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_public INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site_tags (
    site_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (site_id, tag_id),
    FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_site_tags_tag ON site_tags(tag_id);
