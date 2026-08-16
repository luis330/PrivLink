# Nav Local Service

一个基于 FastAPI 的网站记录采集服务。输入网页 URL 后，服务会解析网站名称和 icon，并写入本地 SQLite。

## 功能

- 前端页面：`GET /`
- 直连公网 IPv4：`GET /api/network/public-ip`
- 解析接口：`POST /api/site/parse`
- 浏览器采集上报接口：`POST /api/site/ingest`
- icon 静态访问：`GET /ICON/<filename>`
- 存储：
  - SQLite：`data/sites.db`
  - 图标目录：`ICON/`

## 本地运行（uv）

1. 安装依赖：

```bash
uv sync
```

> 中国大陆网络环境下如需加速，可在用户级 uv 配置（`~/.config/uv/uv.toml` 或 Windows `%APPDATA%\uv\uv.toml`）中配置 PyPI 镜像（如 `https://pypi.tuna.tsinghua.edu.cn/simple/`），项目本身不锁定镜像源。

2. 配置参数（可选）：复制 `.env.example` 为 `.env` 并按需修改（见下文「配置方式」）。

3. 启动服务：

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

4. 访问：

- 页面：`http://127.0.0.1:8000/`
- OpenAPI：`http://127.0.0.1:8000/docs`

## API

### `GET /api/network/public-ip`

返回服务端当前网络的直连公网 IPv4。该查询明确忽略 `HTTP_PROXY`、`HTTPS_PROXY` 和
`ALL_PROXY`，不会降级为代理出口 IP。首页加载时会自动查询，点击“公网IP：”可立即刷新。

成功响应：

```json
{
  "ip": "203.0.113.10"
}
```

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

## 访问控制（单用户模式）

通过部署时的环境变量控制：

- `NAV_MODE`：体系模式，默认 `single`（单用户自部署）。`multi`（多用户）为未来预留值，当前设置后按 single 运行并输出警告。
- `NAV_TOKEN`：访问 token（兼容旧变量名 `NAV_INGEST_TOKEN`，`NAV_TOKEN` 优先）。
  - **不设置 = 开放模式**：所有 API 无需鉴权（适合内网/本机自用），浏览器采集接口保持禁用。
  - **设置后 = 门禁模式**：除公开只读接口外，全部 `/api/*` 要求请求头 `X-Nav-Token` 与之一致，未携带或不一致返回 401；首页页面与静态图标仍公开（图标文件名为不可枚举哈希，无目录列表）。

门禁模式下的站点可见性：每个站点有「公开站点」属性（添加结果页与编辑弹窗中勾选，默认公开）。**未持 token 的访客可浏览公开站点并点击跳转**（`GET /api/sites`、`GET /api/tags` 只返回公开站点及其标签，私有站点的标签名也不泄露），但没有任何修改能力；**取消勾选的私有站点仅持 token 的浏览器可见**（图标壳右上角有小锁标识）。前端通过公开状态接口 `GET /api/auth/status`（返回 `{token_required, authorized}`，不泄露敏感信息）感知服务器是否处于门禁模式。

门禁模式下的使用流程：未持 token 的访客打开首页直接看到全部公开站点（只读，**不会自动弹出 Token 输入窗**，公网 IP 一栏显示「需 Token」）；访客触发添加、修改、删除、拖拽排序等管理动作时，才弹窗提示先保存访问 Token，关闭或取消弹窗即中止该操作。您自己在浏览器点右上角「访问 Token」输入一次（保存前会先向服务器验证，验证通过才写入本机），保存在该浏览器的 localStorage 并解锁私有站点与全部管理能力；换浏览器或清除站点数据后重新输入即可；输入框留空保存可清除本机 Token、回到访客视图。

安全建议：token 使用 32 位以上随机字符串（如 `openssl rand -hex 24` 生成）；公网部署务必经反向代理启用 HTTPS；修改 token 需更新环境变量并重启服务，浏览器与采集器端同步更新。

## 浏览器采集模式

当目标网站需要 Cloudflare、登录态或浏览器验证时，推荐使用浏览器采集模式。它不会绕过验证码，也不会导出 cookie，只保存当前浏览器已经能正常打开的页面信息。

### 启用 token

浏览器采集与访问控制共用同一个 token，在 `.env` 中设置 `NAV_TOKEN` 后重启服务生效（见上文「访问控制」与「配置方式」章节）：

```bash
# .env
NAV_TOKEN=your-secret-token
```

源码部署重启：`systemctl restart nav-local`（或重新运行 uvicorn）；Docker 部署重启：`docker compose up -d`。

### Tampermonkey

1. 安装 Tampermonkey。
2. 新建脚本并使用 `collectors/nav-local.user.js` 的内容。
3. 在任意目标网站页面中打开 Tampermonkey 菜单：
   - 先执行“设置 Nav Local API 地址”，默认是 `http://127.0.0.1:8000`。
   - 再执行“设置 Nav Local Token”，填入部署配置的访问 token。
   - 通过验证并停留在目标页后，执行“保存当前页到 Nav Local”。

### Chrome / Edge 插件

1. 打开浏览器扩展管理页并启用开发者模式。
2. 选择“加载已解压的扩展程序”，目录选择 `browser-extension/`。
3. 打开插件“选项”，填写 API 地址和 `X-Nav-Token`。
4. 在目标网站页面通过验证后，点击插件按钮保存。

## 配置方式（.env）

所有参数统一通过项目根目录的 `.env` 文件配置：复制 `.env.example` 为 `.env` 后按需修改。两种部署方式共用同一文件——源码部署时服务启动自动加载（真实环境变量优先），Docker Compose 部署时由 compose 自动读取做变量替换。

通用默认体验只需配置 `NAV_TOKEN` 一项；代理、主机映射、内网白名单等属于「抓取增强」可选项——不配置时，服务端抓不到的站点（被墙、内网、需验证）改用浏览器采集器上报即可入库。

## 代理支持（HTTP / SOCKS5）

> 可选配置：无代理时解析失败的站点可用浏览器采集模式兜底。

服务请求目标网站时支持代理，读取标准环境变量（在 `.env` 中配置）：

- `HTTP_PROXY`
- `HTTPS_PROXY`
- `ALL_PROXY`
- `NO_PROXY`

抓取请求默认使用浏览器式 `User-Agent`，可通过 `NAV_USER_AGENT` 覆盖；`Accept-Language` 可通过 `NAV_ACCEPT_LANGUAGE` 覆盖。

### 示例

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

示例（`.env`）：

```bash
HTTP_PROXY=http://192.168.1.2:7890
HTTPS_PROXY=http://192.168.1.2:7890
NO_PROXY=127.0.0.1,localhost,::1,example.com,.example.com
NAV_HOST_ALIASES=*.example.com=192.168.1.10
NAV_ALLOWED_PRIVATE_NETWORKS=192.168.1.0/24
```

`NAV_ALLOWED_PRIVATE_NETWORKS` 是 SSRF 防护的内网白名单，**默认全禁内网**（最安全）。需要服务端直接抓取内网站点时才显式放行网段（如 `192.168.1.0/24,172.17.0.0/16`）；更推荐的做法是内网站点用浏览器采集器上报，白名单保持默认。

## Debian 13 Docker 部署

### 部署要求

1. Debian 13 已安装 Docker 与 Docker Compose 插件。
2. 服务器允许访问外网（需要抓取目标网站 HTML/icon）。
3. 服务器开放应用端口（默认 `8000`）。
4. 项目目录可写（用于 `data/` 和 `ICON/` 持久化）。
5. `Dockerfile` 基础镜像默认为官方 `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`；拉取官方镜像困难时，构建传入 `--build-arg BASE_IMAGE=<你的代理镜像地址>` 覆盖。

### Debian 13 安装 Docker（如未安装）

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin || sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
```

### 部署步骤（最简）

1. 获取项目并进入目录：

```bash
git clone <your-repo-url>
cd Nav_Loacl
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

4. 查看状态：

```bash
docker compose ps
docker compose logs -f
```

5. 验证访问：

- 页面：`http://<服务器IP>:8000/`
- 接口：`POST http://<服务器IP>:8000/api/site/parse`

### 常用维护命令

```bash
# 重启
docker compose restart

# 停止并删除容器
docker compose down

# 升级后重建
docker compose up -d --build
```

### 升级代码与镜像

是的，通常需要先 `git pull` 拉取最新代码，再重建并重启容器。

```bash
# 1) 进入项目目录
cd Nav_Loacl

# 2) 拉取最新代码
git pull

# 3) 重建并启动最新容器
docker compose up -d --build

# 4) 查看运行状态
docker compose ps
docker compose logs -f --tail=100
```

如果只是重启服务（代码和镜像都没变），可直接：

```bash
docker compose restart
```

## 源码部署（无 Docker）

适合不便使用 Docker 的服务器。项目无前端构建步骤，克隆后即可运行：

```bash
git clone <仓库地址> nav-local && cd nav-local
uv sync
cp .env.example .env   # 编辑 .env：至少设置 NAV_TOKEN
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

- 服务启动时自动加载项目根目录 `.env`（真实环境变量优先）。
- `--workers 1` 为硬约束（SQLite 单写入者）；Linux 下自动启用 uvloop / httptools，单实例足够个人使用。
- 生产环境建议用 systemd 托管（开机自启、崩溃自动拉起）：

```ini
# /etc/systemd/system/nav-local.service
[Unit]
Description=Nav Local Service
After=network-online.target

[Service]
WorkingDirectory=/opt/nav-local
ExecStart=/usr/local/bin/uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now nav-local
```

- 配置全部来自 `WorkingDirectory` 下的 `.env`，改配置只需编辑 `.env` 后 `systemctl restart nav-local`。
- 对外域名场景经 Nginx / Caddy 反向代理并启用 HTTPS；本服务对反代无特殊要求（Caddy 一行 `reverse_proxy 127.0.0.1:8000` 即可）。
- 更新：`git pull` 后 `systemctl restart nav-local`。
- 备份 / 迁移：只需 `data/`（数据库）与 `ICON/`（站点图标）两个目录；从 Docker 迁移时把挂载卷中的这两个目录拷入项目根即可。

## 运行策略说明

- `url` 冲突时执行 upsert 更新。
- 启动时自动创建 `data/`、`ICON/`、数据表。
- 启用 SSRF 防护，默认禁止本机/内网地址；需要抓取内网站点时在 `.env` 的 `NAV_ALLOWED_PRIVATE_NETWORKS` 显式放行（或改用浏览器采集器上报）。
- Docker 默认单实例运行（`workers=1`），适配 SQLite。

## 性能与缓存策略

- 响应启用 gzip 压缩（大于 1KB 的 HTML/JSON），首页传输体积约为原始大小的 1/6。
- 首页 `GET /` 支持 ETag / 304 协商缓存，内容未变化时刷新页面几乎零流量。
- 静态图标带浏览器缓存：`/ICON` 缓存 1 天，`/icons` 图标库缓存 7 天。更换站点图标后浏览器最多 1 天内仍显示旧图，Ctrl+F5 强制刷新可立即生效。
- 前端把站点和标签数据缓存在 localStorage（`nav_cache_v1:*`）：再次打开页面立即渲染本地数据，后台拉取最新数据后自动更新；清除浏览器站点数据即可重置本地缓存。
- Linux 容器内自动启用 uvloop / httptools（依赖 `uvicorn[standard]`）；Windows 开发环境自动跳过 uvloop，启动命令不变。

## 图标库版权

- `icons/` 目录的预设图标库来自字节跳动开源的 [IconPark](https://github.com/bytedance/IconPark)，以 [Apache License 2.0](icons/LICENSE) 授权使用与再分发（许可证全文见 `icons/LICENSE`）。
- 其中的品牌类图标（如支付宝、Adobe 系列等）图形以 Apache-2.0 授权，但商标权归各品牌方所有，本项目仅用于指示对应站点，不构成品牌背书。
- `ICON/` 目录存放的是各网站抓取的 favicon，仅用于指向其对应站点（与浏览器书签同类的指示性使用）。

## 许可证（License）

Copyright (c) 2026 luis

本项目以 [GNU Affero General Public License v3.0](LICENSE)（AGPL-3.0）开源：

- 您可以自由使用、修改与再分发本项目；
- 基于本项目修改后再分发，或**通过网络对外提供服务**（如部署为公开网站），必须以 AGPL-3.0 同等条款开放完整源代码，并保留原始版权声明与许可证文本，修改过的文件须标注改动说明；
- 若您基于本项目二次开发，欢迎（非强制）在您的 README 中注明来源并链接回本仓库。

第三方组件：`icons/` 图标库来自 IconPark（Apache-2.0，与 AGPL-3.0 兼容），归属声明见上文「图标库版权」章节。
