/**
 * 参数绑定测试：用严格 stub 模拟 D1 对「占位符数量 == bind 参数数量」的校验。
 *
 * 真实 D1 在数量不匹配时抛 D1_ERROR: Wrong number of parameter bindings，
 * 而宽松的手写 stub 会默默放行，因此这类缺陷只有显式校验才能在测试中暴露。
 */

import { describe, it, expect } from "vitest";
import app, { normalizeTagList } from "../src/index";

/** 按 SQL 中 "?" 的个数校验 bind 参数数量的 D1 stub */
function strictDb(row: Record<string, unknown> | null = null) {
  return {
    prepare(sql: string) {
      const expected = (sql.match(/\?/g) ?? []).length;
      const stmt = {
        bind(...args: unknown[]) {
          if (args.length !== expected) {
            throw new Error(
              `D1_ERROR: Wrong number of parameter bindings: ` +
                `SQL expects ${expected}, got ${args.length}`
            );
          }
          return stmt;
        },
        all: async () => ({ results: row ? [row] : [] }),
        first: async () => row,
        run: async () => ({ success: true }),
      };
      return stmt;
    },
  };
}

const siteRow = {
  id: 1,
  url: "https://example.com",
  site_name: "示例",
  icon_rel_path: "",
  icon_source_url: "",
  updated_at: "2026-08-20T00:00:00Z",
  sort_order: 0,
  is_public: 1,
};

function envWith(db: unknown) {
  return { DB: db, NAV_TOKEN: "", NAV_MODE: "single" } as never;
}

describe("PUT /api/sites/:id — 参数绑定", () => {
  it("不带 icon_file 时绑定数量与 SQL 一致", async () => {
    const res = await app.request(
      "/api/sites/1",
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          site_name: "新名字",
          url: "https://example.com",
        }),
      },
      envWith(strictDb(siteRow))
    );
    expect(res.status).toBe(200);
  });

  it("带 icon_file 时绑定数量与 SQL 一致", async () => {
    const res = await app.request(
      "/api/sites/1",
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          site_name: "新名字",
          url: "https://example.com",
          icon_file: "github",
        }),
      },
      envWith(strictDb(siteRow))
    );
    expect(res.status).toBe(200);
  });
});

describe("PUT /api/sites/:id — 输入校验（对齐 Python 端）", () => {
  function putBody(extra: Record<string, unknown>) {
    return {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        site_name: "名称",
        url: "https://example.com",
        ...extra,
      }),
    };
  }

  it("icon_file 为未知 slug 时 400", async () => {
    const res = await app.request(
      "/api/sites/1",
      putBody({ icon_file: "definitely-not-a-real-slug-xyz" }),
      envWith(strictDb(siteRow))
    );
    expect(res.status).toBe(400);
    expect((await res.json() as any).error).toBe("图标不存在");
  });

  it("icon_file 含路径分隔符时 400", async () => {
    const res = await app.request(
      "/api/sites/1",
      putBody({ icon_file: "../etc/passwd" }),
      envWith(strictDb(siteRow))
    );
    expect(res.status).toBe(400);
    expect((await res.json() as any).error).toBe("无效的图标文件名");
  });

  it("非 http/https 的 url 被拒", async () => {
    const res = await app.request(
      "/api/sites/1",
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ site_name: "x", url: "ftp://example.com" }),
      },
      envWith(strictDb(siteRow))
    );
    expect(res.status).toBe(400);
  });

  it("超长标签返回 400 而非写入数据库", async () => {
    const res = await app.request(
      "/api/sites/1",
      putBody({ tags: ["x".repeat(21)] }),
      envWith(strictDb(siteRow))
    );
    expect(res.status).toBe(400);
    expect((await res.json() as any).error).toContain("20");
  });
});

describe("normalizeTagList — 与 Python normalize_tag_list 对齐", () => {
  // 期望值取自 Python 端实际输出
  it("折叠空白、丢弃空值、按小写去重并保留首次写法", () => {
    expect(normalizeTagList(["  a  b ", "", "  ", "A B"])).toEqual(["a b"]);
    expect(normalizeTagList(["Python", "python", "PYTHON"])).toEqual(["Python"]);
    expect(normalizeTagList(["tag1", "tag2"])).toEqual(["tag1", "tag2"]);
  });

  it("空输入返回空数组", () => {
    expect(normalizeTagList(undefined)).toEqual([]);
    expect(normalizeTagList(null)).toEqual([]);
    expect(normalizeTagList([])).toEqual([]);
  });

  it("长度上限为 20：20 通过，21 抛错", () => {
    expect(normalizeTagList(["x".repeat(20)])).toEqual(["x".repeat(20)]);
    expect(() => normalizeTagList(["x".repeat(21)])).toThrow(/20/);
  });
});
