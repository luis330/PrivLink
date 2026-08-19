/**
 * 从 Simple Icons CDN 拉取图标数据并生成 deploy/cloudflare/assets/simple-icons.json。
 * 运行：node scripts/fetch-simple-icons.mjs
 */

import { writeFileSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, "..");
const OUTPUT_PATH = resolve(ROOT, "deploy/cloudflare/assets/simple-icons.json");
const CDN_URL = "https://cdn.simpleicons.org/simple-icons.json";

async function main() {
  console.log(`Fetching Simple Icons from ${CDN_URL} ...`);
  const resp = await fetch(CDN_URL, { signal: AbortSignal.timeout(15000) });
  if (!resp.ok) {
    console.error(`Failed to fetch: ${resp.status} ${resp.statusText}`);
    process.exit(1);
  }
  const data = await resp.json() as Record<string, unknown>;
  console.log(`Fetched ${Object.keys(data).length} icons.`);
  writeFileSync(OUTPUT_PATH, JSON.stringify(data, null, 2), "utf-8");
  console.log(`Written to ${OUTPUT_PATH}`);
}

main().catch((err) => { console.error(err); process.exit(1); });
