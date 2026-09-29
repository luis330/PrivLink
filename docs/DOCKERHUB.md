# PrivLink — 轻量自部署书签导航站

输入网址，自动解析站名与 favicon，卡片式展示、标签筛选、拖拽排序；支持公开/私有站点与访客只读浏览。基于 FastAPI + SQLite，单容器运行，数据全部落在三个挂载目录。

- GitHub：https://github.com/luis330/PrivLink
- 完整部署文档（systemd、代理、内网抓取、API）：https://github.com/luis330/PrivLink/blob/main/docs/DEPLOYMENT.md
- Cloudflare Workers 免服务器部署：https://github.com/luis330/PrivLink/blob/main/docs/CLOUDFLARE-DEPLOYMENT.md

## 快速开始（Docker Compose，推荐）

```bash
git clone https://github.com/luis330/PrivLink privlink && cd privlink
cp .env.example .env    # 编辑 .env：公网部署务必设置 NAV_TOKEN
docker compose pull && docker compose up -d
```

打开 `http://127.0.0.1:8000/` 即可使用。

不想克隆仓库时，最小 `docker run`：

```bash
mkdir privlink && cd privlink
docker run -d --name privlink -p 8000:8000 \
  -e NAV_TOKEN=<你的token> \
  -v "$PWD/data":/app/data -v "$PWD/ICON":/app/ICON -v "$PWD/background":/app/background \
  luis330/privlink:latest
```

## 标签（Tags）说明

| 标签 | 含义 | 适用 |
|---|---|---|
| `latest` | 跟随 main 分支最新自动构建 | 想始终用最新版 |
| `0.1.0` | 具体版本号（与仓库 pyproject 的 version 对齐） | 生产锁定 |
| `0.1` / `0` | 同一版本的次级 / 主级别名 | 宽松锁定 |

生产环境建议锁定具体版本号；`latest` 随 main 更新，可能与最新发布版有偏差。锁定方式：compose 用户的 `.env` 中设置 `DOCKER_IMAGE=luis330/privlink:0.1.0`。

## 环境变量

通过 `.env`（Docker Compose 自动读取）或 `docker run -e` 传入：

| 变量 | 默认 | 说明 |
|---|---|---|
| `NAV_TOKEN` | 空 | 访问 token。空 = 开放模式（无鉴权，浏览器采集接口禁用）；非空 = 门禁模式，全部管理接口需请求头 `X-Nav-Token`。建议 32 位以上随机串（`openssl rand -hex 24`） |
| `NAV_MODE` | `single` | single（单用户）；multi 为未来预留值 |
| `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` | 空 | 服务端抓取目标网站时使用的标准代理变量 |
| `NAV_HOST_ALIASES` | 空 | 主机解析映射（如 `*.example.com=192.168.1.10`），仅改 TCP 目标 IP、保留 Host/SNI，NAT 回环 / 内网直连场景用 |
| `NAV_ALLOWED_PRIVATE_NETWORKS` | 空（全禁内网） | SSRF 防护白名单，允许服务端抓取的内网网段（如 `192.168.1.0/24`） |
| `NAV_USER_AGENT` / `NAV_ACCEPT_LANGUAGE` | 内置默认 | 抓取目标网站时的 UA 与语言头 |
| `DOCKER_IMAGE`（仅 compose） | `luis330/privlink:latest` | 实际使用的镜像名；锁定版本或走自建镜像源时设置 |

## 端口与数据卷

- 端口：`8000`（HTTP）
- 挂载卷（备份迁移只需这三个目录）：
  - `/app/data` — SQLite 数据库（`sites.db`）
  - `/app/ICON` — 站点图标
  - `/app/background` — 背景图片

## 许可证

AGPL-3.0。内置图标库数据来自 [Simple Icons](https://github.com/simple-icons/simple-icons)（CC0-1.0），品牌图标商标权归各品牌方所有。
