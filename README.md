# Nav Local Service

一个基于 FastAPI 的网站记录采集服务。输入网页 URL 后，服务会解析网站名称和 icon，并写入本地 SQLite。

## 功能

- 前端页面：`GET /`
- 解析接口：`POST /api/site/parse`
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

## 代理支持（HTTP / SOCKS5）

服务请求目标网站时支持代理，读取标准环境变量：

- `HTTP_PROXY`
- `HTTPS_PROXY`
- `ALL_PROXY`
- `NO_PROXY`

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
  - NO_PROXY=127.0.0.1,localhost,::1
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
- 启用 SSRF 防护，默认禁止本机/内网地址。
- Docker 默认单实例运行（`workers=1`），适配 SQLite。
