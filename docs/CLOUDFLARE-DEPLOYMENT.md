# PrivLink Cloudflare 部署架构与运维

PrivLink 在 Docker / 源码部署之外提供第三条部署路径：**Cloudflare Workers**。本文档说明该分支的架构、与 Python 分支的差异，以及部署运维步骤。

产品功能概览见根目录 [README](../README.md)，本地/Docker 部署见 [docs/DEPLOYMENT.md](DEPLOYMENT.md)，快速命令见 [deploy/cloudflare/README.md](../deploy/cloudflare/README.md)。

## 目录

1. [双栈架构](#1-双栈架构)
2. [Cloudflare 服务映射](#2-cloudflare-服务映射)
3. [与 Python 分支的差异](#3-与-python-分支的差异)
4. [双栈一致性保障](#4-双栈一致性保障)
5. [部署与运维](#5-部署与运维)

---

## 1. 双栈架构

Python/FastAPI 是主要实现（Docker / 源码部署），TypeScript/Hono + D1 + R2 作为独立目录 `deploy/cloudflare/` 并存。两者共用同一份前端与同一套 API 契约。

```
PrivLink/
├── main.py                          # FastAPI 实现（SQLite + 本地文件系统）
├── index.html                       # 前端单文件（两端共用）
├── simple-icons.json                # 图标库数据（两端共用数据源）
├── tests/                           # pytest
├── docker-compose.yml / Dockerfile  # 容器部署
├── collectors/ / browser-extension/ # 浏览器采集器
├── scripts/
│   ├── sync-frontend.py             # index.html + simple-icons.json → assets/
│   ├── fetch-simple-icons.py        # 从 unpkg 拉取图标库数据
│   └── check-api-alignment.py       # 双端端点对齐检查
└── deploy/cloudflare/               # TypeScript 分支
    ├── wrangler.toml                # Workers 配置（D1 / R2 / Assets 绑定）
    ├── src/
    │   ├── index.ts                 # Hono 入口，全部路由 + 鉴权中间件
    │   ├── types.ts                 # 公共类型（对应 Pydantic 模型）
    │   ├── db.ts                    # D1 数据库层
    │   ├── storage.ts               # R2 存储抽象
    │   ├── simple-icons.ts          # 图标数据加载与搜索
    │   └── fetcher.ts               # 远程抓取 + HTML 解析
    ├── assets/                      # Workers Assets（由 sync-frontend.py 同步生成）
    ├── migrations/001_init.sql      # D1 建表
    └── tests/
        ├── api.spec.ts              # 端点结构、URL 规范化、R2 路由 key
        ├── fetcher.spec.ts          # HTML 字节解码与解析
        └── bindings.spec.ts         # D1 参数绑定与输入校验
```

三层职责：

- **前端**：两端共用同一份 `index.html`，不感知部署差异。
- **API 契约**：端点路径、请求/响应结构、状态码为单一事实来源。
- **基础设施**：存储介质差异（SQLite/D1、本地目录/R2）完全隔离在各实现内部。

---

## 2. Cloudflare 服务映射

| 能力 | FastAPI（本地/Docker） | Cloudflare（TS 分支） |
|---|---|---|
| Web 框架 | FastAPI + uvicorn | Hono |
| 数据库 | SQLite `data/sites.db` | D1（SQL 语法与 SQLite 兼容） |
| 站点图标 | `ICON/` 本地目录 | R2 bucket `privlink-icons` |
| 背景图 | `background/` 本地目录 | R2 bucket `privlink-backgrounds` |
| 前端页面 | `index.html` 文件 | Workers Assets |
| 图标库 | `simple-icons.json` + CDN 外链 | 同左（`import` 打包进 bundle） |
| 远程抓取 | httpx（支持代理） | 原生 `fetch()`（无代理） |
| gzip 压缩 | FastAPI 中间件 | Cloudflare 边缘自动处理 |
| ETag / 304 | FastAPI 首页中间件 | 首页由 Workers Assets 提供；`/ICON/*`、`/background/*` 透传 R2 的 ETag |

### R2 与 Workers Assets 的职责边界

按"是否运行时写入"划分，两者不可互换：

| 内容 | 性质 | 存储位置 |
|---|---|---|
| `index.html` | 部署时固定 | Workers Assets |
| `simple-icons.json` | 构建时固定 | `import` 打包进 bundle |
| `ICON/<hash>` | 运行时上传/抓取 | R2 |
| `background/<hash>` | 运行时上传 | R2 |

> Workers Assets 无法运行时写入，动态内容必须走 R2。

---

## 3. 与 Python 分支的差异

### 3.1 `icon_rel_path` 语义

| 场景 | Python | TS |
|---|---|---|
| 服务端抓取 favicon | `ICON/9a5b632f….png`（本地路径） | `ICON/9a5b632f….png`（R2 key） |
| 浏览器上传图标 | `ICON/upload-xxx.png` | `ICON/upload-xxx.png`（R2 key） |
| 从图标库选择 | `https://cdn.simpleicons.org/{slug}` | 同左 |

两端取值刻意保持一致。TS 端有一条硬约束：**`icon_rel_path` 与 R2 key 必须是同一个字符串**——`/ICON/*` 路由按 `path.slice(1)` 取 key，`deleteObject()` 也直接拿 `icon_rel_path` 当 key 用。写入时若只存裸文件名，前端会拼出 `/9a5b632f….png` 而绕过 `/ICON/*` 路由，图标一律 404。背景图同理：写入 key 为 `background/<file>`，读取路由同样用 `path.slice(1)`。

前端 `toIconUrl()` 对 `http(s)://` 开头的值直接返回原值（图标库 CDN 外链），其余按站内相对路径拼接。

### 3.2 TS 端不提供的能力

| 能力 | 原因 |
|---|---|
| HTTP / SOCKS5 代理抓取 | Workers 运行在边缘网络，无法配置上游代理 |
| SSRF 防护（内网网段白名单、`getaddrinfo` 钩子） | Workers 的 `fetch()` 无法访问内网，风险面由平台隔离 |
| `NAV_HOST_ALIASES` | 仅内网直连场景需要 |
| `GET /api/network/public-ip` | Workers 出口 IP 由 Cloudflare 管理，探测无意义，端点已从 TS 端移除（Python 端保留）。`check-api-alignment.py` 中以 `EXEMPT` 豁免 |

> 抓不到的站点（需验证码、需登录态）两端都用浏览器采集器兜底，行为一致。

### 3.3 静态路由

| 路径 | Python | TS |
|---|---|---|
| `/`、`/index.html` | FastAPI 动态返回（ETag/304 + gzip） | Workers Assets 托管 |
| `/ICON/*` | 本地目录（StaticFiles） | R2 流式回源 |
| `/background/*` | 本地目录（StaticFiles） | R2 流式回源 |

TS 端两个 R2 路由共用 `serveR2Object()`，行为要点：

- **流式**：把 R2 的 `ReadableStream` 直接交给 `Response`。若先 `await arrayBuffer()`，整个对象要读进 Worker 内存才开始响应——1MB 的背景图会让 TTFB 涨到秒级，并占用 128MB 的实例内存。
- **Content-Type 按扩展名推断**（`contentTypeForKey()`）。Python 端由 `StaticFiles` 自动补该头；Workers 侧手写 `Response` 必须自己带上，否则浏览器不会在 `<img>` 中渲染，SVG 尤其严格。
- **条件请求**：透传 R2 的 `httpEtag`，并把请求头交给 R2 的 `onlyIf` 处理，重复访问命中 304。
- 两端缓存头一致：`Cache-Control: public, max-age=86400`。

### 3.4 鉴权

两端一致：`NAV_TOKEN` 为空即开放模式，非空则除 `/api/sites`、`/api/tags`、`/api/auth/status`、`/api/appearance/background` 外的 `/api/*` 均需 `X-Nav-Token`。TS 端从 Workers secret 读取该值。

### 3.5 已知残留差异

| 差异 | 说明 |
|---|---|
| URL 中显式写出的默认端口 | `https://x:443` 被 WHATWG `URL` 归一化掉，Python 的 `urlsplit` 则保留 `:443`。要对齐需回退到字符串级解析，代价大于收益，未处理（见 `normalizeUrl()` 注释） |
| `POST /api/site/ingest` 的 `error` 字段 | 非 `success` 时填字符串 `"partial"`，语义与 Python 端不完全一致。修改会影响采集器的判断逻辑，暂未调整 |
| `initStorage()` 执行时机 | Python 只在启动时建表一次；Workers 无常驻状态，当前每个请求都执行一遍 5 条 `CREATE TABLE IF NOT EXISTS`。功能正确，但每请求多 5 次 D1 往返 |

> 除上述三项外，两端行为以 `scripts/check-api-alignment.py` 与双端测试为准。凡是 Python 端由标准库隐式完成的事（`urlsplit` 的 scheme、`httpx` 的字节解码、`Path` 的目录拼接、`StaticFiles` 的 MIME 头），在 Workers 侧都必须显式实现——历史缺陷绝大多数出自这一类遗漏。

---

## 4. 双栈一致性保障

### 4.1 端点对齐检查

```bash
python3 scripts/check-api-alignment.py
```

脚本扫描 `main.py` 的 `@app.*` 装饰器与 `index.ts` 的 `app.get/post/put/delete`，比对两端端点集合（自动归一化 `{id}` ↔ `:id`）。有意的单端端点通过脚本内 `EXEMPT` 列表豁免。

### 4.2 前端同步

根目录 `index.html` 与 `simple-icons.json` 是唯一来源，通过脚本同步到 Workers Assets：

```bash
python3 scripts/sync-frontend.py            # 同步
python3 scripts/sync-frontend.py --check    # 仅校验是否漂移
```

`package.json` 的 `deploy` 与 `icons:fetch` 均已挂载该脚本，CI 部署前也会执行，避免两处漂移。

### 4.3 测试

```bash
uv run python -m pytest tests/ -q                       # Python 端
cd deploy/cloudflare && npm run typecheck && npm test   # TS 端
```

TS 端测试的重点不是覆盖率，而是**锁住两端易漂移的约定**：

| 文件 | 覆盖 |
|---|---|
| `api.spec.ts` | 端点响应结构；URL 规范化逐字符对齐 Python 输出；`/ICON/*`、`/background/*` 取的 R2 key 与写入形式一致；Content-Type 正确 |
| `fetcher.spec.ts` | `fetchHtml()` 返回字符串而非字节；utf-8 / gb18030 解码；图标兜底 |
| `bindings.spec.ts` | 用**按 SQL 占位符数量校验 bind 参数**的严格 D1 stub 拦截参数绑定错误；slug 校验、路径穿越、scheme 校验、标签长度 |

> 涉及 Python 行为的期望值一律取自 Python 端的实际输出，不靠推断。宽松的手写 stub 会放行参数数量错误等缺陷，`bindings.spec.ts` 的严格 stub 正是为此。

### 4.4 新增功能的流程

1. 更新 API 契约（`types.ts` + Pydantic 模型）
2. Python 实现（`main.py` + `tests/`）
3. TS 实现（`deploy/cloudflare/src/` + `tests/`）
4. 端点对齐检查 + 双端测试通过

---

## 5. 部署与运维

### 5.1 首次初始化（必须手动执行一次）

> **CI 不会创建 Cloudflare 资源。** `.github/workflows/deploy-cloudflare.yml` 只执行「同步前端 → `wrangler deploy` → 写入 `NAV_TOKEN` secret」，不含 D1/R2 创建、迁移执行或 `database_id` 填充。首次部署前必须在本地完成下列步骤，否则部署会因绑定缺失而失败。

```bash
cd deploy/cloudflare
npm install
npx wrangler login

npx wrangler d1 create privlink                  # 记下返回的 database_id
npx wrangler r2 bucket create privlink-icons
npx wrangler r2 bucket create privlink-backgrounds

# 把上一步的 uuid 填入 wrangler.toml 的 database_id 并提交
npx wrangler d1 execute privlink --remote --file=migrations/001_init.sql
```

### 5.2 GitHub Actions 自动部署

在仓库 **Settings → Secrets and variables → Actions** 添加：

| Secret | 必需 | 说明 |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | ✅ | 含 Workers / D1 / R2 读写权限（模板选 "Edit Cloudflare Workers"） |
| `CLOUDFLARE_ACCOUNT_ID` | 建议 | 账户 ID；不填则由 Token 解析 |
| `NAV_TOKEN` | 可选 | 门禁 Token；不配置则部署为开放模式 |

触发方式：

- **自动**：push 到 `main` 且改动涉及 `deploy/cloudflare/`、根 `index.html`、`simple-icons.json` 或 workflow 文件。
- **手动**：Actions → Deploy to Cloudflare Workers → Run workflow，可在输入框临时填写 `NAV_TOKEN` 覆盖仓库 Secret。

Workflow 实际执行：

1. `python3 scripts/sync-frontend.py` 同步前端资源到 `assets/`
2. `wrangler deploy`
3. 若 `NAV_TOKEN` 非空，`wrangler secret put NAV_TOKEN` 写入（覆盖语义）；为空则跳过，保持开放模式

部署成功后地址形如 `https://privlink.<你的-workers-子域>.workers.dev`。

> **验证 secret 是否真正写入**：部署日志的绑定列表中应出现 `env.NAV_TOKEN  Secret`。只看 job 变绿不足以判断——secret 写入失败或步骤被跳过时 job 仍会成功。

### 5.3 本地命令行部署

```bash
cd deploy/cloudflare
npm run deploy          # 内含 sync-frontend.py + wrangler deploy
```

### 5.4 数据迁移（从本地/Docker 迁入）

- **站点 / 标签 / 背景设置**：导出 SQLite 为 SQL，`wrangler d1 execute privlink --remote --file=<dump>.sql` 导入。
- **图标 / 背景图**：上传到 R2 时 **key 必须带目录前缀**，与 `sites.icon_rel_path`、`app_settings` 中的取值严格对应：

  | 本地文件 | R2 key |
  |---|---|
  | `ICON/9a5b632f….png` | `ICON/9a5b632f….png` |
  | `background/bg-8e7c640d….png` | `background/bg-8e7c640d….png` |

  ```bash
  npx wrangler r2 object put privlink-icons/ICON/9a5b632f.png --file=ICON/9a5b632f.png --remote
  npx wrangler r2 object put privlink-backgrounds/background/bg-8e7c640d.png --file=background/bg-8e7c640d.png --remote
  ```

  > 传成裸文件名（不带 `ICON/` 或 `background/`）会导致 `/ICON/*`、`/background/*` 一律 404——读取路由按 `path.slice(1)` 取 key，前缀是 key 的一部分。

- **背景设置的取值形态**：`app_settings` 里 `image` 字段存的是**裸文件名**（`bg-….png`），拼接前缀由路由与 `backgroundImageUrl()` 负责；`sites.icon_rel_path` 存的则是**含前缀的完整 key**。两者不同，迁移时勿混。

### 5.5 注意事项

- Worker 名称固定为 `privlink`（见 `wrangler.toml`）；账户中已存在同名 Worker 会被覆盖部署。
- `wrangler.toml` 中的 `database_id` 会随仓库提交，**fork 前请替换为自己的 uuid**，否则会指向他人数据库。
- 更换 `NAV_TOKEN`：改仓库 Secret 或手动触发时填写输入框，重新运行 workflow 即可覆盖。
