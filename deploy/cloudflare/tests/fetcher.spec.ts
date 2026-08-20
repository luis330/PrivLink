/**
 * fetcher 模块测试：HTML 字节解码与解析。
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { decodeHtml, fetchHtml, parseHtmlForSiteInfo } from "../src/fetcher";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("decodeHtml - 与 Python decode_html 对齐", () => {
  it("解码 UTF-8", () => {
    const bytes = new TextEncoder().encode("<title>百度一下，你就知道</title>");
    expect(decodeHtml(bytes.buffer as ArrayBuffer)).toContain("百度一下，你就知道");
  });

  it("非 UTF-8 字节回退到 gb18030，不抛错", () => {
    // GBK 编码的“百度”：0xB0 0xD9 0xB6 0xC8（在 UTF-8 下是非法序列）
    const gbk = new Uint8Array([0xb0, 0xd9, 0xb6, 0xc8]);
    const out = decodeHtml(gbk.buffer as ArrayBuffer);
    expect(typeof out).toBe("string");
    expect(out).toBe("百度");
  });

  it("空内容不抛错", () => {
    expect(decodeHtml(new ArrayBuffer(0))).toBe("");
  });
});

describe("fetchHtml - 返回值类型回归", () => {
  // 回归：fetchWithRedirects 返回 ArrayBuffer，但 FetchHtmlResult.body
  // 声明为 string，且函数返回类型写成 Promise<any> 掩盖了不一致。
  // 结果 parseHtmlForSiteInfo 收到 ArrayBuffer，线上报
  // "Internal server error: html.match is not a function"。
  it("body 是字符串，可直接交给 parseHtmlForSiteInfo", async () => {
    const html =
      "<html><head><title>测试站点</title>" +
      '<link rel="icon" href="/fav.svg">' +
      "</head><body>hi</body></html>";
    vi.stubGlobal(
      "fetch",
      async () =>
        new Response(new TextEncoder().encode(html), {
          status: 200,
          headers: { "Content-Type": "text/html; charset=utf-8" },
        })
    );

    const res = await fetchHtml("https://example.com");
    expect(typeof res.body).toBe("string");

    const info = parseHtmlForSiteInfo(res.body, "https://example.com");
    expect(info.title).toBe("测试站点");
    expect(info.iconLinks.some((i) => i.url.endsWith("/fav.svg"))).toBe(true);
  });
});

describe("parseHtmlForSiteInfo", () => {
  const base = "https://example.com";

  it("优先取 og:site_name，其次 title", () => {
    const withOg =
      '<head><meta property="og:site_name" content="站点名"><title>页面标题</title></head>';
    expect(parseHtmlForSiteInfo(withOg, base).ogSiteName).toBe("站点名");
    expect(parseHtmlForSiteInfo(withOg, base).title).toBe("页面标题");
  });

  it("无图标声明时兜底 /favicon.ico", () => {
    const info = parseHtmlForSiteInfo("<head><title>x</title></head>", base);
    expect(info.iconLinks.map((i) => i.url)).toContain(
      "https://example.com/favicon.ico"
    );
  });
});
