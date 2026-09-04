# PrivLink 部署与技术说明

面向部署者的完整文档：环境要求、Docker / 源码部署、全部配置项、代理与内网抓取、运行与缓存策略、API 细节。产品功能概览见根目录 [README](../README.md)。

## 目录

1. [部署要求](#1-部署要求)
2. [Docker 部署](#2-docker-部署)
3. [源码部署（无 Docker）](#3-源码部署无-docker)
4. [配置（.env）](#4-配置env)
5. [代理支持（HTTP / SOCKS5）](#5-代理支持http--socks5)
6. [运行策略说明](#6-运行策略说明)
7. [性能与缓存策略](#7-性能与缓存策略)
8. [API 说明](#8-api-说明)

---

## 1. 部署要求

- **运行环境**：Python 3.11+（推荐用 [uv](https://docs.astral.sh/uv/) 管理依赖），或任意支持 Docker 的主机。
- **网络**：服务端需能访问目标网站以抓取 HTML 与图标（可配代理，见[第 5 节](#5-代理支持http--socks5)）；默认监听 `8000` 端口。
- **磁盘**：全部状态只落在 `data/`（SQLite 数据库）、`ICON/`（站点图标）与 `background/`（背景图片）三个目录，项目目录需可写。
- **单实例约束**：`--workers 1` 为硬约束（SQLite 单写入者），单实例足够个人使用；不要横向扩容多个实例指向同一数据目录。
- 公网部署务必经反向代理启用 HTTPS。

## 2. Docker 部署

以 Debian 13 为例（其他发行版同理，安装 Docker 部分按官方文档调整）。

### 安装 Docker（如未安装）

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin || sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
```

### 部署步骤

1. 获取项目并进入目录：

```bash
git clone <your-repo-url>
cd PrivLink
```

2. 配置参数（可选但公网部署强烈建议）：

```bash
cp .env.example .env
# 编辑 .env：至少设置 NAV_TOKEN；代理/内网白名单等按需
```

3. 启动服务：

```bash
docker compose up -d --build
```

4. 验证访问：

- 页面：`http://<服务器IP>:8000/`
- 接口：`POST http://<服务器IP>:8000/api/site/parse`

### 基础镜像说明

`Dockerfile` 基础镜像默认为官方 `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`。拉取官方镜像困难时，在 `.env` 中配置 `DOCKER_BASE_IMAGE=<你的代理镜像地址>` 即可（`docker compose build` 自动生效）；不经 compose 直接构建时用 `docker build --build-arg BASE_IMAGE=<地址> .` 覆盖。

### 常用维护命令

```bash
docker compose ps          # 查看状态
docker compose logs -f     # 跟随日志

docker compose restart     # 重启（代码与镜像未变时）
docker compose down        # 停止并删除容器
```

### 升级代码与镜像

```bash
cd PrivLink
git pull
docker compose up -d --build
docker compose logs -f --tail=100
```

## 3. 源码部署（无 Docker）

适合不便使用 Docker 的服务器。项目无前端构建步骤，克隆后即可运行：

```bash
git clone <仓库地址> privlink && cd privlink
uv sync
cp .env.example .env   # 编辑 .env：至少设置 NAV_TOKEN
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

- 服务启动时自动加载项目根目录 `.env`（真实环境变量优先）。
- Linux 下自动启用 uvloop / httptools；Windows 开发环境自动跳过 uvloop，启动命令不变。
- 生产环境建议用 systemd 托管（开机自启、崩溃自动拉起）：

```ini
# /etc/systemd/system/privlink.service
[Unit]
Description=PrivLink
After=network-online.target

[Service]
WorkingDirectory=/opt/privlink
ExecStart=/usr/local/bin/uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now privlink
```

- 配置全部来自 `WorkingDirectory` 下的 `.env`，改配置只需编辑 `.env` 后 `systemctl restart privlink`。
- 对外域名场景经 Nginx / Caddy 反向代理并启用 HTTPS；本服务对反代无特殊要求（Caddy 一行 `reverse_proxy 127.0.0.1:8000` 即可）。
- 更新：`git pull` 后 `systemctl restart privlink`。
- **备份 / 迁移**：只需 `data/`（数据库）、`ICON/`（站点图标）与 `background/`（背景图片）三个目录；从 Docker 迁移时把挂载卷中的这三个目录拷入项目根即可。

> 本地开发同样使用上述 uv 命令。中国大陆网络环境下如需加速，可在用户级 uv 配置（`~/.config/uv/uv.toml` 或 Windows `%APPDATA%\uv\uv.toml`）中配置 PyPI 镜像（如 `https://pypi.tuna.tsinghua.edu.cn/simple/`），项目本身不锁定镜像源。

## 4. 配置（.env）

所有参数统一通过项目根目录的 `.env` 文件配置：复制 `.env.example` 为 `.env` 后按需修改。两种部署方式共用同一文件——源码部署时服务启动自动加载（真实环境变量优先），Docker Compose 部署时由 compose 自动读取做变量替换。

> 环境变量沿用 `NAV_` 前缀、API 鉴权头沿用 `X-Nav-Token`（项目历史沿革），保持既有部署与采集器客户端的完全兼容（`NAV_TOKEN` 兼容旧变量名 `NAV_INGEST_TOKEN`，`NAV_TOKEN` 优先）。

### 核心配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `NAV_TOKEN` | 空 | 访问 token。**空 = 开放模式**：所有 API 无需鉴权（适合内网/本机自用），浏览器采集接口禁用。**非空 = 门禁模式**：除公开只读接口外，全部 `/api/*` 需请求头 `X-Nav-Token` 与之一致，未携带或不一致返回 401。建议 32 位以上随机串（`openssl rand -hex 24`） |
| `NAV_MODE` | `single` | 体系模式。`multi`（多用户）为未来预留值，当前设置后按 single 运行并输出警告 |

安全提示：修改 token 需更新环境变量并重启服务，浏览器与采集器端同步更新；公网部署务必经反向代理启用 HTTPS。

### 可选：抓取增强

无代理 / 内网直连需求时可全部留空——服务端抓不到的站点（被墙、内网、需验证）改用浏览器采集器上报即可入库。

| 变量 | 默认 | 说明 |
|---|---|---|
| `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` | 空 | 标准代理环境变量，见[第 5 节](#5-代理支持http--socks5) |
| `NAV_HOST_ALIASES` | 空 | 主机解析映射（仅改 TCP 目标 IP、保留 Host/SNI），支持精确域名与 `*.example.com` 通配，见[本地域名](#本地域名--hosts-域名) |
| `NAV_ALLOWED_PRIVATE_NETWORKS` | 空（全禁内网） | SSRF 防护内网白名单，逗号分隔网段；更推荐内网站点用浏览器采集器上报，白名单保持默认 |
| `NAV_USER_AGENT` | 内置浏览器式 UA | 抓取目标网站时使用的 `User-Agent` |
| `NAV_ACCEPT_LANGUAGE` | `zh-CN,zh;q=0.9,en;q=0.8` | 抓取时的 `Accept-Language`（影响目标站返回的标题语言） |

### 可选：Docker 构建

| 变量 | 默认 | 说明 |
|---|---|---|
| `DOCKER_BASE_IMAGE` | 官方 `ghcr.io` 源 | 构建基础镜像地址，拉取困难时指向自己的代理/镜像仓库（仅 `docker compose build` 时生效） |

## 5. 代理支持（HTTP / SOCKS5）

> 可选配置：无代理时解析失败的站点可用浏览器采集模式兜底。

服务请求目标网站时支持代理，读取标准环境变量（在 `.env` 中配置）：

- `HTTP_PROXY`
- `HTTPS_PROXY`
- `ALL_PROXY`
- `NO_PROXY`

HTTP 代理：

```bash
export HTTP_PROXY=http://user:pass@127.0.0.1:7890
export HTTPS_PROXY=http://user:pass@127.0.0.1:7890
```

SOCKS5 代理（推荐 `socks5h`，DNS 也走代理）：

```bash
export ALL_PROXY=socks5h://user:pass@127.0.0.1:1080
export NO_PROXY=127.0.0.1,localhost
```

### 本地域名 / hosts 域名

如果目标网站只能通过本机 hosts 或内网 DNS 解析，例如 `*.example.com` 指向内网反向代理，Docker 容器里也必须具备同样的解析结果。否则浏览器能访问，不代表服务端抓取进程能访问。

同时，内网站点必须放进 `NO_PROXY`，避免 HTTPS 请求被送到外部代理后出现 `SSL: UNEXPECTED_EOF_WHILE_READING`、代理解析失败或证书握手异常。

`NAV_HOST_ALIASES` 用于给应用进程配置主机解析映射，支持精确域名和 `*.example.com` 通配子域名。映射只改变 TCP 连接目标 IP，HTTP `Host` 和 HTTPS SNI 仍保留原始域名。

综合示例（`.env`）：

```bash
HTTP_PROXY=http://192.168.1.2:7890
HTTPS_PROXY=http://192.168.1.2:7890
NO_PROXY=127.0.0.1,localhost,::1,example.com,.example.com
NAV_HOST_ALIASES=*.example.com=192.168.1.10
NAV_ALLOWED_PRIVATE_NETWORKS=192.168.1.0/24
```

## 6. 运行策略说明

- `url` 冲突时执行 upsert 更新。
- 启动时自动创建 `data/`、`ICON/`、`background/`、数据表，并自动执行 SQLite 列迁移（如 `is_public`）。
- 启用 SSRF 防护，默认禁止本机/内网地址；需要抓取内网站点时在 `.env` 的 `NAV_ALLOWED_PRIVATE_NETWORKS` 显式放行（或改用浏览器采集器上报）。
- Docker 默认单实例运行（`workers=1`），适配 SQLite。

## 7. 性能与缓存策略

- 响应启用 gzip 压缩（大于 1KB 的 HTML/JSON），首页传输体积约为原始大小的 1/6。
- 首页 `GET /` 支持 ETag / 304 协商缓存，内容未变化时刷新页面几乎零流量。
- 静态图标带浏览器缓存：`/ICON` 缓存 1 天，`/background` 背景图缓存 1 天。更换站点图标后浏览器最多 1 天内仍显示旧图，Ctrl+F5 强制刷新可立即生效。
- 前端把站点和标签数据缓存在 localStorage（`nav_cache_v1:*`）：再次打开页面立即渲染本地数据，后台拉取最新数据后自动更新；清除浏览器站点数据即可重置本地缓存。
- Linux 容器内自动启用 uvloop / httptools（依赖 `uvicorn[standard]`）；Windows 开发环境自动跳过 uvloop，启动命令不变。

## 8. API 说明

运行时访问 `http://<host>:8000/docs` 可查看交互式 OpenAPI 文档。门禁模式下，除下表标注「公开」的接口外，均需请求头 `X-Nav-Token`。

### 接口总览

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/` | 公开 | 前端页面（ETag/304 + gzip） |
| GET | `/api/auth/status` | 公开 | 门禁状态探测：`{token_required, authorized}` |
| GET | `/api/sites` | 公开 | 站点列表；匿名只返回公开站点 |
| GET | `/api/tags` | 公开 | 标签列表；匿名不含私有站点独占标签 |
| GET | `/api/appearance/background` | 公开 | 当前背景设置 `{type, color, image, image_url}`；匿名访客可见 |
| GET | `/api/network/public-ip` | 需 token | 服务端直连公网 IPv4（`{ip, kind:"server"}`；Cloudflare 端语义不同且对访客公开） |
| POST | `/api/site/parse` | 需 token | URL 解析入库 |
| POST | `/api/site/ingest` | 需 token | 浏览器采集上报 |
| GET | `/api/icons` | 需 token | 内置图标库列表（Simple Icons，返回 `{name, slug, url}`） |
| PUT | `/api/appearance/background` | 需 token | 设置背景（`type=default/color/image`） |
| GET | `/api/appearance/background/images` | 需 token | 背景图片列表 |
| POST | `/api/appearance/background/images` | 需 token | 上传背景图（multipart 字段 `image`，≤5MB，jpg/png/webp；上传即生效） |
| DELETE | `/api/appearance/background/images/{file}` | 需 token | 删除背景图；删的是当前背景时自动重置为默认 |
| PUT | `/api/sites/reorder` | 需 token | 拖拽排序持久化 |
| PUT | `/api/sites/{id}` | 需 token | 更新站点（名称/URL/标签/可见性/图标） |
| POST | `/api/sites/{id}/icon` | 需 token | 上传站点图标（multipart，≤1MB） |
| DELETE | `/api/sites/{id}` | 需 token | 删除站点 |
| GET | `/ICON/<file>`、`/background/<file>` | 公开 | 静态图标与背景图（文件名为不可枚举哈希，无目录列表） |
| GET | `/favicon.svg`、`/favicon.ico` | 公开 | 站点默认图标；`.ico` 覆盖浏览器与爬虫对根路径的隐式请求 |

### `GET /api/network/public-ip`

返回服务端当前网络的直连公网 IPv4。该查询明确忽略 `HTTP_PROXY`、`HTTPS_PROXY` 和
`ALL_PROXY`，不会降级为代理出口 IP。首页加载时会自动查询，点击“公网IP：”可立即刷新。

成功响应：

```json
{
  "ip": "203.0.113.10",
  "kind": "server"
}
```

`kind` 标明返回的是谁的 IP，本端恒为 `server`（服务端出口）。Cloudflare 部署下同名端点
返回 `client`（访问者自己的 IP）——Workers 的出口是 CF 任播边缘节点，探测出口 IP 无意义。
前端据该字段给出提示文案，详见 [CLOUDFLARE-DEPLOYMENT.md](CLOUDFLARE-DEPLOYMENT.md) 的 3.2。

所有直连查询源均不可用时返回 `502`：

```json
{
  "error": "无法获取直连公网 IPv4"
}
```

### `POST /api/site/parse`

请求体：

```json
{
  "url": "https://example.com"
}
```

响应字段：

- `url`: 规范化后的 URL（唯一键）
- `final_url`: 跟随重定向后的最终 URL
- `site_name`: 网站名称（`og:site_name > title > 域名`）
- `icon_rel_path`: icon 本地相对路径（如 `ICON/xxxxxx.ico`）
- `icon_source_url`: icon 下载来源 URL
- `status`: `success | partial | failed | invalid`
- `error`: 错误信息
- `warning`: 警告信息（例如页面被 403 拒绝但 icon 抓取成功）

### `POST /api/site/ingest`

浏览器采集上报接口，用于 Cloudflare 等 Web 验证导致服务端无法直接抓取的站点。用户先在真实浏览器中打开目标网站并通过验证，再由 Tampermonkey 脚本或浏览器插件读取当前页标题、URL 和 icon 后上报。

该接口默认禁用，需要部署时通过环境变量 `NAV_TOKEN` 配置访问 token，并在请求头中携带相同 token：

```http
X-Nav-Token: your-secret-token
```

请求体：

```json
{
  "url": "https://example.com/page",
  "final_url": "https://example.com/page",
  "site_name": "Example",
  "icon": {
    "source_url": "https://example.com/favicon.ico",
    "content_type": "image/x-icon",
    "filename": "favicon.ico",
    "data_base64": "AAABAA..."
  }
}
```

- `icon` 可为空；为空时如果该 URL 已存在，会保留旧 icon。
- icon 大小限制为 1MB。
- 支持常见图片类型：`ico/png/jpg/jpeg/svg/webp/gif/bmp/avif`。
