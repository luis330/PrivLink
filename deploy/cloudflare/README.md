# PrivLink Cloudflare 分支

TypeScript + Hono 实现，部署到 Cloudflare Workers。

## 快速开始

```bash
cd deploy/cloudflare
npm install
npx wrangler dev          # 本地开发（miniflare，使用本地 D1/R2，不碰线上数据）
npx wrangler deploy       # 生产部署
```

> `wrangler dev` 用的是本地模拟的 D1，首次需要先建本地表：
> `npx wrangler d1 execute privlink --local --file=migrations/001_init.sql`
> （`--local` 作用于本地库，`--remote` 才是线上库。）

## 开发校验

```bash
npm run typecheck    # tsc --noEmit
npm test             # vitest：端点结构、URL 规范化、R2 key、参数绑定
```

改动 `src/` 后这两条都应通过。测试的作用是锁住与 Python 端易漂移的约定，
详见 [docs/CLOUDFLARE-DEPLOYMENT.md](../../docs/CLOUDFLARE-DEPLOYMENT.md) 第 4.3 节。

## 首次初始化

> GitHub Actions workflow（`.github/workflows/deploy-cloudflare.yml`）**只执行部署**，不会创建 D1/R2 资源、不会执行迁移、也不会填充 `wrangler.toml` 中的 `database_id`。以下步骤在首次部署前必须手动执行一次：

```bash
npx wrangler login
npx wrangler d1 create privlink          # 记下返回的 database_id
npx wrangler r2 bucket create privlink-icons
npx wrangler r2 bucket create privlink-backgrounds
# 编辑 wrangler.toml 把空占位 database_id = "" 替换为上面创建返回的 uuid
npx wrangler d1 execute privlink --remote --file=migrations/001_init.sql
npx wrangler secret put NAV_TOKEN        # 可选，配置门禁
```

完成后，push 到 `main` 即可由 Actions 自动部署（`NAV_TOKEN` 也可改由仓库 Secret 提供，见根 README）。

## 获取完整图标库

```bash
npm run icons:fetch
```

## 端点对齐检查

```bash
python3 ../../scripts/check-api-alignment.py
```
