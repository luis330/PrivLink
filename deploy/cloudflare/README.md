# PrivLink Cloudflare 分支

TypeScript + Hono 实现，部署到 Cloudflare Workers。

## 快速开始

```bash
cd deploy/cloudflare
npm install
npx wrangler dev          # 本地开发
npx wrangler deploy       # 生产部署
```

## 首次初始化

> 通过 GitHub Actions 一键部署（仓库 `.github/workflows/deploy-cloudflare.yml`）会自动创建/查找 D1 与 R2 并填充 `wrangler.toml` 中的 `database_id` 占位符，无需手动操作。以下命令适用于本地/命令行部署：

```bash
npx wrangler login
npx wrangler d1 create privlink
npx wrangler r2 bucket create privlink-icons
npx wrangler r2 bucket create privlink-backgrounds
# 编辑 wrangler.toml 把空占位 database_id = "" 替换为上面创建返回的 uuid
npx wrangler secret put NAV_TOKEN
npx wrangler d1 execute privlink --file=migrations/001_init.sql
```

## 获取完整图标库

```bash
npm run icons:fetch
```

## 端点对齐检查

```bash
python3 ../../scripts/check-api-alignment.py
```
