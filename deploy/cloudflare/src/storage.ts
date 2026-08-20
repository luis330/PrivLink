/**
 * R2 存储抽象层：替代 main.py 中的 Path IO。
 */

// ── MIME 推断 ──────────────────────────────────────────

const MIME_BY_EXT: Record<string, string> = {
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".gif": "image/gif",
  ".bmp": "image/bmp",
  ".avif": "image/avif",
};

/**
 * 按 key 的扩展名推断 Content-Type。
 *
 * 必须显式回给浏览器：`<img>` 加载缺少 MIME 的资源时不会渲染，
 * SVG 尤其严格。Python 端由 StaticFiles 按扩展名自动处理，
 * Workers 侧的 Response 需要自己带上。
 */
export function contentTypeForKey(key: string): string {
  const i = key.lastIndexOf(".");
  const ext = i >= 0 ? key.slice(i).toLowerCase() : "";
  return MIME_BY_EXT[ext] ?? "application/octet-stream";
}

// ── 核心操作 ───────────────────────────────────────────

export async function putObject(
  bucket: unknown,
  key: string,
  body: ArrayBuffer | Uint8Array
): Promise<void> {
  await (bucket as any).put(key, body, {
    // R2 的 httpMetadata 用驼峰键；此处曾误写 "cache-control"，该项一直未生效
    httpMetadata: {
      contentType: contentTypeForKey(key),
      cacheControl: "public, max-age=86400",
    },
  });
}

export async function getObject(
  bucket: unknown,
  key: string
): Promise<{ body: ArrayBuffer; size: number } | null> {
  const obj = await (bucket as any).get(key);
  if (!obj) return null;
  return { body: await obj.arrayBuffer(), size: obj.size };
}

export async function deleteObject(bucket: unknown, key: string): Promise<void> {
  await (bucket as any).delete(key);
}

export async function listObjects(
  bucket: unknown,
  prefix?: string
): Promise<Array<{ key: string; size: number; uploaded: Date }>> {
  const result = await (bucket as any).list({ prefix: prefix ?? "" });
  return result.objects
    .map((o: any) => ({ key: o.key, size: o.size, uploaded: o.uploaded }))
    .sort((a: any, b: any) => b.uploaded.getTime() - a.uploaded.getTime());
}

// ── 背景图工具 ──────────────────────────────────────────

const BACKGROUND_FILENAME_RE = /^bg-[0-9a-f]{24}\.(?:jpg|jpeg|png|webp)$/;

export function isValidBackgroundFilename(filename: string): boolean {
  return BACKGROUND_FILENAME_RE.test(filename);
}

export function backgroundImageUrl(filename: string): string {
  return `/background/${filename}`;
}

// ── 图标文件名工具（异步 SHA-256）───────────────────────

export async function iconUploadFilename(
  content: Uint8Array,
  originalName: string
): Promise<string> {
  const ext = extFromFilename(originalName);
  const digest = await sha256Hex(content);
  return `upload-${digest.slice(0, 24)}${ext}`;
}

export async function iconFilenameFromUrl(
  normalizedUrl: string,
  extension: string
): Promise<string> {
  const digest = await sha256Hex(new TextEncoder().encode(normalizedUrl));
  return `${digest.slice(0, 24)}${extension}`;
}

function extFromFilename(name: string): string {
  const parts = name.split(".");
  const ext = parts.length > 1 ? `.${parts[parts.length - 1].toLowerCase()}` : "";
  return ext || ".ico";
}

async function sha256Hex(data: Uint8Array): Promise<string> {
  const hash = await crypto.subtle.digest("SHA-256", data as BufferSource);
  return Array.from(new Uint8Array(hash))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}
