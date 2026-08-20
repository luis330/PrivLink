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
import app, { isAllowedScheme, normalizeUrl } from "../src/index";

describe("PrivLink Cloudflare - URL scheme 校验", () => {
  // 回归：URL.protocol 返回 "https:"（带冒号），曾被直接拿去比对
  // ["http", "https"]，导致所有合法 URL 都被判为 invalid，
  // 添加网站功能在 Cloudflare 端完全不可用。
  it("接受 http / https（protocol 带尾随冒号）", () => {
    expect(isAllowedScheme(new URL("https://www.baidu.com"))).toBe(true);
    expect(isAllowedScheme(new URL("http://example.com/path"))).toBe(true);
    expect(isAllowedScheme(new URL("HTTPS://EXAMPLE.COM"))).toBe(true);
  });

  it("拒绝非 http/https", () => {
    expect(isAllowedScheme(new URL("ftp://example.com"))).toBe(false);
    expect(isAllowedScheme(new URL("file:///etc/passwd"))).toBe(false);
    expect(isAllowedScheme(new URL("javascript:alert(1)"))).toBe(false);
    expect(isAllowedScheme(new URL("data:text/html,hi"))).toBe(false);
  });
});

describe("PrivLink Cloudflare - URL 规范化与 Python 端对齐", () => {
  // 期望值直接取自 Python 端 main.normalize_url() 的实际输出。
  // 两端必须逐字符一致，否则同一网址会在 sites.url（UNIQUE）下
  // 变成两条记录，upsert 失效。
  const cases: Array<[string, string]> = [
    ["https://www.baidu.com", "https://www.baidu.com"],
    ["https://www.baidu.com/", "https://www.baidu.com"],
    ["https://EXAMPLE.com/A/B/", "https://example.com/A/B"],
    ["https://example.com/path?q=1#frag", "https://example.com/path?q=1"],
    ["http://example.com//", "http://example.com"],
    ["https://example.com:8443/x/", "https://example.com:8443/x"],
    ["https://user:pass@example.com/a/", "https://user:pass@example.com/a"],
    ["https://example.com?a=1", "https://example.com?a=1"],
  ];

  for (const [input, expected] of cases) {
    it(`${input} -> ${expected}`, () => {
      expect(normalizeUrl(new URL(input))).toBe(expected);
    });
  }
});

describe("PrivLink Cloudflare - API 结构测试", () => {
  // Hono 的 app.request 不传 env 时 c.env 为 undefined，访问绑定会抛错。
  // 这里给一个最小 D1 桩：查询一律返回空集，足以验证响应结构。
  const stubDb = {
    prepare: () => ({
      bind() {
        return this;
      },
      all: async () => ({ results: [] }),
      first: async () => null,
      run: async () => ({ success: true }),
    }),
  };
  const env = { DB: stubDb, NAV_TOKEN: "", NAV_MODE: "single" } as never;

  it("GET /api/auth/status 返回正确结构", async () => {
    const res = await app.request("/api/auth/status", undefined, env);
    expect(res.status).toBe(200);
    const body = await res.json() as Record<string, unknown>;
    expect(body).toHaveProperty("token_required");
    expect(body).toHaveProperty("authorized");
    expect(typeof body.token_required).toBe("boolean");
    expect(typeof body.authorized).toBe("boolean");
  });

  it("GET /api/sites 返回数组", async () => {
    const res = await app.request("/api/sites", undefined, env);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(Array.isArray(body)).toBe(true);
  });

  it("GET /api/tags 返回数组", async () => {
    const res = await app.request("/api/tags", undefined, env);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(Array.isArray(body)).toBe(true);
  });

  it("GET /api/appearance/background 返回默认设置", async () => {
    const res = await app.request("/api/appearance/background", undefined, env);
    expect(res.status).toBe(200);
    const body = await res.json() as Record<string, unknown>;
    expect(body.type).toBe("default");
    expect(body.color).toBe("");
    expect(body.image).toBe("");
    expect(body.image_url).toBe("");
  });

  // 回归：icon_rel_path 曾只存裸文件名（"abc.svg"），前端 toIconUrl 拼成
  // "/abc.svg"，而代理路由只认 "/ICON/*"，图标一律 404。
  // icon_rel_path 与 R2 key 必须是同一个字符串——deleteObject 也直接拿它当 key。
  it("GET /ICON/* 用带前缀的 key 取对象", async () => {
    let requestedKey = "";
    const bucket = {
      get: async (k: string) => {
        requestedKey = k;
        // R2 对象契约：getObject 会调用 arrayBuffer() 与 size
        return { arrayBuffer: async () => new ArrayBuffer(0), size: 0 };
      },
    };
    const res = await app.request("/ICON/abc123.svg", undefined, {
      ...(env as object),
      ICON_BUCKET: bucket,
    } as never);
    expect(res.status).toBe(200);
    expect(requestedKey).toBe("ICON/abc123.svg");
    // 缺 Content-Type 时浏览器不会在 <img> 中渲染，SVG 尤其严格
    expect(res.headers.get("Content-Type")).toBe("image/svg+xml");
  });

  // 回归：读取路由曾用 slice(11) 得到 "/bg.png"，而上传端点写入的 key 是
  // "background/bg.png"，两者对不上，背景图上传后永远取不到。
  it("GET /background/* 用带前缀的 key 取对象", async () => {
    let requestedKey = "";
    const bucket = {
      get: async (k: string) => {
        requestedKey = k;
        // R2 对象契约：getObject 会调用 arrayBuffer() 与 size
        return { arrayBuffer: async () => new ArrayBuffer(0), size: 0 };
      },
    };
    const res = await app.request("/background/bg.png", undefined, {
      ...(env as object),
      BACKGROUND_BUCKET: bucket,
    } as never);
    expect(res.status).toBe(200);
    expect(requestedKey).toBe("background/bg.png");
    expect(res.headers.get("Content-Type")).toBe("image/png");
  });
});
