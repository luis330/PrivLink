/**
 * 对齐测试：断言 TS 端关键端点的响应结构。
 *
 * 运行（需 wrangler）：
 *   npx wrangler test deploy/cloudflare/tests/api.spec.ts
 *
 * 注：wrangler test 使用 miniflare（本地模拟 Workers 环境），
 * 不支持 D1/R2 真实绑定，此处仅测试无 DB 依赖的路由。
 */

import { describe, it, expect } from "vitest";
import app from "../src/index";

describe("PrivLink Cloudflare - API 结构测试", () => {
  it("GET /api/auth/status 返回正确结构", async () => {
    const res = await app.request("/api/auth/status");
    expect(res.status).toBe(200);
    const body = await res.json() as Record<string, unknown>;
    expect(body).toHaveProperty("token_required");
    expect(body).toHaveProperty("authorized");
    expect(typeof body.token_required).toBe("boolean");
    expect(typeof body.authorized).toBe("boolean");
  });

  it("GET /api/sites 返回数组", async () => {
    const res = await app.request("/api/sites");
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(Array.isArray(body)).toBe(true);
  });

  it("GET /api/tags 返回数组", async () => {
    const res = await app.request("/api/tags");
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(Array.isArray(body)).toBe(true);
  });

  it("GET /api/appearance/background 返回默认设置", async () => {
    const res = await app.request("/api/appearance/background");
    expect(res.status).toBe(200);
    const body = await res.json() as Record<string, unknown>;
    expect(body.type).toBe("default");
    expect(body.color).toBe("");
    expect(body.image).toBe("");
    expect(body.image_url).toBe("");
  });
});
