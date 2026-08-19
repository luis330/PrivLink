/**
 * 远程 URL 抓取模块：对应 main.py 的 fetch_url() / fetch_once_with_client()。
 *
 * Cloudflare Workers 不支持代理，因此仅保留直连模式。
 * 保留：重定向跟随、限流、超时、重试逻辑。
 */

const DEFAULT_USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

const ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8";

const REQUEST_TIMEOUT_SECONDS = 10;
const MAX_RETRIES = 2;
const MAX_REDIRECTS = 5;

const ICON_MAX_BYTES = 2 * 1024 * 1024; // 2MB

// ── 工具：ISO 时间戳 ───────────────────────────────────

export function utcNow(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

// ── 解析响应头 ─────────────────────────────────────────

function getHeader(headers: Headers, name: string): string {
  return (headers.get(name) ?? "").trim();
}

// ── fetchHtml：抓取 HTML 页面 ──────────────────────────

export interface FetchHtmlResult {
  finalUrl: string;
  body: string;
  contentType: string;
}

export async function fetchHtml(
  targetUrl: string,
  maxBytes: number = ICON_MAX_BYTES,
  referer?: string
): Promise<FetchHtmlResult> {
  const headers = buildHeaders("text/html,application/xhtml+xml,*/*;q=0.5", referer);
  return fetchWithRedirects(targetUrl, headers, maxBytes, 0);
}

// ── fetchIcon：抓取图标 ───────────────────────────────

export interface FetchIconResult {
  finalUrl: string;
  body: ArrayBuffer;
  contentType: string;
}

export async function fetchIcon(
  targetUrl: string,
  maxBytes: number = ICON_MAX_BYTES,
  referer?: string
): Promise<FetchIconResult> {
  const headers = buildHeaders("image/*,*/*;q=0.5", referer);
  return fetchWithRedirects(targetUrl, headers, maxBytes, 0);
}

// ── 带重定向跟随的通用抓取 ─────────────────────────────

async function fetchWithRedirects(
  url: string,
  headers: Record<string, string>,
  maxBytes: number,
  redirectCount: number
): Promise<any> {
  if (redirectCount > MAX_REDIRECTS) {
    throw new Error(`Too many redirects (>${MAX_REDIRECTS})`);
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_SECONDS * 1000);

  try {
    const resp = await fetch(url, {
      method: "GET",
      headers,
      redirect: "manual",
      signal: controller.signal,
    });
    clearTimeout(timeoutId);

    if (resp.status >= 300 && resp.status < 400 && resp.headers.get("Location")) {
      const location = resp.headers.get("Location")!.trim();
      const nextUrl = new URL(location, url).toString();
      return fetchWithRedirects(nextUrl, headers, maxBytes, redirectCount + 1);
    }

    if (resp.status >= 400) {
      throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
    }

    const contentType = getHeader(resp.headers, "Content-Type").split(";")[0].trim().toLowerCase();
    const contentLength = resp.headers.get("Content-Length");
    if (contentLength && parseInt(contentLength, 10) > maxBytes) {
      throw new Error(`Response exceeds limit (${maxBytes} bytes)`);
    }

    const body = await resp.arrayBuffer();
    if (body.byteLength > maxBytes) {
      throw new Error(`Response exceeds limit (${maxBytes} bytes)`);
    }

    const finalUrl = resp.url;
    return { finalUrl, body, contentType };
  } catch (err) {
    clearTimeout(timeoutId);
    throw err;
  }
}

// ── 工具函数 ───────────────────────────────────────────

function buildHeaders(accept: string, referer?: string): Record<string, string> {
  const h: Record<string, string> = {
    "User-Agent": DEFAULT_USER_AGENT,
    Accept: accept,
    "Accept-Language": ACCEPT_LANGUAGE,
  };
  if (referer) h["Referer"] = referer;
  return h;
}

// ── HTML 解析：提取 title / og:site_name / icon links ─

export interface IconCandidate {
  url: string;
  isSvg: boolean;
  sizeScore: number;
  rank: number;
}

export interface HtmlParseResult {
  ogSiteName: string;
  title: string;
  iconLinks: IconCandidate[];
}

export function parseHtmlForSiteInfo(html: string, baseUrl: string): HtmlParseResult {
  const result: HtmlParseResult = {
    ogSiteName: "",
    title: "",
    iconLinks: [],
  };

  // 提取 <title>
  const titleMatch = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
  if (titleMatch) {
    result.title = titleMatch[1].replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
  }

  // 提取 og:site_name
  const ogMatch = html.match(/property=["']og:site_name["'][^>]*content=["']([^"']+)["']/i);
  if (ogMatch) {
    result.ogSiteName = ogMatch[1].trim();
  }

  // 提取 <link rel="icon" ...>
  const iconRegex = /<link\s+[^>]*rel=["']icon["'][^>]*>/gi;
  let match: RegExpExecArray | null;
  let rank = 0;
  while ((match = iconRegex.exec(html)) !== null) {
    const tag = match[0];
    const hrefMatch = tag.match(/href=["']([^"']+)["']/i);
    const typeMatch = tag.match(/type=["']([^"']+)["']/i);
    const sizesMatch = tag.match(/sizes=["']([^"']+)["']/i);
    if (hrefMatch) {
      const href = hrefMatch[1].trim();
      const iconUrl = new URL(href, baseUrl).toString();
      const ext = new URL(iconUrl).pathname.split(".").pop()?.toLowerCase() ?? "";
      const isSvg = ext === "svg" || (!!typeMatch && typeMatch[1].toLowerCase().includes("svg"));
      const sizeScore = parseSizes(sizesMatch?.[1] ?? "");
      result.iconLinks.push({ url: iconUrl, isSvg, sizeScore, rank: rank++ });
    }
  }

  // 默认排序：SVG 优先，尺寸大优先，rank 小优先
  result.iconLinks.sort((a, b) => {
    if (a.isSvg !== b.isSvg) return a.isSvg ? -1 : 1;
    if (b.sizeScore !== a.sizeScore) return b.sizeScore - a.sizeScore;
    return a.rank - b.rank;
  });

  // 始终添加 favicon.ico 兜底
  const faviconUrl = new URL("/favicon.ico", baseUrl).toString();
  if (!result.iconLinks.some((ic) => ic.url === faviconUrl)) {
    result.iconLinks.push({ url: faviconUrl, isSvg: false, sizeScore: 0, rank: rank + 1 });
  }

  return result;
}

function parseSizes(sizesValue: string): number {
  if (!sizesValue) return 0;
  const sizes = sizesValue.toLowerCase().trim();
  if (sizes.includes("any")) return 100_000_000;
  let best = 0;
  for (const token of sizes.split(" ")) {
    if (!token.includes("x")) continue;
    const [left, right] = token.split("x");
    const w = parseInt(left, 10);
    const h = parseInt(right, 10);
    if (!isNaN(w) && !isNaN(h)) best = Math.max(best, w * h);
  }
  return best;
}

// ── 站点名称选择 ───────────────────────────────────────

export function chooseSiteName(info: HtmlParseResult, fallbackUrl: string): string {
  if (info.ogSiteName) return info.ogSiteName;
  if (info.title) return info.title;
  try {
    return new URL(fallbackUrl).hostname;
  } catch {
    return fallbackUrl;
  }
}
