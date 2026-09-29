# PrivLink

自部署的个人网站导航站。输入网址，自动解析站名与图标，卡片式展示、标签筛选、拖拽排序；支持公开/私有站点与访客只读浏览。基于 FastAPI + SQLite，无前端构建步骤、无外部数据库依赖，提供 Docker 镜像、Cloudflare Workers、源码三种部署方式，运行同一份 Python 代码。

## 功能特性

- **一键收录**：粘贴网址，服务端自动抓取网站名称（`og:site_name > title > 域名`）与 favicon 入库；同一网址重复提交自动更新，也支持从内置图标库（Simple Icons，3400+ 品牌图标）挑选或上传自定义图标。
- **可视化管理**：玻璃拟态卡片网格、多标签筛选（多选胶囊面板）、拖拽排序、右键编辑/删除。
- **公开 / 私有站点**：每个站点可勾选「公开站点」属性（默认公开）。私有站点带 🔒 标识，仅自己可见——连它的标签名都不会向访客泄露。
- **Token 门禁**：不设 Token 为开放模式（内网自用零门槛）；设置后，未持 Token 的访客打开页面即可静默浏览全部公开站点（只读、可点击跳转、零弹窗），触发添加/修改/删除/排序等管理动作时才引导保存 Token。
- **浏览器采集兜底**：目标站点有 Cloudflare、登录态等验证导致服务端抓不到时，用 Tampermonkey 脚本或 Chrome/Edge 扩展把浏览器里已通过验证的页面一键上报入库（不绕过验证码、不导出 cookie）。
- **轻量自部署**：单实例 uvicorn + SQLite + 本地图标目录；全部数据只有 `data/`、`ICON/` 与 `background/` 三个目录，备份即拷贝。Docker 与源码部署共用同一份 `.env` 配置；Cloudflare 部署由 Secrets / 变量承载同款配置。
- **顺手的细节**：首页 ETag/304 协商缓存 + gzip，二次打开秒级渲染（localStorage 本地缓存）；面板顶栏显示公网 IP 并可点击刷新（自部署显示服务端出口 IP，Cloudflare 部署显示访问者自己的 IP）；Linux 下自动启用 uvloop/httptools，Windows 开发环境自动兼容。

## 快速开始

三种部署方式运行同一份 Python 代码（`src/privlink/`），按基础设施选择：

| 方式 | 适合场景 | 说明 |
|---|---|---|
| **Docker**（推荐） | 服务器 / NAS 自部署 | 拉取预构建镜像 `luis330/privlink`（amd64 / arm64），数据挂三个目录 |
| **Cloudflare Workers** | 免服务器上云 | D1 + R2 存储，免费层覆盖个人使用，支持 GitHub Actions 一键部署，见[下文专节](#cloudflare-一键部署) |
| **源码运行** | 开发者 / 无 Docker 环境 | [uv](https://docs.astral.sh/uv/) 管理依赖，克隆即跑 |

### Docker（拉取预构建镜像）

```bash
git clone <仓库地址> privlink && cd privlink
cp .env.example .env    # 公网部署务必设置 NAV_TOKEN
docker compose pull && docker compose up -d
```

打开 `http://127.0.0.1:8000/` 即可使用；交互式 API 文档见 `/docs`。

- 升级：编排有更新时 `git pull`，再 `docker compose pull && docker compose up -d`。
- 版本锁定 / 回滚：在 `.env` 中设置 `DOCKER_IMAGE=luis330/privlink:0.1.0`；走自建镜像源同理（如 `DOCKER_IMAGE=docker.freeba.org/luis330/privlink:0.1.0`）。
- 开发者本地构建：`docker compose up -d --build`。

### 源码运行（需 [uv](https://docs.astral.sh/uv/)）

```bash
uv sync
cp .env.example .env    # 可选；公网部署务必设置 NAV_TOKEN
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

打开 `http://127.0.0.1:8000/` 即可使用（同上）。

## Cloudflare 一键部署

免费上云：同一份 Python 代码以 Cloudflare Python Workers 运行（D1 数据库 + R2 存储 + Workers Assets 静态资源），免费层覆盖个人使用，无需服务器。**无需预先创建任何资源**——D1 数据库与 R2 桶在首次部署时按名称自动创建，表结构由首个请求自动建立。

### 方式一：GitHub Actions（推荐，日常自动部署）

[![Deploy via GitHub Actions](https://img.shields.io/badge/Deploy%20via%20GitHub%20Actions-black?logo=githubactions&logoColor=white)](https://github.com/luis330/PrivLink/actions/workflows/deploy-cloudflare.yml)

> 1. Fork 本仓库到 GitHub 个人账号
> 2. 在 Fork 后的仓库页面点击顶部 **Actions** 标签，找到 **Deploy to Cloudflare Workers**，点击 **"I understand my workflows, go ahead and enable them"** 启用 Actions
> 3. 在仓库 **Settings → Secrets and variables → Actions** 添加：
>    - `CLOUDFLARE_API_TOKEN`（必需）：Cloudflare API Token（含 Workers / D1 / R2 读写权限，模板选 "Edit Cloudflare Workers"）
>    - `CLOUDFLARE_ACCOUNT_ID`（建议）：Cloudflare 账户 ID；不填则由 Token 自动解析
>    - `NAV_TOKEN`（可选）：门禁 Token；不配置则为开放模式
> 4. 在 **Actions** 标签页 → **Deploy to Cloudflare Workers** → **Run workflow** 手动触发首次部署
> 5. 部署成功后访问 `https://privlink.<你的-workers-子域>.workers.dev`；之后每次 push 到 `main` 分支且涉及 `src/`、依赖文件、`wrangler.jsonc`、前端文件或 workflow 时自动测试并重新部署

### 方式二：命令行本地部署

> 需要 [uv](https://docs.astral.sh/uv/) 与 Node.js（提供 `npx wrangler`）：
>
> ```bash
> uv sync
> uv run python scripts/patch-pywrangler.py   # 仅 Windows 需要（每次 uv sync 后执行）
> npx wrangler login
> python scripts/sync-frontend.py             # 同步前端到 assets/
> uv run pywrangler deploy
> npx wrangler secret put NAV_TOKEN           # 可选：设置门禁 Token
> ```
>
> 本地调试 Workers 版本可用 `uv run pywrangler dev`（secret 写在 `.dev.vars`）；已有本地 / Docker 数据可用 `scripts/migrate-local-to-cloudflare.py` 迁入。

> Cloudflare 部署的架构、与本地运行的差异、回滚与数据迁移见 [docs/CLOUDFLARE-DEPLOYMENT.md](docs/CLOUDFLARE-DEPLOYMENT.md)。

## 部署概览

| 项目 | 要求 |
|---|---|
| 运行环境 | Python 3.11+（uv 管理依赖）或任意 Docker 主机 |
| 预构建镜像 | Docker Hub `luis330/privlink`（amd64 / arm64 多架构；`latest` 随 main，`v*` tag 出版本号） |
| 资源占用 | 单实例（SQLite 单写入者），小型 VPS / NAS / 家用主机均可 |
| 网络 | 需能访问目标网站（可配 HTTP/SOCKS5 代理）；默认端口 8000 |
| 数据持久化 | `data/`（SQLite 数据库）、`ICON/`（站点图标）与 `background/`（背景图），备份迁移只拷这三个目录 |
| 公网部署 | 建议反向代理 + HTTPS（Caddy 一行 `reverse_proxy 127.0.0.1:8000` 即可） |

> **Docker 完整步骤（含 Debian 安装、维护升级、版本锁定与回滚）、源码部署（systemd 托管）、维护者发布镜像指南、全部环境变量、代理与内网抓取配置、API 细节，见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。**

## 访问控制

通过部署环境变量 `NAV_TOKEN` 控制：

- **不设置 = 开放模式**：所有 API 无需鉴权（适合内网/本机自用），浏览器采集接口保持禁用。
- **设置后 = 门禁模式**：除公开只读接口（`/api/sites`、`/api/tags`、`/api/auth/status`、`/api/appearance/background` 的 GET 请求；Cloudflare 部署下另含 `/api/network/public-ip`，返回的是访问者自己的 IP）外，全部 `/api/*` 要求请求头 `X-Nav-Token` 与之一致；首页、静态图标与背景图仍公开（文件名为不可枚举哈希，无目录列表）。

门禁模式下的站点可见性：**未持 token 的访客可浏览公开站点并点击跳转，但没有任何修改能力；取消勾选「公开站点」的私有站点仅持 token 的浏览器可见**（图标壳右上角有小锁标识），私有站点独有的标签名也不会出现在访客的标签栏里。

门禁模式下的使用流程：访客打开首页直接看到全部公开站点（只读，不会自动弹出 Token 输入窗，公网 IP 一栏显示「需 Token」）；访客触发管理动作时，才弹窗提示先保存访问 Token，关闭或取消即中止。您自己在浏览器点右上角「访问 Token」输入一次（保存前会先向服务器验证，验证通过才写入本机），即可解锁私有站点与全部管理能力；换浏览器或清除站点数据后重新输入；输入框留空保存可清除本机 Token、回到访客视图。

安全建议：token 使用 32 位以上随机字符串（如 `openssl rand -hex 24` 生成）；修改 token 需更新环境变量并重启服务，浏览器与采集器端同步更新。

## 浏览器采集模式

当目标网站需要 Cloudflare、登录态或浏览器验证时，推荐使用浏览器采集模式。它不会绕过验证码，也不会导出 cookie，只保存当前浏览器已经能正常打开的页面信息。

**Tampermonkey**：新建脚本并使用 `collectors/privlink.user.js` 的内容；在目标网站页面打开 Tampermonkey 菜单，先「设置 PrivLink API 地址」（默认 `http://127.0.0.1:8000`），再「设置 PrivLink Token」，最后「保存当前页到 PrivLink」。

**Chrome / Edge 插件**：扩展管理页启用开发者模式 →「加载已解压的扩展程序」选择 `browser-extension/` → 在插件选项里填写 API 地址与 `X-Nav-Token` → 在目标页面点击插件按钮保存。

采集上报依赖 `NAV_TOKEN` 启用（见上文章节）；接口细节见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 图标库版权

- 内置图标库使用 [Simple Icons](https://github.com/simple-icons/simple-icons)（CC0-1.0）的品牌图标元数据，图标本身由 `cdn.simpleicons.org` 外链提供，仓库内只保存名称与 slug 索引（`simple-icons.json`）。
- 品牌图标的商标权归各品牌方所有，本项目仅用于指示对应站点，不构成品牌背书。
- `ICON/` 目录存放的是各网站抓取的 favicon，仅用于指向其对应站点（与浏览器书签同类的指示性使用）。

## 许可证（License）

Copyright (c) 2026 luis

本项目以 [GNU Affero General Public License v3.0](LICENSE)（AGPL-3.0）开源：

- 您可以自由使用、修改与再分发本项目；
- 基于本项目修改后再分发，或**通过网络对外提供服务**（如部署为公开网站），必须以 AGPL-3.0 同等条款开放完整源代码，并保留原始版权声明与许可证文本，修改过的文件须标注改动说明；
- 若您基于本项目二次开发，欢迎（非强制）在您的 README 中注明来源并链接回本仓库。

第三方组件：内置图标库数据来自 [Simple Icons](https://github.com/simple-icons/simple-icons)（CC0-1.0，与 AGPL-3.0 兼容），归属声明见上文「图标库版权」章节。
