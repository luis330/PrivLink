# PrivLink Cloudflare 部署需求说明与开发计划

本文档记录 PrivLink 在现有 Docker / 源码部署之外，新增 **Cloudflare 一键部署**能力的需求分析与开发计划。面向开发者的完整部署文档见 [docs/DEPLOYMENT.md](DEPLOYMENT.md)，产品功能概览见根目录 [README](../README.md)。

## 目录

1. [背景与目标](#1-背景与目标)
2. [需求说明](#2-需求说明)
3. [总体技术方案](#3-总体技术方案)
4. [Cloudflare 服务映射](#4-cloudflare-服务映射)
5. [与现有 FastAPI 架构的兼容性审查](#5-与现有-fastapi-架构的兼容性审查)
6. [前端（index.html）改动清单](#6-前端indexhtml改动清单)
7. [图标库方案（方案 B：Simple Icons CDN）](#7-图标库方案方案-bsimple-icons-cdn)
8. [开发计划（分阶段）](#8-开发计划分阶段)
9. [双栈一致性保障机制](#9-双栈一致性保障机制)
10. [风险评估](#10-风险评估)
11. [部署与运维](#11-部署与运维)

---

## 1. 背景与目标

### 1.1 现状

- PrivLink 当前支持两种部署方式：**Docker 部署**（`docker compose up -d --build`）与**源码 clone 本地部署**（`uv run uvicorn ...`）。
- 后端为单文件 **FastAPI + SQLite**（`main.py`，约 2000 行），前端为无构建步骤的单文件 `index.html`。
- 依赖本地文件系统存储图标（`ICON/`）、背景图（`background/`）与数据库（`data/sites.db`）。

### 1.2 目标

在保持现有 Docker / 源码部署完全兼容的前提下，新增第三条部署路径：**一键部署到 Cloudflare**。

- 使用 Cloudflare 免费服务：**Workers**（运行时）、**D1**（SQL 数据库）、**R2**（对象存储）、**Workers Assets**（静态资源托管）。
- 架构上允许更换实现语言：FastAPI 无法在 Cloudflare Workers 运行，评估后采用 **TypeScript + Hono** 新增独立分支，Python 分支保持不变。
- **前端路径保持不变**（`/`、`/api/*`、`/ICON/*`、`/background/*`），通过 Worker 路由分发，浏览器采集器等既有客户端无需感知部署差异。

### 1.3 明确不做的事

- 不迁移、不废弃现有 Python / Docker / 源码部署。
- 不在 Python 端引入任何 Cloudflare 依赖。
- 不支持代理抓取与 SSRF socket 防护（两者仅在本地网络中需要；Cloudflare 边缘网络天然不可用，见 [5.5](#55-不移植的能力及其理由)）。

---

## 2. 需求说明

### 2.1 功能需求

| 编号 | 需求 | 说明 |
|---|---|---|
| FR-1 | 一键部署 | 执行单一命令即可将服务部署到 Cloudflare（`npx wrangler deploy`）。 |
| FR-2 | 零成本 | 全部使用 Cloudflare 免费层服务，不引入付费项。 |
| FR-3 | 功能等价 | TS 分支实现与 Python 分支相同的全部 API 端点与前端行为。 |
| FR-4 | 前端零迁移 | `index.html` 与浏览器采集器无需感知部署差异，路径不变。 |
| FR-5 | 数据持久化 | 站点、标签、背景设置存 D1；图标、背景图存 R2；服务重启 / 重新部署不丢数据。 |
| FR-6 | 现有部署不受影响 | Python 分支（`main.py`）、Docker 配置、pytest 测试零改动。 |
| FR-7 | 图标库可用 | 删除本地 2657 个 SVG 图标库，改用 Simple Icons CDN 方案（方案 B），前端仍可选择图标。 |

### 2.2 非功能需求

| 编号 | 需求 | 说明 |
|---|---|---|
| NFR-1 | 免费额度覆盖 | Workers 10万请求/天、D1 500万读/10万写/月、R2 10GB 存储，个人使用绰绰有余。 |
| NFR-2 | 行为对齐 | 两端点对同一请求返回相同结构、相同状态码。 |
| NFR-3 | 缓存策略对齐 | 首页 ETag/304、图标与背景图的 `Cache-Control` 与 Python 端一致。 |
| NFR-4 | 安全对齐 | `NAV_TOKEN` 门禁、`X-Nav-Token` 鉴权、`X-Nav-Token` 校验逻辑与 Python 端一致。 |

---

## 3. 总体技术方案

### 3.1 双栈架构

保留 Python/FastAPI 作为主要实现（Docker/源码部署继续），新增 TypeScript/Hono + D1 + R2 分支作为独立目录 `deploy/cloudflare/`。

```
PrivLink/
├── main.py                          # 现有 FastAPI（零改动）
├── pyproject.toml                   # 现有依赖（零改动）
├── index.html                       # 前端（微调，见第 6 节）
├── tests/                           # 现有 pytest（零改动）
├── docker-compose.yml / Dockerfile  # 现有容器部署（零改动）
├── collectors/ / browser-extension/ # 采集器（零改动）
└── deploy/
    └── cloudflare/                  # 新增 TS 分支（独立）
        ├── package.json
        ├── tsconfig.json
        ├── wrangler.toml
        ├── src/
        │   ├── index.ts            # Worker 入口 + Hono 路由
        │   ├── types.ts            # 全部 TS 类型定义
        │   ├── db.ts               # D1 连接、迁移、查询封装
        │   ├── storage.ts          # R2 存储抽象（替换 Path IO）
        │   ├── simple-icons.ts     # Simple Icons 静态数据 + 搜索
        │   ├── fetcher.ts          # 远程 URL 抓取（无代理版）
        │   ├── site.ts             # 站点处理核心逻辑
        │   └── icons.ts            # 图标选择器逻辑（Simple Icons 源）
        ├── assets/
        │   └── simple-icons.json   # 构建时生成的静态数据
        ├── migrations/
        │   └── 001_init.sql        # D1 初始化 SQL
        └── tests/
            └── api.spec.ts         # 端点对齐测试
```

### 3.2 分层架构

```
┌─────────────────────────────────────────┐
│  前端 index.html（不变）                 │
│  浏览器采集器（不变）                     │
├─────────────────────────────────────────┤
│  API 契约层（单一事实来源）               │
│  端点路径 / 请求响应结构 / 状态码         │
├──────────────────┬──────────────────────┤
│  Python 实现     │  TypeScript 实现      │
│  main.py         │  src/index.ts        │
│  DB: SQLite      │  DB: D1              │
│  FS: Path        │  FS: R2              │
│  图标库: 本地     │  图标库: Simple Icons │
└──────────────────┴──────────────────────┘
```

三层职责：
- **前端**：两端共用同一份 `index.html`，无感知。
- **API 契约**：两实现必须满足同一端点集合、请求/响应结构与状态码。
- **基础设施**：存储介质差异完全隔离在各实现内部，不对外暴露。

---

## 4. Cloudflare 服务映射

| 现有能力 | FastAPI（本地/Docker） | Cloudflare（TS 分支） | 说明 |
|---|---|---|---|
| Web 框架 | FastAPI + uvicorn | **Hono**（Workers 原生） | FastAPI 无法在 Workers 运行 |
| 数据库 | SQLite `data/sites.db` | **D1** | SQL 语法与 SQLite 完全兼容，建表语句直接移植 |
| 站点图标存储 | `ICON/` 本地目录 | **R2** bucket | 运行时动态写入 |
| 背景图存储 | `background/` 本地目录 | **R2** bucket | 运行时动态写入 |
| 前端页面 | `index.html` 文件 | **Workers Assets** | 部署时打包，只读 |
| 图标库 | `icons/` 本地 2657 个 SVG | **Simple Icons CDN**（外链） | 删除本地图标库（见第 7 节） |
| 远程抓取 | httpx + 代理 | 原生 `fetch()` | 无代理模式（见 [5.5](#55-不移植的能力及其理由)） |
| gzip / ETag | 中间件 | Hono 手动实现 | 行为对齐 |
| uvloop / httptools | `uvicorn[standard]` | 不需要 | Workers 运行时自带 |

### 4.1 R2 与 Workers Assets 的职责边界

两者**不可互相替代**，按"是否运行时写入"划分：

| 内容 | 性质 | 存储位置 |
|---|---|---|
| `index.html` | 静态，部署时固定 | Workers Assets |
| `simple-icons.json` | 静态，构建时固定 | 打包进 Worker bundle |
| `ICON/<hash>`（站点图标） | 运行时上传 / 抓取 | R2 |
| `background/<hash>`（背景图） | 运行时上传 | R2 |

> Workers Assets 不能运行时写入文件，因此动态内容必须使用 R2。

---

## 5. 与现有 FastAPI 架构的兼容性审查

### 5.1 数据库 schema — 完全兼容

D1 SQL 与 SQLite 一致，直接移植 `main.py` 的 `init_storage()` 建表语句。表结构、字段、约束完全不变：

```sql
-- sites / tags / site_tags / app_settings
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
```

### 5.2 `icon_rel_path` 语义变化（最关键的兼容点）

| 场景 | Python 当前值 | TS 新值 | 前端兼容性 |
|---|---|---|---|
| 服务端抓取 favicon | `ICON/9a5b632f...png` | `/ICON/9a5b632f...png`（R2 key） | ✅ `toIconUrl()` 直接可用 |
| 浏览器上传图标 | `ICON/upload-xxx.png` | `/ICON/upload-xxx.png`（R2 key） | ✅ 同上 |
| 从图标库选择 | `ICON/<sha>.svg`（拷贝本地） | `https://cdn.simpleicons.org/github`（纯 URL） | ✅ `toIconUrl()` 识别 `http` 开头直接返回 |

- 前端 `toIconUrl()` 已能处理以 `http` 开头的值（返回原值），因此**无需修改**图标展示逻辑。
- 仅需修改图标**选择器**相关逻辑（见第 6 节）。
- 当前数据库无 `icon-lib://` 存量数据（10 个站点全部为抓取图标），**无迁移成本**。

### 5.3 API 响应格式对齐

| 端点 | Python 响应 | TS 响应 | 差异 |
|---|---|---|---|
| `GET /api/sites` | `[{id, url, site_name, icon_rel_path, updated_at, sort_order, is_public, tags}]` | 一致 | ✅ |
| `GET /api/tags` | `[{name, count}]` | 一致 | ✅ |
| `POST /api/site/parse` | `{url, final_url, site_name, icon_rel_path, icon_source_url, status, error, warning}` | 一致 | ✅ |
| `POST /api/site/ingest` | 同上 | 一致 | ✅ |
| `GET /api/appearance/background` | `{type, color, image, image_url}` | 一致 | ✅ |
| `GET /api/icons` | `[{name, keyword, file}]` | `[{name, slug, url}]` | ⚠️ 前端适配（见 6.2） |
| 其余端点 | — | 一致 | ✅ |

### 5.4 鉴权与安全行为对齐

- `NAV_TOKEN`：空 = 开放模式；非空 = 门禁模式。从 Workers 环境变量（secrets）读取。
- `PUBLIC_READONLY_API_PATHS`：`/api/sites`、`/api/tags`、`/api/auth/status`、`/api/appearance/background` 匿名可读，其余 `/api/*` 需要 `X-Nav-Token`。
- 浏览器采集接口（`/api/site/ingest`）：仅在配置 `NAV_TOKEN` 后可用，否则返回 403。

### 5.5 不移植的能力及其理由

| 能力 | 理由 |
|---|---|
| HTTP / SOCKS5 代理抓取 | Cloudflare Workers 运行在边缘网络，无法配置上游代理；代理仅本地网络需要。 |
| SSRF 防护（socket.getaddrinfo 钩子、内网网段白名单） | Workers 的 `fetch()` 天然受限，无法访问内网；SSRF 风险面由 Cloudflare 平台隔离。 |
| `NAV_HOST_ALIASES`（hosts 解析映射） | 仅内网直连场景需要，Workers 上不可用。 |
| `NAV_ALLOWED_PRIVATE_NETWORKS` | 同上，Workers 无法访问内网。 |
| `GET /api/network/public-ip` | 保留，但语义从"服务端公网 IP"变为"Cloudflare 边缘出口 IP"，多源探测逻辑保留。 |

> 抓不到的站点（被墙、需验证）继续使用浏览器采集器兜底，两分支行为一致。

### 5.6 静态路由差异

| 路径 | Python（StaticFiles 挂载） | TS（Worker 路由） |
|---|---|---|
| `/`、`/index.html` | FastAPI 动态返回（ETag/304） | Workers Assets 托管 + ETag 头 |
| `/ICON/*` | 本地目录 | R2 对象代理（`Cache-Control: public, max-age=86400`） |
| `/background/*` | 本地目录 | R2 对象代理（`Cache-Control: public, max-age=86400`） |
| `/icons/*` | 本地图标库目录 | **删除**（改用 Simple Icons CDN 外链） |

---

## 6. 前端（index.html）改动清单

前端是两端共用的单文件，改动需**兼容两种部署**。所有改动仅涉及"图标库来源"，不涉及业务逻辑。

### 6.1 改动点列表

**改动 1**：删除 `getIconLibUrl(filename)` 函数（原约 L1784），图标 URL 改由 API 返回的 `item.url` 提供（Simple Icons CDN 地址）。

**改动 2**：`renderResultIconGrid()` 中（约 L2295、L2305）：
```javascript
// before
img.src = getIconLibUrl(item.file);
// after
img.src = item.url;
```

**改动 3**：`renderIconGrid()` 中（约 L2912）：
```javascript
// before
img.src = getIconLibUrl(item.file);
// after
img.src = item.url;
```

**改动 4**：`selectIcon(iconFile)` 中（约 L2937）：
```javascript
// before
editIconPreview.src = getIconLibUrl(iconFile);
// after
editIconPreview.src = "https://cdn.simpleicons.org/" + iconFile;
```

**改动 5**：图标搜索过滤（`filterIcons()` / `filterResultIcons()`），搜索字段从 `name/keyword` 改为 `name/slug`：
```javascript
// before
item.name.toLowerCase().includes(q) || item.keyword.toLowerCase().includes(q)
// after
item.name.toLowerCase().includes(q) || item.slug.toLowerCase().includes(q)
```

**改动 6**：`PUT /api/sites/{id}` 提交的 `body.icon_file` 从文件名（如 `github.svg`）改为 slug（如 `github`）。

> 说明：改动 2/3/5 需同时兼容 Python 端（本地图标库尚未删除时）与 TS 端。若 Python 端同步删除本地图标库并返回新结构，则改动 2-6 可直接生效；否则需按部署模式分支。**推荐决策：Python 端也执行方案 B**（删除本地图标库），使两端返回结构完全一致，前端零分支。

### 6.2 前端不动的内容

- 卡片网格、拖拽排序、标签筛选、管理弹窗。
- `toIconUrl()`、背景图相关逻辑。
- 采集器 `collectors/privlink.user.js`、`browser-extension/`。

---

## 7. 图标库方案（方案 B：Simple Icons CDN）

### 7.1 现状与决策

- 当前本地图标库：2657 个 SVG（约 2MB），文件名 `中文关键词_英文关键词.svg`。
- 实际使用率：数据库 10 个站点全部使用服务端抓取的 favicon，图标库使用率为 0。
- **决策：采用最简洁的方案 B**——删除本地图标库，改用 Simple Icons CDN 外链，不采用混合方案。

### 7.2 Simple Icons 数据

- 数据源：`https://cdn.simpleicons.org/simple-icons.json`（约 3000+ 图标）。
- 构建时生成 `deploy/cloudflare/assets/simple-icons.json`（脚本 `scripts/fetch-simple-icons.ts`），打包进 Worker bundle（约 2MB，远低于 128MB 内存限制）。
- 图标 URL 格式：`https://cdn.simpleicons.org/{slug}`。
- API 响应结构：`[{ name, slug, url }]`，`url = https://cdn.simpleicons.org/{slug}`。

### 7.3 Python 端同步调整（可选但推荐）

为使两端行为一致，Python 端 `main.py` 也需：

- 删除 `scan_icons_library()`（原 L1318）与 `/icons` 挂载（原 L1477）。
- `/api/icons` 改为返回 Simple Icons 结构（内嵌同一份 `simple-icons.json`）。
- `copy_library_icon()`（原 L916）逻辑：`icon_file` 为 slug，`icon_rel_path` 存 `https://cdn.simpleicons.org/{slug}`。

> 该调整与 Cloudflare 分支独立，可在任一阶段单独合并，仅当希望"前端零分支"时需要。

### 7.4 降级策略

- Simple Icons CDN 不可用时，前端 `img.onerror` 触发时图标位显示兜底（如域名首字母），与现有无图标行为一致。
- 抓取 favicon 的路径（`/ICON/*`）不受影响。

---

## 8. 开发计划（分阶段）

### Phase 1：基础设施（独立可先行）

**目标**：建立可部署骨架，不依赖业务逻辑。

- 新建 `deploy/cloudflare/` 目录：`package.json`（hono、@cloudflare/workers-types、wrangler）、`tsconfig.json`、`wrangler.toml`。
- `src/types.ts`：全部公共类型（`SiteItem`、`ParseResponse`、`BackgroundSettingResponse` 等，与 Python Pydantic 模型一一对应）。
- `src/db.ts`：D1 连接 + 建表迁移 + 查询函数。
- `src/index.ts`：Hono app + 基础路由（`/api/auth/status`、`GET /api/sites`、`GET /api/tags`）。
- `migrations/001_init.sql`：建表 SQL（从 `main.py:init_storage()` 移植）。

**验收**：`wrangler dev` 启动后，`/api/auth/status` 与 `GET /api/sites` 返回正确 JSON。

### Phase 2：存储层（R2 抽象）

**目标**：以 R2 替代 `Path` 文件 IO，提供与文件系统一致的接口。

- `src/storage.ts`：`putObject / getObject / deleteObject / listObjects` 抽象。
- 路由：`GET /ICON/*`、`GET /background/*` 代理 R2 对象，并带 `Cache-Control`。
- 背景图上传 / 删除端到端。

**验收**：R2 对象上传、读取、列表、删除全部可用；背景图上传即生效。

### Phase 3：Simple Icons 集成

**目标**：替换本地图标库。

- `scripts/fetch-simple-icons.ts`：拉取 `simple-icons.json` 生成 `assets/`。
- `src/simple-icons.ts`：静态数据 + 搜索过滤（`name`/`slug` 子串匹配）。
- `GET /api/icons` 端点。
- 图标选择逻辑：`icon_rel_path` 存 CDN URL。

**验收**：`/api/icons` 返回 Simple Icons 列表，搜索可用。

### Phase 4：站点处理核心（fetcher + site）

**目标**：实现 `POST /api/site/parse` 与 `POST /api/site/ingest` 核心逻辑。

- `src/fetcher.ts`：`fetchHtml / fetchIcon`，对应 Python `fetch_url()`（去掉代理/SSRF，保留重定向、限流、超时、重试）。
- `src/site.ts`：`processSiteUrl / processBrowserIngest`，对应 Python `process_site_url()` / `process_browser_ingest()`。
- HTML 解析（title / og:site_name / icon link 提取）在 TS 内用轻量正则或 DOM 解析器实现。
- 图标下载写入 R2（内容寻址文件名，同 Python `icon_filename()`）。

**验收**：`POST /api/site/parse` 对真实 URL 返回与 Python 端结构一致的响应。

### Phase 5：完整路由 + 端点对齐测试

**目标**：实现全部端点，与 Python 行为对齐。

端点清单（与 Python 完全一致）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | Workers Assets 托管 index.html（ETag/304） |
| GET | `/api/auth/status` | 门禁状态探测 |
| GET | `/api/sites` | 站点列表（公开，匿名只返回公开站点） |
| GET | `/api/tags` | 标签列表（公开，匿名不含私有站点独占标签） |
| GET | `/api/appearance/background` | 背景设置（公开） |
| GET | `/api/network/public-ip` | 公网 IP 多源探测 |
| GET | `/api/icons` | Simple Icons 列表 |
| POST | `/api/site/parse` | URL 解析入库 |
| POST | `/api/site/ingest` | 浏览器采集上报 |
| GET | `/api/appearance/background/images` | 背景图列表 |
| POST | `/api/appearance/background/images` | 上传背景图（multipart ≤5MB） |
| DELETE | `/api/appearance/background/images/{file}` | 删除背景图 |
| PUT | `/api/sites/reorder` | 拖拽排序 |
| PUT | `/api/sites/{id}` | 更新站点 |
| POST | `/api/sites/{id}/icon` | 上传站点图标（multipart ≤1MB） |
| DELETE | `/api/sites/{id}` | 删除站点 |
| GET | `/ICON/*` | R2 代理站点图标 |
| GET | `/background/*` | R2 代理背景图 |

- 鉴权中间件（token_guard）行为对齐。
- `tests/api.spec.ts`：端点对齐测试（见第 9 节）。
- 前端改动（第 6 节）落地。

**验收**：全部端点测试通过，前端在 `wrangler dev` 下功能完整。

### Phase 6：文档与发布

- 更新根 `README.md` 与 `docs/DEPLOYMENT.md`：增加 Cloudflare 部署章节。
- 提供一键部署脚本与首次初始化说明（见第 11 节）。
- 可选：Python 端方案 B 同步（第 7.3 节）。

---

## 9. 双栈一致性保障机制

### 9.1 单一事实来源：API 契约

以"端点路径 + 请求/响应结构 + 状态码"为契约。TS 端类型定义（`types.ts`）与 Python 端 Pydantic 模型一一对应，作为对齐基准。

### 9.2 端点对齐检查

CI 或本地脚本扫描两端端点，确保无遗漏：

```bash
# Python 端：列出全部 @app.* 路由
# TS 端：列出全部 app.get/post/put/delete 路由
# 对比两集合，缺失或多余即失败
```

### 9.3 双端测试

- Python：现有 pytest 不变（`tests/`）。
- TS：`deploy/cloudflare/tests/api.spec.ts` 对同一组请求断言相同响应结构。

### 9.4 新功能增量流程

```
新增功能
  │
  ├─ 1. 定义/更新 API 契约（types.ts + Pydantic 模型）
  │
  ├─ 2. Python 实现（main.py + tests/）
  │
  ├─ 3. TS 实现（deploy/cloudflare/src/ + tests/）
  │
  └─ 4. 端点对齐检查 + 双端测试通过 → 合并
```

---

## 10. 风险评估

| 风险 | 等级 | 缓解措施 |
|---|---|---|
| D1 与 SQLite 行为差异 | 低 | SQL 语法兼容；建表语句直接移植，`PRAGMA foreign_keys` 在 D1 中等价处理 |
| R2 读取延迟高于本地文件 | 低 | Workers 边缘缓存 R2 对象，冷启动约百毫秒级 |
| Simple Icons CDN 不可用 | 低 | 前端 `img.onerror` 兜底显示首字母，抓取 favicon 不受影响 |
| 前端改动破坏 Python 部署 | 中 | 改动集中在图标来源（第 6 节），Python 端同步方案 B 后两端一致；改动前保留 Python 端兼容分支 |
| Workers 内存限制（128MB） | 中 | simple-icons.json 约 2MB，打包无压力；避免一次加载全表大查询 |
| multipart 上传兼容 | 中 | Workers 原生支持 `request.formData()`，行为对齐 |
| 中文域名 / 非 ASCII favicon | 低 | 与 Python 端相同的 `normalize_url()` 逻辑移植 |

---

## 11. 部署与运维

### 11.1 首次初始化

```bash
# 1. 登录
npx wrangler login

# 2. 创建 D1 数据库
npx wrangler d1 create privlink

# 3. 创建 R2 buckets
npx wrangler r2 bucket create privlink-icons
npx wrangler r2 bucket create privlink-backgrounds

# 4. 在 wrangler.toml 中绑定 D1 与 R2，并配置 NAV_TOKEN 为 secret：
npx wrangler secret put NAV_TOKEN
```

### 11.2 开发调试

```bash
npx wrangler dev deploy/cloudflare/src/index.ts
```

### 11.3 一键部署

```bash
npx wrangler deploy deploy/cloudflare/src/index.ts
```

### 11.4 数据库迁移

```bash
npx wrangler d1 execute privlink --file=deploy/cloudflare/migrations/001_init.sql
```

### 11.5 数据迁移（从本地/Docker 迁入）

- 站点 / 标签 / 背景设置：导出 SQLite 为 SQL，用 `d1 execute --file` 导入。
- 图标 / 背景图：将 `ICON/`、`background/` 目录内文件按 key 上传到对应 R2 bucket。

---

## 附录：关键参考

| 参考 | 位置 |
|---|---|
| 现有部署文档 | `docs/DEPLOYMENT.md` |
| 产品功能概览 | `README.md` |
| Python 后端实现 | `main.py`（`init_storage`、`process_site_url`、`fetch_url`、`copy_library_icon` 等） |
| 前端 | `index.html`（`getIconLibUrl`、`renderIconGrid`、`filterIcons` 等） |
| 采集器 | `collectors/privlink.user.js`、`browser-extension/` |
| 现有测试 | `tests/` |
