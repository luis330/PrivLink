// 内联 JS 语法自检：把目标文件里的每个内联 <script> 块交给 V8 编译（不执行）。
//
// 覆盖范围：默认 = 仓库根 index.html + plugins/registry.json 里每个插件的 entry 文件。
// 插件也是单 HTML 自包含，语法错只会在沙箱 iframe 里白屏，没有别的门禁兜底。
// assets/ 副本无需重复编译：tests/test_frontend_sync.py 已保证它与根目录逐字节一致。
//
// 已知局限：只按 Script 编译，不认 type="module" 的内联块（本仓库 CSP 与插件静态审查
// 都禁止外部脚本，现状无模块脚本；真要用，需改用 vm.SourceTextModule 并加
// --experimental-vm-modules）。
//
// 用法：node scripts/check-inline-js.cjs [文件 ...]
// 退出码：0 = 全部通过，1 = 有文件未通过，2 = 目标文件不存在 / 插件清单不可读
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.join(__dirname, "..");
// 跳过带 src 的外部脚本（静态审查也禁止），只取内联块
const SCRIPT_RE = /<script(?![^>]*\bsrc\s*=)[^>]*>([\s\S]*?)<\/script>/g;

// 输出统一用正斜杠：Windows 的 path.relative 会给反斜杠，断言与人工比对都会踩
function displayPath(file) {
  return path.relative(ROOT, file).split(path.sep).join("/") || file;
}

function defaultTargets() {
  const targets = [path.join(ROOT, "index.html")];
  const registryPath = path.join(ROOT, "plugins", "registry.json");
  const registry = JSON.parse(fs.readFileSync(registryPath, "utf8"));
  const entries = Array.isArray(registry.plugins) ? registry.plugins : [];
  if (!entries.length) {
    // 防静默空检查：清单读空时宁可报错，也不能"只查首页就报通过"
    throw new Error("plugins/registry.json 未声明任何插件");
  }
  for (const entry of entries) {
    if (!entry || !entry.id) continue;
    targets.push(path.join(ROOT, "plugins", entry.id, entry.entry || "index.html"));
  }
  return targets;
}

function checkFile(file) {
  const relative = displayPath(file);
  if (!fs.existsSync(file)) {
    console.log(`FAIL ${relative}：文件不存在`);
    return { failed: true, missing: true };
  }
  const blocks = [...fs.readFileSync(file, "utf8").matchAll(SCRIPT_RE)];
  if (!blocks.length) {
    console.log(`FAIL ${relative}：未找到内联 <script> 块`);
    return { failed: true };
  }
  let chars = 0;
  let failed = false;
  blocks.forEach((match, index) => {
    chars += match[1].length;
    try {
      // filename 进编译产物：报错信息里带 block#序号，便于定位
      new vm.Script(match[1], { filename: `${relative}#block${index}` });
    } catch (error) {
      failed = true;
      console.log(`FAIL ${relative} block#${index}：${error.message}`);
    }
  });
  if (!failed) console.log(`OK   ${relative}：${blocks.length} 块 / ${chars} 字符`);
  return { failed: failed };
}

function main(argv) {
  const args = argv.slice(2);
  let targets;
  try {
    targets = args.length ? args.map((item) => path.resolve(item)) : defaultTargets();
  } catch (error) {
    console.log(`无法读取插件清单：${error.message}`);
    return 2;
  }
  let failed = 0;
  let missing = 0;
  for (const file of targets) {
    const result = checkFile(file);
    if (result.failed) failed += 1;
    if (result.missing) missing += 1;
  }
  if (failed) {
    console.log(`\n共 ${failed} 个文件未通过：内联 JS 语法检查失败`);
    return missing ? 2 : 1;
  }
  console.log(`内联 JS 语法检查通过：${targets.length} 个文件`);
  console.log("JS OK"); // 保留原脚本的成功标记，人工工作流沿用
  return 0;
}

process.exit(main(process.argv));
