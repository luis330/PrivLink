from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import closing, contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator

from privlink import config
from privlink.jsinterop import js_field, to_py

TAG_NAME_MAX_LEN = 20

# SiteItem 需要的列（to_site_item 的输入）；列表与单行查询共用，避免多处手写漂移
SITE_ITEM_COLUMNS = "id, url, site_name, icon_rel_path, updated_at, sort_order, is_public"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class IntegrityConflictError(Exception):
    """唯一约束冲突：SQLite 与 D1 统一映射为本异常，路由据此返回 409。"""


def _is_integrity_conflict(exc: BaseException) -> bool:
    return "UNIQUE constraint" in str(exc)


@contextmanager
def db_connect() -> Iterator[sqlite3.Connection]:
    """本地同步直连（仅供 init_storage 迁移逻辑与测试使用）。"""
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


class Database:
    """异步数据库三件套；行统一归一化为 dict。"""

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        raise NotImplementedError

    async def run(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """执行单条写语句，返回受影响行数。"""
        raise NotImplementedError

    async def batch(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        """原子执行一组写语句（SQLite 单事务 / D1 batch API）。"""
        raise NotImplementedError


class SqliteDatabase(Database):
    """本地 SQLite 后端：每次操作独立连接，经 asyncio.to_thread 卸载阻塞调用。"""

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(config.DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        def work() -> list[dict[str, Any]]:
            with closing(self._connect()) as conn:
                return [dict(row) for row in conn.execute(sql, params).fetchall()]

        return await asyncio.to_thread(work)

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        def work() -> dict[str, Any] | None:
            with closing(self._connect()) as conn:
                row = conn.execute(sql, params).fetchone()
                return dict(row) if row is not None else None

        return await asyncio.to_thread(work)

    async def run(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        def work() -> int:
            with closing(self._connect()) as conn:
                try:
                    cursor = conn.execute(sql, params)
                    conn.commit()
                except sqlite3.IntegrityError as exc:
                    conn.rollback()
                    raise IntegrityConflictError(str(exc)) from exc
                except Exception:
                    conn.rollback()
                    raise
                return max(cursor.rowcount, 0)

        return await asyncio.to_thread(work)

    async def batch(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        if not statements:
            return

        def work() -> None:
            with closing(self._connect()) as conn:
                try:
                    for sql, params in statements:
                        conn.execute(sql, params)
                    conn.commit()
                except sqlite3.IntegrityError as exc:
                    conn.rollback()
                    raise IntegrityConflictError(str(exc)) from exc
                except Exception:
                    conn.rollback()
                    raise

        await asyncio.to_thread(work)


def _d1_rows(response: Any) -> list[Any]:
    rows = js_field(response, "results")
    if rows is None:
        return []
    return list(to_py(rows))


class D1Database(Database):
    """Cloudflare D1 后端（经 request.scope["env"].DB 注入）。"""

    def __init__(self, d1: Any) -> None:
        self._d1 = d1

    @staticmethod
    def _reraise(exc: BaseException) -> None:
        if _is_integrity_conflict(exc):
            raise IntegrityConflictError(str(exc)) from exc
        raise exc

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        try:
            response = await self._d1.prepare(sql).bind(*params).all()
        except Exception as exc:
            self._reraise(exc)
        return [dict(to_py(row)) for row in _d1_rows(response)]

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        try:
            row = await self._d1.prepare(sql).bind(*params).first()
        except Exception as exc:
            self._reraise(exc)
        if row is None:
            return None
        return dict(to_py(row))

    async def run(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        try:
            response = await self._d1.prepare(sql).bind(*params).run()
        except Exception as exc:
            self._reraise(exc)
        meta = js_field(response, "meta") or {}
        return int(js_field(meta, "changes", 0) or 0)

    async def batch(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        if not statements:
            return
        try:
            bound = [self._d1.prepare(sql).bind(*params) for sql, params in statements]
            await self._d1.batch(bound)
        except Exception as exc:
            self._reraise(exc)


_db_var: ContextVar[Database | None] = ContextVar("privlink_db", default=None)
_local_db: SqliteDatabase | None = None


def get_db() -> Database:
    """请求上下文内返回注入的后端（Workers=D1），否则回落本地 SQLite。"""
    db = _db_var.get()
    if db is not None:
        return db
    global _local_db
    if _local_db is None:
        _local_db = SqliteDatabase()
    return _local_db


def bind_db(db: Database) -> Token:
    return _db_var.set(db)


def unbind_db(token: Token) -> None:
    _db_var.reset(token)


# D1 全新库：建表语句自带 sort_order/is_public 列，无需本地旧库的 ALTER 迁移
D1_SCHEMA_STATEMENTS: list[tuple[str, tuple[Any, ...]]] = [
    (
        "CREATE TABLE IF NOT EXISTS app_settings ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);",
        (),
    ),
    (
        "CREATE TABLE IF NOT EXISTS sites ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE, "
        "site_name TEXT, icon_rel_path TEXT, icon_source_url TEXT, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "last_status TEXT NOT NULL, last_error TEXT, "
        "sort_order INTEGER NOT NULL DEFAULT 0, is_public INTEGER NOT NULL DEFAULT 1);",
        (),
    ),
    (
        "CREATE TABLE IF NOT EXISTS tags ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL UNIQUE COLLATE NOCASE, created_at TEXT NOT NULL);",
        (),
    ),
    (
        "CREATE TABLE IF NOT EXISTS site_tags ("
        "site_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, "
        "PRIMARY KEY (site_id, tag_id), "
        "FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE, "
        "FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE);",
        (),
    ),
    ("CREATE INDEX IF NOT EXISTS idx_site_tags_tag ON site_tags(tag_id);", ()),
    (
        "CREATE TABLE IF NOT EXISTS plugin_data ("
        "plugin_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, PRIMARY KEY (plugin_id, key));",
        (),
    ),
]

_remote_schema_ready = False


async def ensure_remote_schema() -> None:
    """Workers/D1：每个 isolate 首个请求执行一次 DDL（lifespan 拿不到 env bindings）。"""
    global _remote_schema_ready
    if _remote_schema_ready:
        return
    await get_db().batch(D1_SCHEMA_STATEMENTS)
    _remote_schema_ready = True


def normalize_tag_name(raw: str) -> str:
    collapsed = " ".join((raw or "").split())
    return collapsed


def normalize_tag_list(raw_tags: list[str] | None) -> list[str]:
    if not raw_tags:
        return []
    seen: dict[str, str] = {}
    for item in raw_tags:
        name = normalize_tag_name(str(item))
        if not name:
            continue
        if len(name) > TAG_NAME_MAX_LEN:
            raise ValueError(f"标签长度不能超过 {TAG_NAME_MAX_LEN} 个字符")
        key = name.lower()
        if key not in seen:
            seen[key] = name
    return list(seen.values())


def init_storage() -> None:
    """本地初始化：目录 + SQLite 建表与旧库迁移（Workers 由 ensure_remote_schema 覆盖）。"""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.ICON_DIR.mkdir(parents=True, exist_ok=True)
    config.BACKGROUND_DIR.mkdir(parents=True, exist_ok=True)
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.commit()

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                site_name TEXT,
                icon_rel_path TEXT,
                icon_source_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_status TEXT NOT NULL,
                last_error TEXT
            );
            """
        )
        conn.commit()

        # 迁移：添加 sort_order 列
        try:
            conn.execute("ALTER TABLE sites ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 列已存在

        # 迁移：添加 is_public 列（1=公开站点，0=仅持 token 可见）
        try:
            conn.execute("ALTER TABLE sites ADD COLUMN is_public INTEGER NOT NULL DEFAULT 1")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 列已存在

        # 初始化已有数据的 sort_order（仅所有值都为 0 时执行）
        nonzero_count = conn.execute("SELECT COUNT(*) FROM sites WHERE sort_order != 0").fetchone()[0]
        if nonzero_count == 0:
            rows = conn.execute(
                "SELECT id FROM sites ORDER BY updated_at DESC, id DESC"
            ).fetchall()
            for idx, row in enumerate(rows, start=1):
                conn.execute("UPDATE sites SET sort_order = ? WHERE id = ?", (idx, row[0]))
            if rows:
                conn.commit()

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS site_tags (
                site_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (site_id, tag_id),
                FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_site_tags_tag ON site_tags(tag_id);"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plugin_data (
                plugin_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (plugin_id, key)
            );
            """
        )
        conn.commit()


async def get_app_setting(key: str) -> str | None:
    row = await get_db().fetch_one(
        "SELECT value FROM app_settings WHERE key = ?;",
        (key,),
    )
    if not row:
        return None
    return str(row["value"] or "")


async def set_app_setting(key: str, value: str) -> None:
    await get_db().run(
        """
        INSERT INTO app_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at;
        """,
        (key, value, utc_now()),
    )


# ===== 插件市场：安装状态（app_settings 单键 JSON）与插件数据（plugin_data 表） =====

PLUGIN_STATE_SETTING_KEY = "plugins_state"


async def get_plugin_state() -> dict[str, dict[str, Any]]:
    """已安装插件状态：{pluginId: {enabled: bool, installedAt: str}}；损坏的 JSON 视为空。"""
    raw = await get_app_setting(PLUGIN_STATE_SETTING_KEY)
    if not raw:
        return {}
    try:
        state = json.loads(raw)
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


async def set_plugin_state(state: dict[str, dict[str, Any]]) -> None:
    await set_app_setting(PLUGIN_STATE_SETTING_KEY, json.dumps(state, ensure_ascii=False))


async def delete_app_setting(key: str) -> None:
    await get_db().run("DELETE FROM app_settings WHERE key = ?;", (key,))


# 浮动窗口布局（宿主所有，插件经桥不可达）：app_settings 键 plugin_windows:<pluginId>
PLUGIN_WINDOWS_SETTING_PREFIX = "plugin_windows:"


async def get_plugin_windows(plugin_id: str) -> list[dict[str, Any]] | None:
    """窗口布局注册表；键不存在（从未有过实例）返回 None，与「用户已全部关闭」(=[]) 区分。"""
    raw = await get_app_setting(PLUGIN_WINDOWS_SETTING_PREFIX + plugin_id)
    if raw is None:
        return None
    try:
        windows = json.loads(raw)
    except ValueError:
        config.logger.warning("插件 %s 的窗口布局数据损坏，按空处理", plugin_id)
        return []
    return windows if isinstance(windows, list) else []


async def set_plugin_windows(plugin_id: str, windows: list[dict[str, Any]]) -> None:
    await set_app_setting(
        PLUGIN_WINDOWS_SETTING_PREFIX + plugin_id,
        json.dumps(windows, ensure_ascii=False),
    )


async def fetch_plugin_data(plugin_id: str, key: str) -> str | None:
    row = await get_db().fetch_one(
        "SELECT value FROM plugin_data WHERE plugin_id = ? AND key = ?;",
        (plugin_id, key),
    )
    if not row:
        return None
    return str(row["value"])


async def upsert_plugin_data(plugin_id: str, key: str, value: str) -> None:
    await get_db().run(
        """
        INSERT INTO plugin_data (plugin_id, key, value, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(plugin_id, key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at;
        """,
        (plugin_id, key, value, utc_now()),
    )


async def delete_plugin_data(plugin_id: str, key: str | None = None, prefix: str | None = None) -> int:
    """删除插件数据。

    key 为 None 且 prefix 为 None 时清空该插件全部数据（卸载 purge）；给定 key 删单键
    （桥 storage.del）；给定 prefix 删实例前缀下全部键（浮动便签关闭单实例，见 docs/plugins.md）。
    prefix 须由路由先行校验字符集（不含 LIKE 通配符）。
    """
    if key is not None:
        return await get_db().run(
            "DELETE FROM plugin_data WHERE plugin_id = ? AND key = ?;",
            (plugin_id, key),
        )
    if prefix is not None:
        return await get_db().run(
            "DELETE FROM plugin_data WHERE plugin_id = ? AND key LIKE ?;",
            (plugin_id, prefix + "%"),
        )
    return await get_db().run("DELETE FROM plugin_data WHERE plugin_id = ?;", (plugin_id,))


async def plugin_data_usage(plugin_id: str) -> dict[str, int]:
    """配额用量：键数与键值字节总量（D1/SQLite 的 LENGTH 对 TEXT 均按字符计，作近似即可）。"""
    row = await get_db().fetch_one(
        """
        SELECT COUNT(*) AS key_count,
               COALESCE(SUM(LENGTH(value)), 0) AS value_chars
        FROM plugin_data WHERE plugin_id = ?;
        """,
        (plugin_id,),
    )
    return {
        "key_count": int((row or {}).get("key_count") or 0),
        "value_chars": int((row or {}).get("value_chars") or 0),
    }


async def upsert_site_record(
    *,
    url: str,
    site_name: str,
    icon_rel_path: str,
    icon_source_url: str,
    status: str,
    error_text: str,
) -> str:
    now = utc_now()
    db = get_db()
    old_row = await db.fetch_one("SELECT icon_rel_path FROM sites WHERE url = ?;", (url,))
    old_icon_path = (old_row["icon_rel_path"] or "").strip() if old_row else ""
    await db.run(
        """
        INSERT INTO sites (
            url,
            site_name,
            icon_rel_path,
            icon_source_url,
            created_at,
            updated_at,
            last_status,
            last_error,
            sort_order
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(url) DO UPDATE SET
            site_name = excluded.site_name,
            icon_rel_path = excluded.icon_rel_path,
            icon_source_url = excluded.icon_source_url,
            updated_at = excluded.updated_at,
            last_status = excluded.last_status,
            last_error = excluded.last_error;
        """,
        (
            url,
            site_name,
            icon_rel_path,
            icon_source_url,
            now,
            now,
            status,
            error_text or None,
        ),
    )
    config.logger.info("数据库写入成功: %s (状态: %s)", url, status)
    return old_icon_path


async def fetch_site_row(site_id: int) -> dict[str, Any] | None:
    return await get_db().fetch_one(
        f"SELECT {SITE_ITEM_COLUMNS} FROM sites WHERE id = ?;",
        (site_id,),
    )


async def fetch_existing_icon(url: str) -> tuple[str, str]:
    row = await get_db().fetch_one(
        "SELECT icon_rel_path, icon_source_url FROM sites WHERE url = ?;",
        (url,),
    )
    if not row:
        return "", ""
    return (row["icon_rel_path"] or "").strip(), (row["icon_source_url"] or "").strip()


async def fetch_site_tags(site_id: int) -> list[str]:
    rows = await get_db().fetch_all(
        """
        SELECT tags.name
        FROM site_tags
        JOIN tags ON tags.id = site_tags.tag_id
        WHERE site_tags.site_id = ?
        ORDER BY tags.name COLLATE NOCASE ASC;
        """,
        (site_id,),
    )
    return [row["name"] for row in rows]


async def fetch_all_site_tags() -> dict[int, list[str]]:
    rows = await get_db().fetch_all(
        """
        SELECT site_tags.site_id, tags.name
        FROM site_tags
        JOIN tags ON tags.id = site_tags.tag_id
        ORDER BY tags.name COLLATE NOCASE ASC;
        """
    )
    result: dict[int, list[str]] = {}
    for row in rows:
        result.setdefault(int(row["site_id"]), []).append(row["name"])
    return result


def site_tags_statements(site_id: int, names: list[str], now: str) -> list[tuple[str, tuple[Any, ...]]]:
    """整体替换站点标签的语句组；须放进同一 batch 执行以保证原子性。"""
    statements: list[tuple[str, tuple[Any, ...]]] = [
        ("DELETE FROM site_tags WHERE site_id = ?", (site_id,)),
    ]
    for name in names:
        statements.append(("INSERT OR IGNORE INTO tags (name, created_at) VALUES (?, ?)", (name, now)))
        # 子查询取 tag id：不依赖上一步的返回值，整组可一次提交
        statements.append((
            "INSERT OR IGNORE INTO site_tags (site_id, tag_id) "
            "SELECT ?, id FROM tags WHERE name = ? COLLATE NOCASE",
            (site_id, name),
        ))
    return statements
