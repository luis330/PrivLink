# Nav Local Service

一个基于 FastAPI 的网站记录采集服务。输入网页 URL 后，服务会解析网站名称和 icon，并写入本地 SQLite。

## 功能

- 前端页面：`GET /`
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

2. 启动服务：

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

3. 访问：

- 页面：`http://127.0.0.1:8000/`
- OpenAPI：`http://127.0.0.1:8000/docs`

## API

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

该接口默认禁用，必须设置环境变量 `NAV_INGEST_TOKEN`，并在请求头中携带相同 token：

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

## 浏览器采集模式

当目标网站需要 Cloudflare、登录态或浏览器验证时，推荐使用浏览器采集模式。它不会绕过验证码，也不会导出 cookie，只保存当前浏览器已经能正常打开的页面信息。

### 启用 token

本地 uv 运行示例：

```bash
export NAV_INGEST_TOKEN="your-secret-token"
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

Docker Compose 运行示例：

```bash
export NAV_INGEST_TOKEN="your-secret-token"
docker compose up -d --build
```

### Tampermonkey

1. 安装 Tampermonkey。
2. 新建脚本并使用 `collectors/nav-local.user.js` 的内容。
3. 在任意目标网站页面中打开 Tampermonkey 菜单：
   - 先执行“设置 Nav Local API 地址”，默认是 `http://127.0.0.1:8000`。
   - 再执行“设置 Nav Local Token”，填入 `NAV_INGEST_TOKEN`。
   - 通过验证并停留在目标页后，执行“保存当前页到 Nav Local”。

### Chrome / Edge 插件

1. 打开浏览器扩展管理页并启用开发者模式。
2. 选择“加载已解压的扩展程序”，目录选择 `browser-extension/`。
3. 打开插件“选项”，填写 API 地址和 `X-Nav-Token`。
4. 在目标网站页面通过验证后，点击插件按钮保存。

## 代理支持（HTTP / SOCKS5）

服务请求目标网站时支持代理，读取标准环境变量：

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

如果目标网站只能通过本机 hosts 或内网 DNS 解析，例如 `*.freeba.org` 指向 Caddy 反代，Docker 容器里也必须具备同样的解析结果。否则浏览器能访问，不代表服务端抓取进程能访问。

同时，内网站点必须放进 `NO_PROXY`，避免 HTTPS 请求被送到外部代理后出现 `SSL: UNEXPECTED_EOF_WHILE_READING`、代理解析失败或证书握手异常。

`NAV_HOST_ALIASES` 用于给应用进程配置主机解析映射，支持精确域名和 `*.example.com` 通配子域名。映射只改变 TCP 连接目标 IP，HTTP `Host` 和 HTTPS SNI 仍保留原始域名。

示例：

```yaml
services:
  nav-local:
    environment:
      - HTTP_PROXY=http://192.168.50.16:7890
      - HTTPS_PROXY=http://192.168.50.16:7890
      - NO_PROXY=127.0.0.1,localhost,::1,freeba.org,.freeba.org
      - NAV_HOST_ALIASES=*.freeba.org=192.168.50.15  # 改成 Caddy 的内网 IP
      - NAV_ALLOWED_PRIVATE_NETWORKS=192.168.50.0/24
      - NAV_INGEST_TOKEN=${NAV_INGEST_TOKEN:-}
```

`NAV_ALLOWED_PRIVATE_NETWORKS` 是 SSRF 防护的内网白名单。Caddy 如果不在 `192.168.50.0/24`，需要把实际网段加入这个变量，例如 `192.168.50.0/24,172.17.0.0/16`。

## Debian 13 Docker 部署

### 部署要求

1. Debian 13 已安装 Docker 与 Docker Compose 插件。
2. 服务器允许访问外网（需要抓取目标网站 HTML/icon）。
3. 服务器开放应用端口（默认 `8000`）。
4. 项目目录可写（用于 `data/` 和 `ICON/` 持久化）。
5. 当前 `Dockerfile` 基础镜像已改为私有代理地址：`docker.freeba.org/gfcr.ip/...`。

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

2. 启动服务：

```bash
docker compose up -d --build
```

当前仓库默认采用“写死代理地址”模式（不依赖外部环境变量）：

```yaml
environment:
  - HTTP_PROXY=http://192.168.50.16:7890
  - HTTPS_PROXY=http://192.168.50.16:7890
  - NO_PROXY=127.0.0.1,localhost,::1,freeba.org,.freeba.org
  - NAV_HOST_ALIASES=*.freeba.org=192.168.50.15
  - NAV_ALLOWED_PRIVATE_NETWORKS=192.168.50.0/24
```

3. 查看状态：

```bash
docker compose ps
docker compose logs -f
```

4. 验证访问：

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

## 运行策略说明

- `url` 冲突时执行 upsert 更新。
- 启动时自动创建 `data/`、`ICON/`、数据表。
- 启用 SSRF 防护，默认禁止本机/内网地址；放行 `192.168.50.0/24` 网段。
- Docker 默认单实例运行（`workers=1`），适配 SQLite。
