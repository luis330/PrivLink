# AI 接手文档 — Nav Local（网站导航系统）

> 面向接手的 AI/开发者的完整上下文。读这份文档即可了解两个代码体、当前状态、接口契约与下一步。

---

## 1. 项目全景

一个个人网站导航系统。输入 URL → 服务端解析网站名称与 icon → 存入本地 SQLite，前端以网格卡片展示、支持标签筛选、拖拽排序、右键管理。

存在**两个代码体**，关系必须分清：

| | A. 设计工作区（本目录） | B. 真实仓库（`D:\Studyspace\Gitea\Nav_Loacl`） |
|---|---|---|
| 角色 | **新前端视觉稿**（正在做的工作） | **线上真实产品**（后端 + 老前端 + 扩展） |
| 前端文件 | `index.html`（~2995 行单文件） | `index.html`（~91KB，老版视觉） |
| 后端 | **无，纯本地 mock**（localStorage + 模拟 parse） | FastAPI `main.py`（~58KB）+ SQLite |
| 是否上线 | 否，设计预览用 | 是（`nav.freeba.org`，Docker） |

**当前任务：** 把 A 的新视觉/交互设计"回灌"进 B 的真实前端，替换其 `index.html`，并接通真实 API（目前 A 的前端用的是 mock 数据层）。

> **状态（2026-08-13）：回灌已完成。** B 的 `index.html` 已替换为 A 视觉 + 真实 API 数据层；emoji 内置图标已按决策整体切换为后端图标库 `GET /api/icons`；mock 存储键（`nav_sites_v1`/`nav_tags_v1`/`nav_token_v1`）已废弃并在启动时清理，现行键为 `nav_cache_v1:sites`/`nav_cache_v1:tags`/`nav_active_tag`/`nav_add_panel_collapsed`。
>
> **更新（2026-08-13）：已实施单用户 Token 门禁。** `GET/PUT /api/settings/ingest-token` 接口与页面 Token 管理已移除，token 改由部署环境变量 `NAV_TOKEN` 预配置（非空时全部 `/api/*` 需请求头 `X-Nav-Token`，详见 README「访问控制（单用户模式）」章节）。本文档 §3.1 路由表中的 settings 接口及相关 token 描述已过时，以 README 为准。

---

## 2. 代码体 A：设计工作区前端（当前工作对象）

路径：本目录 `index.html`。单文件、无框架、无构建、内联 CSS+JS。

### 2.1 数据层（mock，localStorage）

- `nav_sites_v1` — 站点数组
- `nav_tags_v1` — 标签数组
- `nav_token_v1` — ingest token（仅存本地，**未发任何请求头**）
- `nav_active_tag` — 当前选中标签
- `nav_add_panel_collapsed` — 添加面板折叠态（`ADD_COLLAPSED_KEY`，默认折叠）

站点对象模型（与后端 `SiteItem` 字段一致，见 §3.3）：

```
{ id, url, site_name, icon_rel_path, updated_at, sort_order, tags: [] }
```

### 2.2 内置数据

- `DEFAULT_SITES`（L1404）：16 个内置站点
- `BUILTIN_ICONS`（L1357）：32 个 emoji 图标（`emoji:*` 前缀）
- `seedDemoData()`（L2965）：localStorage 为空时注入 8 个示例站点（仅内存/演示）

### 2.3 关键 JS 函数（全部为本地实现）

- **parse 模拟**：`parseSite(url)`（L1684）—— 假 600ms 延迟，站名由 hostname 推导，icon 走 `https://www.google.com/s2/favicons?domain=...`。**这是与真实后端最大的差异点**。
- **公网 IP 模拟**：`loadPublicIp()` —— 直接 `fetch("https://api.ipify.org?format=json")`（后端真实实现是 `GET /api/network/public-ip`）。
- 渲染：`renderSites()` / `createNavItem()` / `renderTagBar()` / `renderResultManager()` / `renderResult()`
- 排序：`handleReorder(fromIndex, toIndex)` —— 仅 `activeTag === ""` 时可拖拽
- 菜单：`showContextMenu(x, y, siteId)`（右键，防溢出，滚动/缩放隐藏）
- 弹窗：`openEditModal` / `openDeleteModal` / `openTokenModal` / `closeXxxModal`
- 标签：`addDraftTag` / `removeDraftTag` / `renderTagSuggest`（Enter 确认、Backspace 删除、"," 提交、方向键导航）
- 图标：`renderIconGrid` / `filterIcons` / `selectIcon`；上传限 `.ico/.png/.svg` ≤ 1MB（`ICON_UPLOAD_MAX_BYTES = 1024*1024`），转 dataURL
- 工具：`normalizeUrl`/`normalizeInputUrl`/`isHttpUrl`/`normalizeTagName`/`compactUrlLabel`/`cloneSite`/`findSiteByUrl`/`findSiteIndexById`

### 2.4 交互行为清单（新设计已实现）

- 添加面板默认折叠（条状栏），提交后自动展开显示结果
- 结果卡 `renderResult` 浅色磨砂玻璃：status `success | partial | failed | invalid`
- 右键菜单：编辑 / 删除 / （按 URL 去重）
- Esc 全局关闭浮层；点击遮罩关闭
- 拖拽重排仅"全部"标签下可用
- 公网 IP 显示在页脚，点击刷新

### 2.5 设计规范

视觉规范独立成文档：**`nav-design-spec.html`**（本目录，9 章节设计稿）。核心约束：

- 蓝调渐变玻璃拟态：`--bg-a:#0a46b6` → `--bg-b:#1d81db`，径向光斑
- 主按钮 `--button-bg:#0f7cf0` / hover `#0b69cd`；高亮色 `--focus:#7ec0ff`
- **禁止紫色/靛蓝**（`#4f56ea/#6366f1` 等为禁用色）
- 字体：`"IBM Plex Sans","Noto Sans SC","Microsoft YaHei"`；等宽 `"IBM Plex Mono"`
- 中文行高 ≥1.3，不加负字距；UI 文案简体中文
- 语义 token：`--glass-line:#ffffff5e`、`--ink:#12253d`、`--ink-muted:#496178`、`--line:#d7e1ec` 等 16 个（`:root` 字面值，非 var 自引用）
- 弹窗尺寸已收敛：modal-title 19px、modal-label 14.5px、close 24px

---

## 3. 代码体 B：真实后端 API 契约

路径：`D:\Studyspace\Gitea\Nav_Loacl\main.py`（FastAPI，`title="Nav Local Service"` v1.0.0）。存储：SQLite `data/sites.db` + 图标目录 `ICON/`。

### 3.1 路由清单

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/`、`/index.html` | — | 首页（ETag/304 + gzip） |
| GET | `/api/network/public-ip` | — | `{"ip": "..."}`，失败 502 |
| GET | `/api/settings/ingest-token` | — | `{"token","configured"}`（仅同源） |
| PUT | `/api/settings/ingest-token` | `{"token":""}` | 同上（空串=禁用；仅同源） |
| POST | `/api/site/parse` | `{"url": "https://..."}` | `ParseResponse`（见下） |
| GET | `/api/sites` | — | `SiteItem[]` |
| GET | `/api/tags` | — | `TagItem[]` |
| GET | `/api/icons` | — | 图标库列表 |
| POST | `/api/site/ingest` | `BrowserIngestRequest` | `ParseResponse`；需 `X-Nav-Token`（默认禁用） |
| PUT | `/api/sites/reorder` | `{"site_ids": [int, ...]}`（全量 id 按新序，至少 1 个） | `MessageResponse` |
| PUT | `/api/sites/{site_id}` | `SiteUpdateRequest` | `SiteItem` |
| POST | `/api/sites/{site_id}/icon` | 上传 | `SiteItem` |
| DELETE | `/api/sites/{site_id}` | — | `MessageResponse` |

### 3.2 `ParseResponse`（parse 与 ingest 共用）

```
{ url, final_url, site_name, icon_rel_path, icon_source_url,
  status: "success|partial|failed|invalid", error, warning }
```

`site_name` 优先级：`og:site_name > title > 域名`。`url` 为规范化唯一键，冲突时 upsert。

### 3.3 `SiteItem`

```
{ id:int, url, site_name, icon_rel_path, updated_at, sort_order:int, tags:list[str] }
```

与 A 前端的站点对象**字段完全对齐**——回灌时映射成本极低。

### 3.4 ingest / 浏览器采集

- 用于 Cloudflare/登录验证导致服务端抓不到的站点：浏览器里通过验证后由 Tampermonkey 脚本（`collectors/nav-local.user.js`）或 Chrome 扩展（`browser-extension/`，含 background.js/content.js/options）上报。
- 默认禁用；首页"Token 设置"保存后生效，请求头携带 `X-Nav-Token`。
- icon ≤1MB，支持 ico/png/jpg/jpeg/svg/webp/gif/bmp/avif；icon 可空（空则保留旧图）。

### 3.5 安全 / 网络

- SSRF 防护：默认禁本机/内网，`NAV_ALLOWED_PRIVATE_NETWORKS` 白名单（默认放行 `192.168.50.0/24`）
- 代理走标准环境变量 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY`
- `NAV_HOST_ALIASES` 主机解析映射（仅改 TCP 目标 IP，保留 Host/SNI）；`NAV_USER_AGENT`、`NAV_ACCEPT_LANGUAGE` 覆盖抓取头
- Linux 容器自动 uvloop/httptools；Windows 跳过 uvloop

---

## 4. 真实仓库 B 的其他内容

- **AGENTS.md**：issue 在 Gitea（`luis/Nav_Loacl`），走 gitea-issues skill；领域文档单上下文布局（根 `CONTEXT.md` + `docs/adr/`，当前不存在则静默继续）
- **README.md**：部署/代理/缓存策略的权威来源
- **Dockerfile / docker-compose.yml**：私有镜像源 `docker.freeba.org/gfcr.ip/...`，单实例 `workers=1`
- **browser-extension/**：Chrome/Edge 插件（manifest + background/content/options）
- **collectors/nav-local.user.js**：Tampermonkey 采集脚本
- **icons/**：大量中文名 SVG 图标库（`/api/icons` 扫描此目录）
- **tests/**：测试目录

---

## 5. 下一步工作（建议顺序）

目标：**用 A 的新前端替换 B 的 `index.html`，并把 mock 数据层换成真实 API 调用。**

1. **差异核对**：diff B 的 `index.html` 与 A 的 `index.html`，列出 B 有而 A 没有的能力（真实 API 调用、缓存 `nav_cache_v1:*`、token 设置、拖拽持久化等）。
2. **数据层替换（优先级最高）**：A 的 `loadSitesFromStorage/saveSitesToStorage/parseSite/loadPublicIp` 改为调用 B 的真实端点；站点写操作（增删改、排序、图标上传）接入 `POST /api/site/parse` + `PUT/DELETE /api/sites/...`。
3. **保留 A 的视觉**：所有 DOM 结构、样式、交互保持 A 版本不变，只替换数据来源。
4. **对齐 B 的边界**：token 走 `GET/PUT /api/settings/ingest-token`（同源校验）；图标库改用 `GET /api/icons`；parse 结果接入 `renderResult` 四种状态；ingest 触发入口与 token 弹窗打通。
5. **提交到 Gitea**：变更提交到 `luis/Nav_Loacl`（按 AGENTS.md 约定开 issue/PR）。

### 对接时需注意的已知问题

- A 的 `parseSite` 是 mock，且当前 `renderResult` 已支持 partial/failed 但 mock 不产出——接通真实 API 后需验证失败分支。
- `nav_token_v1` 已存但**未接入任何请求头**，需在 ingest/parse 时携带 `X-Nav-Token`（仅 ingest 需要）。
- 两套存储键不互通：A 用 `nav_sites_v1`，B 用 `nav_cache_v1:sites/tags` 缓存。替换后清理旧键，避免冲突。
- `ICON/` 图标用相对路径，跨端口/域名访问时注意 base。

---

## 6. 运行与验证

```bash
# 后端（B 仓库）
uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
# OpenAPI: http://127.0.0.1:8000/docs

# Docker 部署（B）
docker compose up -d --build
```

A 的前端为纯静态文件，浏览器直接打开即可预览（无后端依赖，mock 数据层）。

### 语法自检（本机）

PowerShell 用 Node 检查 A 的 `index.html` 内联 JS（node 路径：`C:\Users\wangl\.workbuddy\binaries\node\versions\22.22.2\node.exe`）。注意 `OD_NODE_BIN` 是 "Open Design.exe"，**不是** Node，不要用它。

---

## 7. 环境备注（Windows / 本工作区）

- 本项目目录：`C:\Users\wangl\AppData\Roaming\Open Design\namespaces\release-stable-win\data\projects\e32bda08-abf0-4e9f-8c66-82b87bc70eb2`
- B 仓库（只读参考）：`D:\Studyspace\Gitea\Nav_Loacl`
- 控制台中文文件名会乱码（编码问题），读仓库列表用 `[System.IO.File]::ReadAllBytes` + UTF8 解码，勿用 `Get-Content -Raw`（ANSI 误读）
- 本目录 `.pen` 文件为加密设计文件，只能通过 Pencil MCP 工具访问，勿用 Read/Grep 读取
