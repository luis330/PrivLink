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

```bash
npx wrangler login
npx wrangler d1 create privlink
npx wrangler r2 bucket create privlink-icons
npx wrangler r2 bucket create privlink-backgrounds
# 编辑 wrangler.toml 填入 database_id
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
