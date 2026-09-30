# 公网部署安全运维清单

应用层加固已内置（安全响应头、CSP、CORS 收敛、鉴权限速、SVG 内容检测、导出收口、异常脱敏），本清单覆盖**部署边界侧**需要人工在 Cloudflare / DNS / 流程上完成的动作。带 ⏱ 的项为一次性配置，其余为周期性例行。

## 1. Cloudflare 区域设置（⏱ 一次性）

### 1.1 HSTS 提升到 1 年

应用层**有意不下发 HSTS**（避免与应用/`_headers` 重复下发导致响应双头），由 Cloudflare 区域设置统一下发：

- 路径：Cloudflare Dashboard → 对应区域 → SSL/TLS → Edge Certificates → Strict Transport Security (HSTS)。
- 现状为 `max-age=15552000`（180 天），改为 **max-age=31536000（1 年）+ includeSubDomains**。
- `preload` 可选：提交 hstspreload.org 后撤回困难，确认全站长期 HTTPS 再开启。

### 1.2 WAF 自定义规则拦 API 文档路径（双保险）

应用已默认关闭 `/docs`、`/redoc`、`/openapi.json`（404），WAF 规则作为第二层：

- 路径：Security → WAF → Custom rules。
- 表达式：`(http.request.uri.path in {"/docs" "/redoc" "/openapi.json"})` → 动作 **Block**。

### 1.3 Rate Limiting 规则（权威限速层）

应用内建限速在 Workers 上是 **per-isolate 内存计数**（尽力而为），公网部署的权威限速配置在边缘：

- 路径：Security → WAF → Rate limiting rules。
- 规则示例：匹配 `http.request.uri.path wildcard "/api/*"` 且响应状态码 `401`，同 IP 每分钟超过 **10 次**即 **Block 15 分钟**（与应用内 `NAV_AUTH_FAIL_*` 默认值对齐）。

### 1.4 Bot 防护定位（认知项，无需配置）

Cloudflare 托管 Bot 防护仅基于 UA/指纹特征，可被简单伪造绕过（2026-09-30 评估实测：补齐常规浏览器头即放行）。它是第一道滤网而非边界，真正的防线是 1.2/1.3 与应用层加固。

## 2. DNS / 邮件（⏱ 一次性）

### 2.1 DMARC 记录

若 `_dmarc.<你的域名>` 缺失（NXDOMAIN），子域可被用于伪造发件人。MX 已指向 Cloudflare Email Routing 时，添加 TXT 记录：

```
名称：_dmarc.<你的域名>
值：v=DMARC1; p=none; rua=mailto:dmarc@<你的域名>
```

先 `p=none` 观察两周聚合报告，确认无合法流被误伤后逐步收紧到 `p=quarantine` / `p=reject`（建议 `adkim=s; aspf=s`）。

> 个人域名等私密配置只记录在本地 `docs/agents/`（已 gitignore，不入库），本文件一律使用占位符。

## 3. 凭证与账号体系

- **NAV_TOKEN 季度轮换**：应用为单一静态共享 token，无过期机制；每季度（或怀疑泄露时）在 Cloudflare Secrets（`wrangler secret put NAV_TOKEN`）/ `.env` 更新，并同步浏览器与采集器端。启动日志对长度 < 32 的 token 有告警。
- **中期方向**（可选）：接入 Cloudflare Zero Trust Access，把单 token 升级为身份 + 设备策略；或应用层改 httpOnly Cookie 会话（涉及前端鉴权流重构，见代码仓库二期计划）。

## 4. 依赖与供应链（季度例行）

- SBOM / CVE 核对：`uvx pip-audit`（或 `uvx cyclonedx-py` 生成 SBOM 后核对），检查 FastAPI / Starlette / httpx / uvicorn 已知漏洞。
- 关注 FastAPI/Starlette 安全公告；`pylock.toml`（Workers 锁定文件）与 `uv.lock` 随依赖升级同步更新。

## 5. 备查：应用层已内置的加固（勿重复配置）

| 项 | 实现位置 | 说明 |
|---|---|---|
| 安全响应头（CSP / nosniff / XFO / RP / PP） | `app.py` SecurityHeadersMiddleware + 仓库根 `_headers` | HSTS 除外（见 1.1） |
| API 文档关闭 | `config.py` `NAV_ENABLE_DOCS`（默认关） | `/docs` 等返回 404 |
| CORS 仅同源 | `app.py` CorsGuard + `NAV_CORS_ORIGINS` | 白名单默认空 |
| 鉴权失败限速 | `app.py` TokenGuard + `NAV_AUTH_FAIL_*` | Workers 上为尽力而为，权威层见 1.3 |
| SVG 沙箱 | `app.py` 媒体路由 + `icons.py` 内容检测 | 上传/抓取双检测 |
| 导出收口 | `app.py` `export_sites` | 开放模式 403 |
| 500 脱敏 | `app.py` generic_exception_handler | 不回显异常细节 |
