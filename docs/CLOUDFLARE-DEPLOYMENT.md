# PrivLink Cloudflare 部署架构与运维

PrivLink 在 Docker / 源码部署之外提供第三条部署路径：**Cloudflare Workers**。三条路径运行的是**同一份 Python 代码**（`src/privlink/`），Cloudflare 上以 Python Workers（Pyodide）运行。本文档说明 Workers 部署的架构、与本地运行的差异、开发调试与部署运维。

产品功能概览见根目录 [README](../README.md)，本地 / Docker 部署见 [docs/DEPLOYMENT.md](DEPLOYMENT.md)。

## 目录

1. [架构](#1-架构)
2. [Cloudflare 服务映射](#2-cloudflare-服务映射)
3. [本地与 Workers 的运行差异](#3-本地与-workers-的运行差异)
4. [开发与测试](#4-开发与测试)
5. [部署与运维](#5-部署与运维)

---

## 1. 架构

同一个 FastAPI 应用，两个入口：

| 入口 | 运行方式 | 存储 |
|---|---|---|
| `main.py`（垫片，`uvicorn main:app`） | 本地源码 / Docker | SQLite `data/sites.db` + `ICON/`、`background/` 目录 |
| `src/worker.py`（`asgi.entrypoint(app)`） | Cloudflare Python Workers | D1 + 两个 R2 桶 |

```
PrivLink/
├── main.py                          # 本地入口垫片（uvicorn main:app）
├── src/
│   ├── worker.py                    # Workers 入口
│   └── privlink/                    # 应用本体（两种部署共用）
│       ├── app.py                   # 路由、TokenGuard、PlatformBindings 中间件
│       ├── db.py                    # Database 抽象：SqliteDatabase / D1Database
│       ├── storage.py               # 文件存储抽象：本地目录 / R2
│       ├── config.py                # 配置与 IS_WORKERS 平台判断
│       └── data/simple_icons_data.py  # 图标库内嵌模块（Workers 读不到 JSON，见 3.1）
├── index.html / favicon.ico / *.png / manifest.json   # 前端与品牌图标（唯一来源）
├── assets/                          # Workers Assets 目录（由 sync-frontend.py 从根目录同步）
├── wrangler.jsonc                   # Workers 配置（D1 / R2 / Assets 绑定）
├── pyproject.toml / uv.lock         # 依赖（本地）
├── pylock.toml                      # Workers 依赖锁（pywrangler 生成，需提交）
├── tests/                           # pytest（覆盖两种后端的共性行为与 Workers 分支）
└── scripts/
    ├── sync-frontend.py             # 根目录前端文件 → assets/
    ├── build-favicon.py             # 一组 PNG → 多尺寸 favicon.ico（纯标准库）
    ├── fetch-simple-icons.py        # 拉取图标库数据，生成 json 与内嵌模块
    ├── migrate-local-to-cloudflare.py  # 本地数据 → D1 / R2
    └── patch-pywrangler.py          # Windows 本地开发用的 pywrangler 补丁（见 4.2）
```

平台差异被隔离在三处：

- **`PlatformBindings` 中间件**：Workers 的 bindings 只能从每个请求的 `scope["env"]` 拿到（lifespan 里拿不到），由它逐请求注入 D1 / R2（ContextVar），并把 `NAV_TOKEN` / `NAV_MODE` 回写到 `config`。本地运行时 scope 中没有 `env`，中间件直接透传。
- **`db.py` / `storage.py`**：业务代码只调用 `get_db()` 与 `storage.*`，不感知后端。
- **`config.IS_WORKERS`**（`sys.platform == "emscripten"`）：少数必须分叉的行为（见第 3 节）。

> **为什么是 `src/` 布局**：wrangler 会把 Python 入口所在目录下的全部 `*.py` 打进部署包，且没有排除机制。入口放在仓库根目录时，`.venv/`、`tests/` 等会被一并打包（约 43 MB，超出 Workers 限制）。因此 `src/` 下**只能放 Workers 运行需要的文件**。

---

## 2. Cloudflare 服务映射

| 能力 | 本地 / Docker | Cloudflare Workers |
|---|---|---|
| 运行时 | CPython + uvicorn | Pyodide（Python Workers） |
| 数据库 | SQLite `data/sites.db` | D1 `privlink`（binding `DB`，SQL 与 SQLite 兼容） |
| 站点图标 | `ICON/` 本地目录 | R2 桶 `privlink-icons`（binding `ICON_BUCKET`） |
| 背景图 | `background/` 本地目录 | R2 桶 `privlink-backgrounds`（binding `BACKGROUND_BUCKET`） |
| 首页与品牌图标 | FastAPI 显式路由（ETag/304） | Workers Assets（`assets/` 目录） |
| 图标库 | `simple-icons.json` | 内嵌模块 `simple_icons_data.py` |
| 远程抓取 | httpx（支持代理） | httpx over 平台 fetch（无代理） |
| gzip 压缩 | GZip 中间件 | 关闭中间件，由 Cloudflare 边缘处理 |
| 建表 | 启动时 `init_storage()` | 每个 isolate 的首个请求执行一次 `CREATE TABLE IF NOT EXISTS` |

### R2 与 Workers Assets 的职责边界

按「是否在运行时写入」划分，两者不可互换：

| 内容 | 性质 | 存储位置 |
|---|---|---|
| `index.html`、favicon、`manifest.json` | 部署时固定 | Workers Assets |
| `ICON/<file>` | 运行时上传或抓取 | R2 |
| `background/<file>` | 运行时上传 | R2 |

> Workers Assets 无法在运行时写入，动态内容必须走 R2。Assets 中命中的路径由 Cloudflare 直接响应，**不会进入 Python 代码**；未命中的请求才交给 Worker。

---

## 3. 本地与 Workers 的运行差异

### 3.1 存储约定

**R2 key 带目录前缀**：图标是 `ICON/<file>`，背景图是 `background/<file>`，即 URL 路径去掉开头的 `/`。前缀只在 `storage.py` 访问桶时拼接或剥离，其余代码一律使用裸文件名。

> ⚠️ 这是线上既有数据的格式（最早由 TS 实现写入），**不可改动**。曾因误用裸文件名 key 导致线上图标全部 404，背景设置也被自愈逻辑改写为默认值。`tests/test_storage_r2.py` 锁定了这条约定。

取值形态对照（迁移数据时勿混）：

| 字段 | 取值 | 示例 |
|---|---|---|
| `sites.icon_rel_path` | 含前缀的完整路径 | `ICON/9a5b632f….png` |
| `app_settings` 中背景设置的 `image` | 裸文件名 | `bg-8e7c640d….png` |
| 图标库图标的 `icon_rel_path` | CDN 外链 | `https://cdn.simpleicons.org/{slug}` |

前端 `toIconUrl()` 对 `http(s)://` 开头的值直接使用原值，其余按站内相对路径拼接。

**图标库数据**：Workers 部署包只收录 Python 与文本模块，JSON 文件不会进包，运行时读不到。因此 `fetch-simple-icons.py` 会额外生成 `src/privlink/data/simple_icons_data.py`，`list_simple_icons()` 依次尝试「工作目录下的 `simple-icons.json`（本地 / Docker）→ 内嵌模块 → 内置 60 个兜底」。

### 3.2 `GET /api/network/public-ip`

同一端点，两种语义，由响应中的 `kind` 字段区分：

| 部署 | 返回 | `kind` | 门禁 |
|---|---|---|---|
| 本地 / Docker | 服务端出口公网 IPv4（直连查询源、忽略代理） | `server` | 需要 token（反代 / 隧道部署下属于源站敏感信息） |
| Workers | 访问者自己的 IP（边缘注入的 `CF-Connecting-IP`，可能是 IPv6，原样返回） | `client` | 公开（对访客本人不构成泄露） |

Workers 的出口是 Cloudflare 任播边缘节点，探测服务端出口 IP 没有意义。前端依据 `kind` 给出提示文案。门禁差异由 `auth.is_public_readonly_path()` 实现，`tests/test_ingest.py` 中两种情况都有测试。

### 3.3 Workers 不提供的能力

| 能力 | 原因 |
|---|---|
| HTTP / SOCKS5 代理抓取 | Workers 运行在边缘网络，无法配置上游代理 |
| SSRF 的 DNS 解析校验 | Pyodide 没有可用的 `getaddrinfo`；只保留对 IP 字面量的拦截，其余依赖平台 fetch 本身无法访问内网 |
| `NAV_HOST_ALIASES` | 仅内网直连场景需要 |

> 抓不到的站点（需验证码、需登录态）两种部署都用浏览器采集器兜底，行为一致。

### 3.4 鉴权

两种部署一致：`NAV_TOKEN` 为空即开放模式；非空时，除公开只读清单外的 `/api/*` 都需要 `X-Nav-Token`。Workers 从 secret 读取该值。

`TokenGuard` 是纯 ASGI 中间件，必须**先注册**；`PlatformBindings` **后注册**，这样它在最外层，先把 env 中的 token 写入 `config`，TokenGuard 再做判断。

### 3.5 冷启动

Python Workers 首次加载需要初始化 Pyodide 和全部依赖：线上首个请求约需数秒，本地 `pywrangler dev` 热重载后的首个请求可能超过 15 秒。之后的请求是毫秒级。这是平台特性，不是故障。

---

## 4. 开发与测试

### 4.1 测试

```bash
uv run python -m pytest tests -q
```

测试在 CPython 下运行。Workers 分支通过 `mock.patch.object(config, "IS_WORKERS", True)` 模拟，R2 行为使用内存假桶（`tests/test_storage_r2.py`）。

### 4.2 本地运行 Workers（`pywrangler dev`）

```bash
uv sync
uv run python scripts/patch-pywrangler.py   # 仅 Windows 需要，每次 uv sync 后执行一次
uv run pywrangler dev                       # http://127.0.0.1:8787，使用本地 miniflare 的 D1/R2
```

- 本地 dev 的 secret 写在仓库根目录的 `.dev.vars`（已 gitignore），例如 `NAV_TOKEN=dev-secret-token`。这个文件只在启动时读取。
- **Windows 补丁**：pywrangler 用宿主 `.venv` 的解释器校验 `pylock.toml` 的 `requires-python`（>=3.13.2，即 Pyodide 版本），宿主为 3.12 时会失败。补丁让它改用 Pyodide 虚拟环境的解释器。`uv sync` 重装 workers-py 后补丁会丢失，需要重新执行。非 Windows 平台上脚本直接跳过。
- **`wrangler.jsonc` 和 `.dev.vars` 不能写中文**：pywrangler 在中文 Windows 上按 GBK 读取它们，遇到中文会报解码错误。
- 想用接近线上的数据调试：用 `npx wrangler d1 export privlink --remote` 导出后以 `--local` 导入；R2 对象用 `wrangler r2 object get … --remote` 拉取，再 `put … --local`。**不要用自己构造的数据代替线上数据**，线上 key 格式的问题正是这样漏掉的。

### 4.3 前端与品牌图标

仓库根目录的 `index.html`、品牌图标和 `manifest.json` 是唯一来源。本地部署直接读取它们；Workers 部署前需同步到 `assets/`：

```bash
python scripts/sync-frontend.py            # 同步
python scripts/sync-frontend.py --check    # 仅校验是否漂移，有差异时退出码 1
```

CI 部署前会自动执行同步。

图标是一整套位图，全部放在仓库根目录：

| 文件 | 用途 |
|---|---|
| `favicon.ico` | 多尺寸 ICO（16/32）。覆盖对 `/favicon.ico` 的隐式请求——浏览器与爬虫不解析 `<link rel="icon">`，会直接请求这个路径 |
| `favicon-16x16.png`、`favicon-32x32.png` | `<link rel="icon">` 显式声明，现代浏览器优先使用 |
| `apple-touch-icon.png` | iOS 主屏图标。同样存在隐式请求：Safari 没看到声明时会直接请求根路径 |
| `android-chrome-192x192.png`、`android-chrome-512x512.png` | 由 `manifest.json` 引用，用于 PWA 安装与 Android 主屏 |

在线 favicon 生成器导出的 `favicon.ico` 常常是改了扩展名的 PNG，直接使用会与 `Content-Type: image/x-icon` 名实不符。更换图标时先确认格式，必要时重新打包成真正的 ICO：

```bash
python scripts/build-favicon.py --inspect favicon.ico                  # 查看是 ICO 还是裸 PNG
python scripts/build-favicon.py favicon-16x16.png favicon-32x32.png    # 打包成 favicon.ico
python scripts/sync-frontend.py                                        # 再同步到 assets/
```

`tests/test_favicon.py` 会校验 ICO 容器格式、各 PNG 的实际像素尺寸以及 manifest 中图标的可达性。

---

## 5. 部署与运维

### 5.1 首次部署

**无需预先创建任何资源。** `wrangler.jsonc` 没有写 `database_id`，部署时 wrangler 按以下顺序处理：

1. 沿用已部署 Worker 上已有的 binding；
2. 否则按名称连接账户中已有的 D1 `privlink` 和 R2 桶 `privlink-icons`、`privlink-backgrounds`；
3. 都没有则自动创建。

表结构由 Worker 收到首个请求时自动建立。

> Fork 用户注意：以上资源都按**名称**在你自己的账户中查找或创建，不会指向他人的数据库。

### 5.2 GitHub Actions 自动部署

在仓库 **Settings → Secrets and variables → Actions** 添加：

| Secret | 必需 | 说明 |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | ✅ | 需要 Workers、D1、R2 的读写权限（模板选 "Edit Cloudflare Workers"） |
| `CLOUDFLARE_ACCOUNT_ID` | 建议 | 账户 ID；不填则由 token 解析 |
| `NAV_TOKEN` | 可选 | 门禁 token；不配置则部署为开放模式 |

触发方式：

- **自动**：push 到 `main`，且改动涉及 `src/`、`pyproject.toml`、`uv.lock`、`pylock.toml`、`wrangler.jsonc`、`assets/`、根目录前端文件、`scripts/sync-frontend.py` 或 workflow 文件。
- **手动**：Actions → Deploy to Cloudflare Workers → Run workflow。可在输入框临时填写 `NAV_TOKEN`，覆盖仓库 Secret。

Workflow 实际执行的步骤：

1. 安装 uv（Python 3.13）和 Node 22；
2. `uv sync --frozen`；
3. 运行 pytest，失败则不部署；
4. `scripts/sync-frontend.py` 同步前端到 `assets/`；
5. 安装固定版本的 wrangler（pywrangler 要求 >= 4.127.1）；
6. `uv run pywrangler deploy`；
7. 若 `NAV_TOKEN` 非空，执行 `wrangler secret put NAV_TOKEN` 写入（覆盖语义）；为空则跳过。

> **验证 secret 是否真正写入**：执行 `npx wrangler secret list --name privlink`，应能看到 `NAV_TOKEN`。只看 job 变绿不足以判断——secret 写入被跳过时 job 也会成功。

### 5.3 本地命令行部署

```bash
uv sync
uv run python scripts/patch-pywrangler.py   # 仅 Windows
npx wrangler login
python scripts/sync-frontend.py
uv run pywrangler deploy
npx wrangler secret put NAV_TOKEN           # 首次部署或更换 token 时执行
```

部署后的地址形如 `https://privlink.<你的-workers-子域>.workers.dev`。自定义域名在 Cloudflare Dashboard 的 Worker 设置中绑定。

### 5.4 回滚

```bash
npx wrangler versions list --name privlink             # 查看历史版本 ID
npx wrangler rollback <version-id> --name privlink     # 回滚到指定版本
```

回滚只切换代码版本，**不会回滚 D1 / R2 中的数据**。有风险的变更上线前，先备份数据库：

```bash
npx wrangler d1 export privlink --remote --output backups/d1-privlink-$(date +%Y%m%d).sql
```

`backups/` 已被 gitignore。

### 5.5 数据迁移（从本地 / Docker 迁入）

使用 `scripts/migrate-local-to-cloudflare.py`，把 `data/sites.db`、`ICON/`、`background/` 导入 D1 与 R2。脚本保留原 id，R2 key 自动加上正确前缀：

```bash
uv run python scripts/migrate-local-to-cloudflare.py --local             # 演练：只生成 SQL 并打印计划
uv run python scripts/migrate-local-to-cloudflare.py --local --apply     # 导入本地 miniflare 验证
uv run python scripts/migrate-local-to-cloudflare.py --remote --apply    # 导入线上
```

- 默认只演练，加 `--apply` 才会写入。
- 目标库已有站点时，脚本会拒绝导入；加 `--replace` 会先清空四张业务表再导入（R2 同名对象覆盖）。**覆盖前务必先按 5.4 导出备份。**
- 线上 D1 尚不存在时，先部署一次（会自动建库），再执行导入。
- 在中文 Windows 控制台运行时，设置 `PYTHONIOENCODING=utf-8` 可避免输出乱码（数据本身不受影响）。

### 5.6 注意事项

- Worker 名称固定为 `privlink`（见 `wrangler.jsonc`）；账户中已有同名 Worker 时会被覆盖部署。
- binding 名（`DB`、`ICON_BUCKET`、`BACKGROUND_BUCKET`）与资源名需与线上保持一致，改名会让部署连接到新的空资源。
- 更换 `NAV_TOKEN`：修改仓库 Secret 或在手动触发时填写输入框，然后重新运行 workflow；也可以在本地执行 `npx wrangler secret put NAV_TOKEN`。
- 自定义域名返回 `403 Your request was blocked.` 时，是 zone 的安全规则（WAF、IP / 地区限制、Bot 规则等）在 Worker 之前拦截了请求，需在 Dashboard → Security → Events 中按 Ray ID 排查，与应用代码无关。
