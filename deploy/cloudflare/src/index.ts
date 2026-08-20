/**
 * PrivLink Cloudflare Worker — 完整路由（Phase 4）
 * 包含所有 API 端点，与 main.py 行为对齐。
 */

import { Hono } from "hono";
import { cors } from "hono/cors";
import type { Context } from "hono";
import {
  initStorage,
  getAppSetting,
  setAppSetting,
  fetchSiteTags,
  fetchAllSiteTags,
  utcNow,
} from "./db";
import type { D1Database } from "@cloudflare/workers-types";
import {
  putObject,
  deleteObject,
  getObject,
  listObjects,
  isValidBackgroundFilename,
  backgroundImageUrl,
  iconUploadFilename,
  iconFilenameFromUrl,
} from "./storage";
import { listIcons, iconUrlForSlug } from "./simple-icons";
import {
  fetchHtml,
  fetchIcon,
  parseHtmlForSiteInfo,
  chooseSiteName,
} from "./fetcher";
import type {
  AuthStatusResponse,
  BackgroundImageItem,
  BackgroundSettingRequest,
  BackgroundSettingResponse,
  BrowserIngestRequest,
  ParseRequest,
  ParseResponse,
  ReorderRequest,
  SiteItem,
  SiteUpdateRequest,
  TagItem,
} from "./types";

// ── 常量 ───────────────────────────────────────────────

const PUBLIC_READONLY_API_PATHS = new Set([
  "/api/sites",
  "/api/tags",
  "/api/auth/status",
  "/api/appearance/background",
]);
const BACKGROUND_SETTING_KEY = "background";
const BACKGROUND_FILENAME_RE = /^bg-[0-9a-f]{24}\.(?:jpg|jpeg|png|webp)$/;
const ALLOWED_BG_EXT = new Set([".jpg", ".jpeg", ".png", ".webp"]);
const ALLOWED_ICON_EXT = new Set([".ico", ".png", ".svg"]);
const BACKGROUND_UPLOAD_MAX = 5 * 1024 * 1024;
const ICON_UPLOAD_MAX = 1024 * 1024;

// ── 类型定义 ───────────────────────────────────────────

type Bindings = {
  DB: D1Database;
  ICON_BUCKET: unknown;
  BACKGROUND_BUCKET: unknown;
  NAV_TOKEN: string;
  NAV_MODE: string;
  ASSETS?: unknown;
};

type HonoContext = Context<{ Bindings: Bindings }>;

// ── 工具函数 ───────────────────────────────────────────

function getDb(c: HonoContext): D1Database {
  return c.env.DB;
}

function errorPayload(msg: string): Record<string, string> {
  return {
    url: "",
    final_url: "",
    site_name: "",
    icon_rel_path: "",
    icon_source_url: "",
    status: "failed",
    error: msg,
    warning: "",
  };
}

/**
 * URL scheme 是否为 http/https。
 *
 * 注意：`URL.protocol` 返回带尾随冒号的值（"https:"），与 Python 端
 * `urlsplit().scheme`（"https"）不同。直接拿 protocol 去比对不带冒号的
 * 白名单会恒为 false，导致所有 URL 被判为 invalid。
 */
export function isAllowedScheme(parsed: URL): boolean {
  const scheme = parsed.protocol.replace(/:$/, "").toLowerCase();
  return scheme === "http" || scheme === "https";
}

/**
 * URL 规范化，与 Python 端 `normalize_url()` 对齐：
 * scheme/host 小写、路径尾部斜杠去除（"/" 视为空）、丢弃 fragment、
 * 保留 query 与 userinfo。两端对同一网址必须产出同一字符串，
 * 否则 sites.url 的 UNIQUE 约束会把它当成两条不同记录。
 *
 * 已知残留差异：显式写出的默认端口（如 https://x:443）会被 URL API
 * 归一化掉，Python 端 urlsplit 则保留 ":443"。该输入形式极罕见，
 * 未做字符串级还原。
 */
export function normalizeUrl(parsed: URL): string {
  const scheme = parsed.protocol.replace(/:$/, "").toLowerCase();

  let auth = "";
  if (parsed.username) {
    auth = parsed.password
      ? `${parsed.username}:${parsed.password}`
      : parsed.username;
    auth = `${auth}@`;
  }

  // URL.hostname 已小写，且 IPv6 自带方括号（Python 端需手动补）
  const host = parsed.hostname.toLowerCase();
  const port = parsed.port ? `:${parsed.port}` : "";

  let path = parsed.pathname || "";
  if (path === "/") path = "";
  else if (path) path = path.replace(/\/+$/, "");

  return `${scheme}://${auth}${host}${port}${path}${parsed.search}`;
}

function resolveIdentity(c: HonoContext): "owner" | null {
  const t = c.env.NAV_TOKEN ?? "";
  if (!t) return null;
  const p = (c.req.header("X-Nav-Token") ?? "").trim();
  return p === t ? "owner" : null;
}

function canViewPrivate(c: HonoContext): boolean {
  return resolveIdentity(c) !== null;
}

function toSiteItem(row: Record<string, unknown>, tags: string[]): SiteItem {
  const url = String(row["url"] ?? "").trim();
  const name =
    String(row["site_name"] ?? "").trim() ||
    (url ? new URL(url).hostname : "");
  return {
    id: Number(row["id"] ?? 0),
    url,
    site_name: name,
    icon_rel_path: String(row["icon_rel_path"] ?? "").trim(),
    updated_at: String(row["updated_at"] ?? "").trim(),
    sort_order: Number(row["sort_order"] ?? 0),
    is_public: Boolean(row["is_public"]),
    tags,
  };
}

function bgSettingResponse(
  type: "default" | "color" | "image",
  color = "",
  image = ""
): BackgroundSettingResponse {
  return {
    type,
    color,
    image,
    image_url: type === "image" ? backgroundImageUrl(image) : "",
  };
}

// ── Hono 应用 ──────────────────────────────────────────

const app = new Hono<{ Bindings: Bindings }>();

// CORS 中间件
app.use("*", cors({ origin: "*", allowMethods: ["GET", "POST", "PUT", "DELETE", "OPTIONS"], allowHeaders: ["*"] }));

// D1 初始化中间件
app.use("*", async (c, next) => {
  const db = getDb(c);
  await initStorage(db);
  return next();
});

// 鉴权中间件
app.use("/api/*", async (c, next) => {
  if (c.req.method === "OPTIONS") return next();
  const path = c.req.path;
  const token = c.env.NAV_TOKEN ?? "";
  if (
    token &&
    path.startsWith("/api/") &&
    !PUBLIC_READONLY_API_PATHS.has(path)
  ) {
    if (resolveIdentity(c) === null) {
      return c.json(
        { error: "需要访问 token" },
        401,
        { "Access-Control-Allow-Origin": "*" }
      );
    }
  }
  return next();
});

// ── R2 静态代理 ────────────────────────────────────────

app.get("/ICON/*", async (c) => {
  const obj = await getObject(c.env.ICON_BUCKET, c.req.path.slice(6));
  if (!obj) return c.notFound();
  return new Response(obj.body, {
    status: 200,
    headers: { "Cache-Control": "public, max-age=86400" },
  });
});

app.get("/background/*", async (c) => {
  const obj = await getObject(c.env.BACKGROUND_BUCKET, c.req.path.slice(11));
  if (!obj) return c.notFound();
  return new Response(obj.body, {
    status: 200,
    headers: { "Cache-Control": "public, max-age=86400" },
  });
});

// ── /api/auth/status ───────────────────────────────────

app.get("/api/auth/status", (c) => {
  const t = c.env.NAV_TOKEN ?? "";
  const resp: AuthStatusResponse = {
    token_required: t.length > 0,
    authorized: t.length === 0 || resolveIdentity(c) !== null,
  };
  return new Response(JSON.stringify(resp), {
    status: 200,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
});

// ── /api/sites ─────────────────────────────────────────

app.get("/api/sites", async (c) => {
  const db = getDb(c);
  const showPrivate = canViewPrivate(c);
  const rows = await (db as any)
    .prepare(
      `SELECT id, url, site_name, icon_rel_path, updated_at, sort_order, is_public
       FROM sites ${showPrivate ? "" : "WHERE is_public = 1"}
       ORDER BY sort_order ASC, id ASC`
    )
    .all();
  const tagsBySite = await fetchAllSiteTags(db);
  return c.json(
    rows.results.map((r: any) => toSiteItem(r, tagsBySite.get(Number(r.id)) ?? []))
  );
});

// ── /api/tags ──────────────────────────────────────────

app.get("/api/tags", async (c) => {
  const db = getDb(c);
  const showPrivate = canViewPrivate(c);
  const sql = showPrivate
    ? `SELECT tags.name, COUNT(site_tags.site_id) AS usage_count
       FROM tags LEFT JOIN site_tags ON site_tags.tag_id = tags.id
       GROUP BY tags.id ORDER BY tags.name COLLATE NOCASE ASC`
    : `SELECT tags.name, COUNT(sites.id) AS usage_count
       FROM tags LEFT JOIN site_tags ON site_tags.tag_id = tags.id
       LEFT JOIN sites ON sites.id = site_tags.site_id AND sites.is_public = 1
       GROUP BY tags.id HAVING COUNT(sites.id) > 0
       ORDER BY tags.name COLLATE NOCASE ASC`;
  const rows = await (db as any).prepare(sql).all();
  return c.json(
    rows.results.map((r: any) => ({ name: r.name, count: r.usage_count }))
  );
});

// ── /api/appearance/background ────────────────────────

app.get("/api/appearance/background", async (c) => {
  const db = getDb(c);
  const raw = await getAppSetting(db, BACKGROUND_SETTING_KEY);
  if (!raw) return c.json(bgSettingResponse("default"));
  try {
    const d = JSON.parse(raw) as Record<string, unknown>;
    const t = String(d.type ?? "").toLowerCase();
    if (t === "default") return c.json(bgSettingResponse("default"));
    if (t === "color") {
      const color = String(d.color ?? "").trim();
      if (!/^#[0-9a-fA-F]{6}$/.test(color)) throw new Error();
      return c.json(bgSettingResponse("color", color));
    }
    if (t === "image") {
      const image = String(d.image ?? "").trim();
      if (!BACKGROUND_FILENAME_RE.test(image)) throw new Error();
      return c.json(bgSettingResponse("image", "", image));
    }
    throw new Error();
  } catch {
    return c.json(bgSettingResponse("default"));
  }
});

app.put("/api/appearance/background", async (c) => {
  const db = getDb(c);
  const p = await c.req.json() as BackgroundSettingRequest;
  const t = String(p.type ?? "").toLowerCase();
  if (t === "default") {
    await setAppSetting(db, BACKGROUND_SETTING_KEY, JSON.stringify({ type: "default" }));
    return c.json(bgSettingResponse("default"));
  }
  if (t === "color") {
    const color = (p.color ?? "").trim();
    if (!/^#[0-9a-fA-F]{6}$/.test(color))
      return c.json({ error: "纯色背景需要形如 #RRGGBB 的颜色值" }, 400);
    await setAppSetting(db, BACKGROUND_SETTING_KEY, JSON.stringify({ type: "color", color }));
    return c.json(bgSettingResponse("color", color));
  }
  if (t === "image") {
    const image = (p.image ?? "").trim();
    if (!BACKGROUND_FILENAME_RE.test(image))
      return c.json({ error: "背景图文件名不合法" }, 400);
    await setAppSetting(db, BACKGROUND_SETTING_KEY, JSON.stringify({ type: "image", image }));
    return c.json(bgSettingResponse("image", "", image));
  }
  return c.json({ error: "背景类型必须是 default、color 或 image" }, 400);
});

// ── /api/appearance/background/images ──────────────────

app.get("/api/appearance/background/images", async (c) => {
  const items = await listObjects(c.env.BACKGROUND_BUCKET, "background/");
  return c.json(
    items.map((o: any) => ({
      file: o.key,
      size: o.size,
      url: backgroundImageUrl(o.key),
    }))
  );
});

app.post("/api/appearance/background/images", async (c) => {
  const form = await c.req.formData();
  const file = form.get("image") as File | null;
  if (!file?.name)
    return c.json({ error: "请上传背景图片文件" }, 400);
  const ext = "." + file.name.split(".").pop()?.toLowerCase();
  if (!ALLOWED_BG_EXT.has(ext))
    return c.json({ error: "仅支持 jpg、png、webp 格式图片" }, 400);
  const bytes = await file.arrayBuffer();
  if (bytes.byteLength === 0)
    return c.json({ error: "图片内容为空" }, 400);
  if (bytes.byteLength > BACKGROUND_UPLOAD_MAX)
    return c.json({ error: "图片大小不能超过 5MB" }, 400);
  const hashBuf = await crypto.subtle.digest("SHA-256", bytes);
  const hex = Array.from(new Uint8Array(hashBuf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  const filename = `bg-${hex.slice(0, 24)}${ext}`;
  await putObject(c.env.BACKGROUND_BUCKET, `background/${filename}`, bytes);
  await setAppSetting(
    getDb(c),
    BACKGROUND_SETTING_KEY,
    JSON.stringify({ type: "image", image: filename })
  );
  return c.json(bgSettingResponse("image", "", filename));
});

app.delete("/api/appearance/background/images/:file", async (c) => {
  const db = getDb(c);
  const filename = c.req.param("file");
  if (!isValidBackgroundFilename(filename))
    return c.json({ error: "文件名不合法" }, 400);
  await deleteObject(c.env.BACKGROUND_BUCKET, `background/${filename}`);
  const raw = await getAppSetting(db, BACKGROUND_SETTING_KEY);
  if (raw) {
    const d = JSON.parse(raw) as Record<string, unknown>;
    if (String(d.image ?? "").trim() === filename) {
      await setAppSetting(db, BACKGROUND_SETTING_KEY, JSON.stringify({ type: "default" }));
      return c.json(bgSettingResponse("default"));
    }
  }
  return c.json(bgSettingResponse("default"));
});

// ── /api/icons ─────────────────────────────────────────

app.get("/api/icons", (c) => {
  const q = c.req.query("q") ?? "";
  return c.json(listIcons(q));
});

// ── POST /api/site/parse ───────────────────────────────

app.post("/api/site/parse", async (c) => {
  const db = getDb(c);
  const raw = await c.req.json() as ParseRequest;
  const url = (raw.url ?? "").trim();
  if (!url)
    return c.json(
      { ...errorPayload("Field 'url' is required"), status: "invalid" },
      400
    );

  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return c.json(
      { ...errorPayload("Invalid URL"), status: "invalid" },
      400
    );
  }
  if (!isAllowedScheme(parsed))
    return c.json(
      { ...errorPayload("URL scheme must be http or https"), status: "invalid" },
      400
    );

  const finalUrlStr = normalizeUrl(parsed);
  let htmlResult: Awaited<ReturnType<typeof fetchHtml>> | null = null;
  let htmlError = "";

  try {
    htmlResult = await fetchHtml(finalUrlStr, 2 * 1024 * 1024);
  } catch (e: any) {
    htmlError = e.message;
  }

  const info = htmlResult
    ? parseHtmlForSiteInfo(htmlResult.body, finalUrlStr)
    : null;
  const siteName = chooseSiteName(
    info ?? { ogSiteName: "", title: "", iconLinks: [] },
    finalUrlStr
  );

  // 下载图标
  let iconRelPath = "", iconSourceUrl = "";
  if (info?.iconLinks && info.iconLinks.length > 0) {
    for (const ic of info.iconLinks) {
      try {
        const iconResult = await fetchIcon(ic.url, 2 * 1024 * 1024, finalUrlStr);
        const ext = ic.url.split(".").pop()?.toLowerCase() ?? "ico";
        const iconExt = ["ico", "png", "svg", "webp", "gif", "bmp", "avif"].includes(ext)
          ? `.${ext}`
          : ".ico";
        const filename = await iconFilenameFromUrl(finalUrlStr, iconExt);
        await putObject(c.env.ICON_BUCKET, filename, iconResult.body);
        iconRelPath = filename;
        iconSourceUrl = iconResult.finalUrl;
        break;
      } catch {
        /* try next candidate */
      }
    }
  }

  const hasName = !!siteName;
  const hasIcon = !!iconRelPath;
  const status: ParseResponse["status"] =
    hasName && hasIcon
      ? "success"
      : hasName || hasIcon
      ? "partial"
      : "failed";
  const now = utcNow();

  // upsert
  await (db as any)
    .prepare(`
      INSERT INTO sites (url, site_name, icon_rel_path, icon_source_url, created_at, updated_at, last_status, last_error, sort_order)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
      ON CONFLICT(url) DO UPDATE SET
        site_name = excluded.site_name,
        icon_rel_path = excluded.icon_rel_path,
        icon_source_url = excluded.icon_source_url,
        updated_at = excluded.updated_at,
        last_status = excluded.last_status,
        last_error = excluded.last_error
    `)
    .bind(
      finalUrlStr,
      siteName,
      iconRelPath,
      iconSourceUrl,
      now,
      now,
      status,
      htmlError || null
    )
    .run();

  // 清理旧图标
  const oldRow = await (db as any)
    .prepare("SELECT icon_rel_path FROM sites WHERE url = ?")
    .bind(finalUrlStr)
    .first();
  const oldIcon = oldRow
    ? String((oldRow as any).icon_rel_path ?? "").trim()
    : "";
  if (oldIcon && oldIcon !== iconRelPath && !oldIcon.startsWith("https://")) {
    await deleteObject(c.env.ICON_BUCKET, oldIcon);
  }

  const resp: ParseResponse = {
    url: finalUrlStr,
    final_url: finalUrlStr,
    site_name: siteName,
    icon_rel_path: iconRelPath,
    icon_source_url: iconSourceUrl,
    status,
    error: status === "success" ? "" : htmlError || "no data",
    warning: status === "success" && htmlError ? htmlError : "",
  };
  return c.json(resp, (status as string) === "invalid" ? 400 : 200);
});

// ── POST /api/site/ingest ──────────────────────────────

app.post("/api/site/ingest", async (c) => {
  const token = c.env.NAV_TOKEN ?? "";
  if (!token)
    return c.json(
      { ...errorPayload("浏览器采集接口未启用"), status: "failed" },
      403
    );
  if (resolveIdentity(c) === null)
    return c.json(
      { ...errorPayload("浏览器采集 token 无效"), status: "failed" },
      401
    );

  const db = getDb(c);
  const payload = await c.req.json() as BrowserIngestRequest;
  const url = (payload.url ?? "").trim();
  if (!url)
    return c.json(
      { ...errorPayload("Field 'url' is required"), status: "invalid" },
      400
    );

  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return c.json(
      { ...errorPayload("Invalid URL"), status: "invalid" },
      400
    );
  }
  // 与 Python 端 validate_remote_url 对齐：只接受 http/https
  if (!isAllowedScheme(parsed))
    return c.json(
      { ...errorPayload("URL scheme must be http or https"), status: "invalid" },
      400
    );
  const finalUrlStr = normalizeUrl(parsed);
  const siteName =
    ((payload.site_name ?? "").trim()) || parsed.hostname || "";

  // 与 Python 端一致：final_url 同样要经过 scheme 校验与规范化
  let finalUrl = finalUrlStr;
  const rawFinalUrl = (payload.final_url ?? "").trim();
  if (rawFinalUrl) {
    let finalParsed: URL;
    try {
      finalParsed = new URL(rawFinalUrl);
    } catch {
      return c.json(
        { ...errorPayload("Invalid URL"), status: "invalid" },
        400
      );
    }
    if (!isAllowedScheme(finalParsed))
      return c.json(
        { ...errorPayload("URL scheme must be http or https"), status: "invalid" },
        400
      );
    finalUrl = normalizeUrl(finalParsed);
  }

  // 处理图标
  let iconRelPath = "", iconSourceUrl = "";
  if (payload.icon?.data_base64?.trim()) {
    try {
      const raw = payload.icon.data_base64.trim().startsWith("data:")
        ? payload.icon.data_base64.split(",")[1] ?? ""
        : payload.icon.data_base64;
      const bytes = Uint8Array.from(atob(raw), (c) => c.charCodeAt(0));
      if (bytes.length > ICON_UPLOAD_MAX)
        throw new Error("Icon size exceeds 1MB");
      const ext = payload.icon.filename?.split(".").pop()?.toLowerCase() || "ico";
      const iconExt = ALLOWED_ICON_EXT.has(`.${ext}`) ? `.${ext}` : ".ico";
      const filename = await iconUploadFilename(bytes, payload.icon.filename ?? "");
      await putObject(c.env.ICON_BUCKET, filename, bytes);
      iconRelPath = filename;
      iconSourceUrl =
        payload.icon.source_url?.trim() || `browser-upload://${filename}`;
    } catch (e: any) {
      iconRelPath = "";
      iconSourceUrl = "";
    }
  } else {
    const existing = await (db as any)
      .prepare(
        "SELECT icon_rel_path, icon_source_url FROM sites WHERE url = ?"
      )
      .bind(finalUrlStr)
      .first();
    if (existing) {
      iconRelPath = String((existing as any).icon_rel_path ?? "").trim();
      iconSourceUrl = String((existing as any).icon_source_url ?? "").trim();
    }
  }

  const hasName = !!siteName;
  const hasIcon = !!iconRelPath;
  const status: ParseResponse["status"] =
    hasName && hasIcon
      ? "success"
      : hasName || hasIcon
      ? "partial"
      : "failed";
  const now = utcNow();

  await (db as any)
    .prepare(`
      INSERT INTO sites (url, site_name, icon_rel_path, icon_source_url, created_at, updated_at, last_status, last_error, sort_order)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
      ON CONFLICT(url) DO UPDATE SET
        site_name = excluded.site_name,
        icon_rel_path = excluded.icon_rel_path,
        icon_source_url = excluded.icon_source_url,
        updated_at = excluded.updated_at,
        last_status = excluded.last_status,
        last_error = excluded.last_error
    `)
    .bind(finalUrlStr, siteName, iconRelPath, iconSourceUrl, now, now, status, null)
    .run();

  return c.json(
    {
      url: finalUrlStr,
      final_url: finalUrl,
      site_name: siteName,
      icon_rel_path: iconRelPath,
      icon_source_url: iconSourceUrl,
      status,
      error: status === "success" ? "" : "partial",
      warning: "",
    },
    ((status as string) === "invalid" ? 400 : 200) as 200 | 400
  );
});

// ── PUT /api/sites/reorder ─────────────────────────────

app.put("/api/sites/reorder", async (c) => {
  const db = getDb(c);
  const payload = await c.req.json() as ReorderRequest;
  const ids = payload.site_ids;
  if (!ids?.length) return c.json({ message: "ok" });
  const placeholders = ids.map(() => "?").join(",");
  const existing = await (db as any)
    .prepare(`SELECT id FROM sites WHERE id IN (${placeholders})`)
    .bind(...ids)
    .all();
  const existingIds = new Set(
    ((existing.results ?? []) as any[]).map((r: any) => r.id)
  );
  for (const sid of ids) {
    if (!existingIds.has(sid))
      return c.json({ error: `网站 ID ${sid} 不存在` }, 400);
  }
  for (let i = 0; i < ids.length; i++) {
    await (db as any)
      .prepare("UPDATE sites SET sort_order = ? WHERE id = ?")
      .bind(i + 1, ids[i])
      .run();
  }
  return c.json({ message: "ok" });
});

// ── PUT /api/sites/{id} ────────────────────────────────

app.put("/api/sites/:id", async (c) => {
  const db = getDb(c);
  const siteId = Number(c.req.param("id"));
  const payload = await c.req.json() as SiteUpdateRequest;
  const name = (payload.site_name ?? "").trim();
  const rawUrl = (payload.url ?? "").trim();
  if (!name || !rawUrl)
    return c.json({ error: "名称和网址不能为空" }, 400);

  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return c.json({ error: "Invalid URL" }, 400);
  }
  const newUrl = parsed.toString();

  // 图标处理
  let newIconPath = "";
  let iconSourceUrl = "";
  if (payload.icon_file) {
    const slug = payload.icon_file.trim();
    if (slug) {
      newIconPath = iconUrlForSlug(slug);
      iconSourceUrl = newIconPath;
    }
  }

  const now = utcNow();
  const row = await (db as any)
    .prepare("SELECT id, icon_rel_path FROM sites WHERE id = ?")
    .bind(siteId)
    .first();
  if (!row) return c.json({ error: "网站不存在" }, 404);
  const oldIcon = String((row as any).icon_rel_path ?? "").trim();

  await (db as any)
    .prepare(
      newIconPath
        ? `UPDATE sites SET url = ?, site_name = ?, icon_rel_path = ?, icon_source_url = ?, updated_at = ? WHERE id = ?`
        : `UPDATE sites SET url = ?, site_name = ?, updated_at = ? WHERE id = ?`
    )
    .bind(
      newUrl,
      name,
      newIconPath,
      newIconPath ? iconSourceUrl : null,
      now,
      siteId,
      newUrl,
      name,
      now,
      siteId
    )
    .run();

  // 标签
  if (payload.tags !== undefined) {
    await (db as any)
      .prepare("DELETE FROM site_tags WHERE site_id = ?")
      .bind(siteId)
      .run();
    if (payload.tags?.length) {
      for (const tag of payload.tags) {
        await (db as any)
          .prepare(
            "INSERT OR IGNORE INTO tags (name, created_at) VALUES (?, ?)"
          )
          .bind(tag.trim(), now)
          .run();
      }
      const tagRows = await (db as any)
        .prepare(
          `SELECT id FROM tags WHERE name IN (${payload.tags.map(() => "?").join(",")}) COLLATE NOCASE`
        )
        .bind(...payload.tags.map((t) => t.trim()))
        .all();
      const tagIds = ((tagRows.results ?? []) as any[]).map(
        (r: any) => r.id
      );
      for (const tid of tagIds) {
        await (db as any)
          .prepare(
            "INSERT OR IGNORE INTO site_tags (site_id, tag_id) VALUES (?, ?)"
          )
          .bind(siteId, tid)
          .run();
      }
    }
  }

  // is_public
  if (payload.is_public !== undefined) {
    await (db as any)
      .prepare("UPDATE sites SET is_public = ? WHERE id = ?")
      .bind(payload.is_public ? 1 : 0, siteId)
      .run();
  }

  // 清理旧图标
  if (oldIcon && oldIcon !== newIconPath && !oldIcon.startsWith("https://")) {
    await deleteObject(c.env.ICON_BUCKET, oldIcon);
  }

  const updated = await (db as any)
    .prepare(
      "SELECT id, url, site_name, icon_rel_path, updated_at, sort_order, is_public FROM sites WHERE id = ?"
    )
    .bind(siteId)
    .first();
  if (!updated) return c.json({ error: "网站不存在" }, 404);
  const tags = await fetchSiteTags(db, siteId);
  return c.json(toSiteItem(updated as any, tags));
});

// ── POST /api/sites/{id}/icon ──────────────────────────

app.post("/api/sites/:id/icon", async (c) => {
  const db = getDb(c);
  const siteId = Number(c.req.param("id"));
  const form = await c.req.formData();
  const file = form.get("icon") as File | null;
  if (!file?.name) return c.json({ error: "请上传图标文件" }, 400);
  const ext = "." + file.name.split(".").pop()?.toLowerCase();
  if (!ALLOWED_ICON_EXT.has(ext))
    return c.json({ error: "仅支持 ico、png、svg 格式图标" }, 400);
  const bytes = await file.arrayBuffer();
  if (bytes.byteLength === 0)
    return c.json({ error: "图标内容为空" }, 400);
  if (bytes.byteLength > ICON_UPLOAD_MAX)
    return c.json({ error: "图标大小不能超过 1MB" }, 400);

  const filename = await iconUploadFilename(new Uint8Array(bytes), file.name);
  await putObject(c.env.ICON_BUCKET, filename, bytes);

  const now = utcNow();
  const row = await (db as any)
    .prepare("SELECT id, icon_rel_path FROM sites WHERE id = ?")
    .bind(siteId)
    .first();
  if (!row) return c.json({ error: "网站不存在" }, 404);
  const oldIcon = String((row as any).icon_rel_path ?? "").trim();
  await (db as any)
    .prepare(
      "UPDATE sites SET icon_rel_path = ?, icon_source_url = ?, updated_at = ? WHERE id = ?"
    )
    .bind(filename, `upload://${filename}`, now, siteId)
    .run();

  if (oldIcon && oldIcon !== filename) {
    await deleteObject(c.env.ICON_BUCKET, oldIcon);
  }

  const updated = await (db as any)
    .prepare(
      "SELECT id, url, site_name, icon_rel_path, updated_at, sort_order, is_public FROM sites WHERE id = ?"
    )
    .bind(siteId)
    .first();
  const tags = await fetchSiteTags(db, siteId);
  return c.json(toSiteItem(updated as any, tags));
});

// ── DELETE /api/sites/{id} ─────────────────────────────

app.delete("/api/sites/:id", async (c) => {
  const db = getDb(c);
  const siteId = Number(c.req.param("id"));
  const row = await (db as any)
    .prepare("SELECT icon_rel_path FROM sites WHERE id = ?")
    .bind(siteId)
    .first();
  if (!row) return c.json({ error: "网站不存在" }, 404);
  const iconPath = String((row as any).icon_rel_path ?? "").trim();
  await (db as any)
    .prepare("DELETE FROM sites WHERE id = ?")
    .bind(siteId)
    .run();
  if (iconPath && !iconPath.startsWith("https://")) {
    await deleteObject(c.env.ICON_BUCKET, iconPath);
  }
  return c.json({ message: "ok" });
});

// ── 错误处理 ───────────────────────────────────────────

app.onError((err, c) => {
  console.error("[PrivLink] 未捕获异常:", err);
  return c.json(
    errorPayload(`Internal server error: ${err.message}`),
    500
  );
});

// ── 导出 ───────────────────────────────────────────────

export default app;
