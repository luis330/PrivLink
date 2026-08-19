# PrivLink Cloudflare 分支 — 实施进度

## ✅ Phase 1-6 全部完成 — 已上线

**线上地址**：https://privlink.wanglf.workers.dev （Worker `privlink`，D1 `privlink`，R2 `privlink-icons` / `privlink-backgrounds`）

### 线上验证结果

| 端点 | 结果 |
|---|---|
| `/api/auth/status` | ✅ `{"token_required":false,"authorized":true}` |
| `/api/sites` | ✅ `[]` |
| `/api/tags` | ✅ `[]` |
| `/api/icons` | ✅ 3453 个，`{name, slug, url}`，slug 来自官方 npm 包 |
| `/api/icons?q=github` | ✅ 匹配 GitHub / GitHub Actions / Copilot / Pages / Sponsors 等 |
| `/api/appearance/background` | ✅ `{"type":"default",...}` |
| `/api/appearance/background/images` | ✅ `[]` |
| `/` (静态页) | ✅ 返回完整前端页面（137KB `index.html`） |
| `cdn.simpleicons.org/{slug}` | ✅ 200 image/svg+xml |

### 部署期修复记录

5. **页面仅显示占位文字 "PrivLink — Cloudflare"**：根因是 `index.html`（前端 137KB 自包含文件，位于仓库根目录）从未被复制进 `deploy/cloudflare/assets/`，且 Worker 中注册了 `app.get("/")` 显式路由抢先返回占位 HTML。修复：
   - 将根目录 `index.html` 复制到 `assets/index.html`（Workers Assets 自动服务）
   - 删除 `index.ts` 中 `app.get("/")` 路由，让 Assets 接管 `/`

1. **D1 运行时 `d.execute is not a function`**：D1 无 `.execute()`，`initStorage` 改为逐条 `db.prepare(sql).run()`；`getAppSetting` 用 `prepare().bind().first("value")`。
2. **`CREATE TABLE ... (: incomplete input: SQLITE_ERROR`**：多行模板字符串 SQL 在 D1 报错，改为单行 SQL 字符串。
3. **`/api/icons?q=github` 报 `Cannot read properties of undefined`**：原 `assets/simple-icons.json` 来自 GitHub `data/simple-icons.json`，为 **list 且无 slug 字段**（CDN 需要 slug）。改为从 `unpkg.com/simple-icons@latest/icons.json` 拉取带 slug 的权威数据（3453 条），`simple-icons.ts` 相应改为遍历数组、用 `e.slug`。
4. **`/api/network/public-ip` 返回"无法获取直连公网 IPv4"**：Workers 出口被 Cloudflare 管理，无法访问外部 IP 探测服务；此端点仅在本地部署有意义（已知限制，见需求文档）。

### 后续改进（P0 清理）

6. **删除 TS 端 `/api/network/public-ip` 端点**：该端点仅在 Python 端保留（本地部署有意义）；CF 端探测总失败、前端已降级显示"获取失败"，删除避免误导。`check-api-alignment.py` 增加 `EXEMPT` 豁免机制（现豁免 3 个：`GET /`、`GET /index.html`、`GET /api/network/public-ip`）。
7. **图标数据统一为共享数据源**：`scripts/fetch-simple-icons.py` 同时写入根目录 `simple-icons.json`（Python 端启动时读取，3453 条）与 `deploy/cloudflare/assets/simple-icons.json`（Workers Assets）。`main.py` 的 `_SIMPLE_ICONS` 重命名为 `_SIMPLE_ICONS_FALLBACK`（兜底），`list_simple_icons()` 优先读共享文件；`PUT /api/sites/{id}` 的 slug 校验改用 `_icons_cache`。Dockerfile 已打包 `simple-icons.json`。
8. **前端同步机制**：新增 `scripts/sync-frontend.py`，将根目录 `index.html` 与 `simple-icons.json` 同步到 `deploy/cloudflare/assets/`（支持 `--check` 校验模式）；`package.json` 的 `deploy` 与 `icons:fetch` 钩子自动先同步，避免两处漂移。

### 交付文件清单

**TypeScript / Cloudflare 分支**
| 文件 | 说明 |
|---|---|
| `deploy/cloudflare/src/index.ts` | Hono 入口，全部 API 端点 + CORS + 鉴权中间件 + R2 静态代理（无 `/api/network/public-ip`，Python 专属） |
| `deploy/cloudflare/src/types.ts` | 公共 TS 类型（与 Pydantic 模型对齐） |
| `deploy/cloudflare/src/db.ts` | D1 数据库层（initStorage / 设置查询 / 标签查询） |
| `deploy/cloudflare/src/storage.ts` | R2 存储抽象（put/get/delete/list + SHA-256 哈希工具） |
| `deploy/cloudflare/src/simple-icons.ts` | Simple Icons 静态数据 + 搜索 |
| `deploy/cloudflare/src/fetcher.ts` | 远程抓取（重定向/限流/超时）+ HTML 解析 |
| `deploy/cloudflare/src/env.d.ts` | 本地开发类型声明 |
| `deploy/cloudflare/wrangler.toml` | Workers 配置（D1 / R2 / Assets 绑定） |
| `deploy/cloudflare/tsconfig.json` / `package.json` | 编译与依赖配置 |
| `deploy/cloudflare/migrations/001_init.sql` | D1 建表迁移 |
| `deploy/cloudflare/assets/simple-icons.json` | 图标库数据（3453 条，含 slug，来自 `unpkg simple-icons@latest`） |
| `deploy/cloudflare/tests/api.spec.ts` | 端点结构测试 |
| `deploy/cloudflare/README.md` | 快速开始指南 |

**Python 端同步改动（方案 B）**
| 文件 | 改动 |
|---|---|
| `main.py` | 删除 `/icons` 静态挂载；`copy_library_icon()` → `icon_url_for_slug()`；`/api/icons` 返回 Simple Icons 结构（启动时读根目录 `simple-icons.json`，3453 条）；`PUT /api/sites/{id}` 接受 slug |
| `scripts/fetch-simple-icons.py` | 从 `unpkg simple-icons@latest/icons.json` 拉取完整图标库（含 slug），写入根目录 `simple-icons.json`（共享数据源）与 `deploy/cloudflare/assets/`；`fetch-simple-icons.mjs`（node 版）弃用 |
| `scripts/check-api-alignment.py` | Python ↔ TypeScript 端点对齐检查（支持 async 函数、`app.mount`、`EXEMPT` 豁免、参数命名归一化） |
| `scripts/sync-frontend.py` | 同步根目录 `index.html` / `simple-icons.json` → `deploy/cloudflare/assets/`（支持 `--check`） |
| `.gitignore` | 新增：`deploy/cloudflare/node_modules/` |
| `simple-icons.json` | 共享图标数据源（3453 条，git 提交，Docker 打包） |
| `Dockerfile` | 新增 `COPY simple-icons.json ./` |

**文档**
| 文件 | 说明 |
|---|---|
| `docs/CLOUDFLARE-DEPLOYMENT.md` | 完整需求说明与开发计划 |
| `deploy/cloudflare/PROGRESS.md` | 本文件 |

---

### 验证结果

| 检查项 | 结果 |
|---|---|
| `uv run python -m pytest tests/ -q` | ✅ 38 passed |
| `npm run typecheck` (Cloudflare 分支) | ✅ 0 errors |
| `npx wrangler deploy` | ✅ 部署成功，Assets + D1 + R2 绑定正常 |
| `python scripts/check-api-alignment.py` | ✅ 端点一致（差异仅为参数命名格式 `{id}` ↔ `:id`） |

---

## 首次部署步骤

```bash
# 1. 安装依赖
cd deploy/cloudflare
npm install

# 2. 登录并创建资源
npx wrangler login
npx wrangler d1 create privlink
npx wrangler r2 bucket create privlink-icons
npx wrangler r2 bucket create privlink-backgrounds

# 3. 编辑 wrangler.toml 填入 database_id

# 4. 配置 Token
npx wrangler secret put NAV_TOKEN

# 5. 执行数据库迁移
npx wrangler d1 execute privlink --file=migrations/001_init.sql

# 6. 拉取完整图标库（可选）
npm run icons:fetch

# 7. 部署
npx wrangler deploy
```

---

## 前端改动摘要（index.html）

共 6 处修改，已同步至两侧：

1. 删除 `getIconLibUrl()` 函数
2. `renderResultIconGrid()`：`item.file` → `item.slug`，`getIconLibUrl(item.file)` → `item.url`
3. `filterResultIcons()`：`item.keyword` → `item.slug`
4. `renderIconGrid()`：同上
5. `filterIcons()`：同上
6. `selectIcon()`：接受 slug，直接拼接 CDN URL
